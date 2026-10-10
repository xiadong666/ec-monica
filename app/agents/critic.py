"""
app/agents/critic.py
====================
★★ 评审 Agent —— 本项目价值最高的一个环节。

为什么必须有它？
    电商归因极易出错，而且错误往往"看起来很有道理"：
      · 把缺货当成详情页问题
      · 把大促峰值当成策略有效
      · 把加预算导致的 ROI 下滑当成渠道不行
    一个专门挑刺的 Agent，比多一个分析 Agent 有用得多。

架构要点：**确定性规则 + LLM 语义审查** 双层
    第一层（quality/rules.py）：正则与字段校验，零漏判、零成本、不花 token。
    第二层（本文件的 LLM critic）：查语义问题 —— 逻辑自洽性、
      归因是否牵强、有没有把相关性当因果、有没有幸存者偏差。
    顺序不能颠倒：能由代码判定的，绝不交给模型判。
"""
from langchain_core.messages import HumanMessage, SystemMessage

from app.llm import build_llm
from app.quality.rules import (
    check_action_safety,
    check_confounder_acknowledgement,
    check_insight_text,
)
from app.schemas import ActionProposal, Critique, Insight

CRITIC_PROMPT = """你是一名极其严格的电商数据评审专家。你的任务是**挑毛病**，不是附和。

你会收到：①核心指标 ②数据质控标记 ③分析结论 ④行动建议。

【请重点审查以下五类问题】
1. **归因污染**：是否把"缺货/改价/大促/流量结构变化"造成的波动，
   错误归因为"策略有效"或"商品/页面问题"？
2. **相关性当因果**：是否仅凭两件事同时发生就断定因果？
   例如"改了主图后转化率涨了"——同期有没有其他变化？
3. **幸存者偏差 / 选择性举证**：是否只挑了支持自己结论的数据，
   忽略了反例？
4. **置信度虚高**：样本是否足够？有污染却标 high 置信度的，直接判不通过。
5. **动作与结论不匹配**：
   - ROI 下降就建议关停渠道 → 错误（除非确认 ROI 已低于盈亏平衡线）；
   - 缺货却建议优化详情页 → 错误（方向完全错了）；
   - 给出"建议关注/持续观察"这类无法执行的话 → 属于无效建议。

【输出要求】
- passed: 结论是否可信可放行；
- problems: 逐条列出你发现的问题（每一条都要具体，不要泛泛而谈）；
- must_fix: 必须修正的点，会被回灌给分析 Agent 重新做；
- verdict: 一句话总评。

【重要】不要为了显得严格而编造问题。
如果结论确实严谨（证据充分、污染已排除、置信度匹配、动作与结论一致），
就应判定 passed=true。无端打回会让流程空转，同样是失职。
"""


def deterministic_review(insights: list[Insight], actions: list[ActionProposal],
                         quality_flags: list[dict], sample_days: int,
                         min_days: int) -> list[str]:
    """第一层：确定性规则校验。返回问题列表。"""
    problems: list[str] = []
    problems += check_insight_text(insights, quality_flags, sample_days, min_days)
    problems += check_action_safety(actions)
    problems += check_confounder_acknowledgement(insights, quality_flags)
    return problems


_critic_llm = None


def _get_critic_llm():
    global _critic_llm
    if _critic_llm is None:
        # 用 structured_llm 而非 with_structured_output 默认模式：
        # DeepSeek 不支持 json_schema，必须走 function_calling
        from app.llm import structured_llm
        _critic_llm = structured_llm(Critique, temperature=0.0)
    return _critic_llm


def llm_review(period: str, metrics: dict, quality_text: str,
               insights: list[Insight], actions: list[ActionProposal]) -> Critique:
    """第二层：LLM 语义审查。"""
    payload = [
        f"【复盘区间】{period}",
        "",
        "【核心指标（由指标层直接计算，可信）】",
        _fmt_metrics(metrics),
        "",
        "【数据质控标记（硬约束）】",
        quality_text or "无异常",
        "",
        "【分析结论】",
        _fmt_insights(insights),
        "",
        "【行动建议】",
        _fmt_actions(actions),
    ]
    messages = [
        SystemMessage(content=CRITIC_PROMPT),
        HumanMessage(content="\n".join(payload)),
    ]
    return _get_critic_llm().invoke(messages)


def review(period: str, metrics: dict, quality_text: str,
           quality_flags: list[dict], insights: list[Insight],
           actions: list[ActionProposal], sample_days: int,
           min_days: int) -> Critique:
    """
    完整评审：先跑确定性规则（零漏判），再跑 LLM 语义审查。

    两者的结论做「或」合并 —— 任意一层发现问题就算不通过。
    确定性规则的问题优先展示，因为它更硬。
    """
    hard = deterministic_review(insights, actions, quality_flags, sample_days, min_days)

    semantic: Critique | None = None
    try:
        semantic = llm_review(period, metrics, quality_text, insights, actions)
    except Exception as e:
        # LLM 评审失败不应阻断流程，但必须留痕 —— 不能假装评审过了
        semantic = Critique(
            passed=True,
            problems=[],
            must_fix=[],
            verdict=f"[警告] LLM 语义评审未能执行（{type(e).__name__}: {e}）。"
                    f"本次仅通过确定性规则校验，语义层面未经审查。",
        )

    all_problems = hard + semantic.problems
    must_fix = list(dict.fromkeys(hard + semantic.must_fix))   # 去重保序

    return Critique(
        passed=(not hard) and semantic.passed,
        problems=all_problems,
        must_fix=must_fix,
        verdict=semantic.verdict,
    )


# ---------------------------------------------------------------------------
# 渲染辅助
# ---------------------------------------------------------------------------

def _fmt_metrics(m: dict) -> str:
    keys = [
        ("gmv_gross", "成交额", "num"), ("gmv_net", "净成交额", "num"),
        ("paid_orders", "订单数", "int"), ("aov", "客单价", "num"),
        ("pay_cvr", "支付转化率", "pct"), ("refund_rate", "退款率", "pct"),
        ("gross_margin_rate", "毛利率", "pct"), ("visitors", "访客数", "int"),
    ]
    lines = []
    for k, name, kind in keys:
        if k not in m:
            continue
        v = m[k]
        if kind == "pct":
            lines.append(f"  - {name}: {v:.2%}")
        elif kind == "int":
            lines.append(f"  - {name}: {v:,}")
        else:
            lines.append(f"  - {name}: {v:,.2f}")
    return "\n".join(lines) or "  （无）"


def _fmt_insights(insights: list[Insight]) -> str:
    if not insights:
        return "  （无）"
    lines = []
    for i, ins in enumerate(insights, 1):
        chg = f"{ins.change_pct:+.2%}" if ins.change_pct is not None else "—"
        lines.append(f"  [{i}] {ins.metric}: {ins.description}")
        lines.append(f"      当前={ins.current_value} 上期={ins.previous_value} 变化={chg}")
        lines.append(f"      证据={ins.evidence}  置信度={ins.confidence}  "
                     f"受污染={ins.is_confounded} 污染因素={ins.confounders}")
    return "\n".join(lines)


def _fmt_actions(actions: list[ActionProposal]) -> str:
    if not actions:
        return "  （无）"
    lines = []
    for i, a in enumerate(actions, 1):
        lines.append(f"  [{i}] ({a.priority}/{a.action_type}) {a.summary}")
        lines.append(f"      对象={a.target}  需审批={a.needs_approval}")
        lines.append(f"      依据={a.rationale}")
        lines.append(f"      预期={a.expected_impact}")
        lines.append(f"      风险={a.risk}")
    return "\n".join(lines)
