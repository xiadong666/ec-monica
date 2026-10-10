"""
app/quality/rules.py
====================
★★ 确定性规则校验（Deterministic Guardrails）。

这里体现本项目最核心的一个判断：

    **能让代码判断的事，绝不要交给 LLM 判断。**

"结论里有没有出现被禁止的表述""建议里有没有提改价但没标审批"
这类检查，用正则和字段判断是 100% 可靠的；
交给 LLM 评审则有概率漏判 —— 而漏判的代价是发出错误建议。

LLM 评审（critic.py）只负责**语义层面**的事：
逻辑是否自洽、归因是否牵强、有没有幸存者偏差。
两者结合：确定性规则先跑（快、零成本、零漏判），LLM 再补语义审查。
"""
import re

from app.schemas import ActionProposal, Insight


# ---------------------------------------------------------------------------
# 禁止性表述：出现即判不通过
# ---------------------------------------------------------------------------

# 有 blocking 质控问题时，禁止出现「策略有效/无效」类因果结论
#   ⚠️ 实测踩过的坑：最初只写了肯定式 `策略(有效|成功|见效)`，
#      结果"运营策略效果不佳"这种**否定式**漏网了。
#      而在真实场景里，否定式的错误归因反而更常见
#      （把缺货造成的下跌说成"策略失败"），所以必须把否定式一并覆盖。
FORBIDDEN_CAUSAL_PATTERNS = [
    r"策略(有效|成功|见效|奏效|失败|无效|不佳|不到位)",
    r"策略.{0,4}效果(不佳|不好|很好|显著|明显)",
    r"运营(做得好|做到位|有效|失败|不到位|策略.{0,4}(有效|无效|不佳))",
    r"效果(不佳|不好|很好|显著|明显).{0,6}(策略|运营|投放)",
    r"(证明|说明|表明).{0,12}(策略|运营|投放).{0,6}(有效|成功|失败|无效|奏效)",
    r"投放(效果很好|很成功|效果不佳|失败)",
    r"归因于.{0,10}(策略|运营|投放)",
]

# 样本不足时禁止使用的措辞
FORBIDDEN_TREND_WORDS = [r"趋势", r"持续(下滑|上升|增长)", r"稳步"]

# 无依据的强断言
FORBIDDEN_ABSOLUTE = [r"一定(会|能)", r"必然", r"保证", r"100%", r"肯定会"]


def check_insight_text(insights: list[Insight], quality_flags: list[dict],
                       sample_days: int, min_days: int) -> list[str]:
    """对 Insight 文本做确定性校验，返回问题列表。"""
    problems: list[str] = []
    has_blocking = any(f.get("level") == "error" for f in quality_flags)

    for i, ins in enumerate(insights, 1):
        text = ins.description

        # 1) 有 blocking 时不许下因果结论
        if has_blocking:
            for pat in FORBIDDEN_CAUSAL_PATTERNS:
                if re.search(pat, text):
                    problems.append(
                        f"Insight[{i}] 出现「{re.search(pat, text).group(0)}」这类因果结论，"
                        f"但质控存在 blocking 级问题（数据不可用于因果归因）。"
                    )

        # 2) 样本不足时不许说"趋势"
        if sample_days < min_days:
            for pat in FORBIDDEN_TREND_WORDS:
                if re.search(pat, text):
                    problems.append(
                        f"Insight[{i}] 使用了「{re.search(pat, text).group(0)}」描述，"
                        f"但样本仅 {sample_days} 天（<{min_days} 天），不足以支撑趋势判断。"
                    )

        # 3) 无依据的绝对化断言
        for pat in FORBIDDEN_ABSOLUTE:
            if re.search(pat, text):
                problems.append(
                    f"Insight[{i}] 出现绝对化断言「{re.search(pat, text).group(0)}」，"
                    f"经营预测不允许使用必然性表述。"
                )

        # 4) evidence 不能为空 —— 没有证据的结论等于猜测
        if not ins.evidence:
            problems.append(
                f"Insight[{i}]（{ins.metric}）缺少 evidence，结论必须注明数据来源。"
            )

        # 5) 被污染却标了高置信度 → 自相矛盾
        if ins.is_confounded and ins.confidence == "high":
            problems.append(
                f"Insight[{i}]（{ins.metric}）标记为受污染(is_confounded=true)，"
                f"却给出 high 置信度，二者矛盾。"
            )

        # 6) 声称受污染却没写具体污染因素
        if ins.is_confounded and not ins.confounders:
            problems.append(
                f"Insight[{i}]（{ins.metric}）is_confounded=true 但 confounders 为空，"
                f"必须写明具体污染因素。"
            )

    return problems


def check_action_safety(actions: list[ActionProposal]) -> list[str]:
    """对行动建议做安全校验，返回问题列表。"""
    problems: list[str] = []

    # 涉及钱的动作类型：必须人工审批
    MONEY_ACTIONS = {"price_change", "ad_budget"}

    for i, a in enumerate(actions, 1):
        if a.action_type in MONEY_ACTIONS and not a.needs_approval:
            problems.append(
                f"Action[{i}]（{a.action_type}）涉及价格/预算，"
                f"needs_approval 必须为 true，不得自动执行。"
            )
        if not a.rationale or len(a.rationale.strip()) < 10:
            problems.append(f"Action[{i}] 缺少有效依据(rationale)。")
        if not a.risk or len(a.risk.strip()) < 5:
            problems.append(
                f"Action[{i}] 未说明风险。电商动作必须写明副作用，"
                f"没有无风险的动作。"
            )

    # 无动作建议必须是唯一一条（避免既说没问题又列一堆动作）
    if len(actions) > 1 and any(a.action_type == "no_action" for a in actions):
        problems.append(
            "同时给出了 no_action 与其他行动建议，结论自相矛盾："
            "要么建议不动作，要么给出具体动作。"
        )

    if not actions:
        problems.append("未产出任何行动建议（至少应给出 no_action）。")

    return problems


def check_confounder_acknowledgement(insights: list[Insight],
                                     quality_flags: list[dict]) -> list[str]:
    """
    检查 Agent 是否承认了质控发现的污染因素。

    这是防"归因污染"的最后一道闸：
    质控说 P001 缺货了，但所有 Insight 都没提到缺货，
    说明 Agent 忽略了关键信息 —— 结论不可信。
    """
    problems: list[str] = []
    if not quality_flags:
        return problems

    blob = " ".join(
        f"{i.description} {' '.join(i.confounders)}" for i in insights
    )

    # 缺货 / 改价 / 大促 / 流量结构 这四类，质控一旦报告就必须被提及
    must_mention = {
        "STOCKOUT_FOUND": ("缺货", "断货", "库存"),
        "PRICE_CHANGE_DETECTED": ("价", "涨价", "调价"),
        "SPECIAL_PERIOD_INCLUDED": ("大促", "618", "特殊日期"),
        "CHANNEL_MIX_SHIFT": ("流量结构", "渠道结构", "占比"),
    }

    reported = {f.get("code") for f in quality_flags}

    for code, keywords in must_mention.items():
        if code in reported and not any(k in blob for k in keywords):
            problems.append(
                f"质控已报告 {code}，但分析结论中完全没有提及"
                f"（未出现任一关键词：{keywords}）。"
                f"说明该污染因素被忽略了，归因不完整。"
            )

    return problems
