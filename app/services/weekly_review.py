"""
app/services/weekly_review.py
=============================
周复盘主流程（编排层）。

★ 一个重要的工程判断：这里**没有**用 LangGraph，而是用普通 Python 控制流。

为什么？——因为这条流水线的控制逻辑是确定性的：
    取数 → 质控 → 切分区块 → 分析 → 策略 → 评审 → (打回 | 通过)
每个节点的进出条件都是明确的 if/else，没有需要"让模型决定下一步走哪"的地方。

    LangGraph 的价值在于「模型驱动的动态路由」：
    下一步走哪个节点由 LLM 判断时，图结构才真正带来好处。
    如果流程本来就是确定的，套上图编排只会：
      · 增加一层调试难度（出错时先要判断是逻辑错还是图错）
      · 增加状态传递的样板代码
      · 让"审计留痕"变复杂（循环次数、每轮输入输出都要额外处理）

    结论：**图编排不是越多越好，用它解决它擅长的问题。**
    在项目的 orchestrator.py 里保留了 LangGraph 版本作为对照，
    你可以直观比较两种写法的差异，自行判断哪种更适合你的场景。

⚠️ 本模块最重要的设计：**确定性骨架包住 LLM 环节**
    取数、质控、指标计算全部由代码完成并直接写入结果，
    LLM 只负责"解读"和"建议"，且解读结果要经过双层评审。
    这样即使模型出错，也不会污染数字本身。
"""
import json
from datetime import datetime

from app.agents.analyst import get_analyst
from app.agents.critic import review as critic_review
from app.agents.planner import get_planner
from app.config import settings
from app.metrics.compute import latest_complete_week, period_compare, period_summary
from app.quality.checks import run_all_checks
from app.agents.critic import _fmt_actions, _fmt_insights, _fmt_metrics
from app.schemas import ActionProposal, Insight, WeeklyReview


def run_weekly_review(start: str | None = None, end: str | None = None,
                      focus: str | None = None,
                      max_revisions: int = 1) -> WeeklyReview:
    """
    执行一次完整的周复盘。

    参数：
        start, end    : 复盘区间，留空则自动取数据中最后一个完整自然周
        focus         : 关注点（如"转化率下滑"），留空则做全面复盘
        max_revisions : 评审打回后最多重做几次（0 表示不做重做循环）

    返回结构化的 WeeklyReview 对象。
    """
    # =====================================================================
    # 步骤 1：确定区间（确定性）
    # =====================================================================
    if not (start and end):
        start, end = latest_complete_week()
    period = f"{start} ~ {end}"

    # =====================================================================
    # 步骤 2：指标计算（确定性，数字的唯一来源）
    #    ★ 这里算出来的数字会直接写进最终报告，不经过 LLM。
    #      所以报告里的数字永远可信，LLM 只负责解释它们。
    # =====================================================================
    comparison = period_compare(start, end, exclude_special=True)
    metrics = comparison["current"]
    changes = comparison["change_pct"]

    # =====================================================================
    # 步骤 3：质控（确定性，硬约束）
    #    ★ 由代码运行，不依赖 Agent 是否有"记得"调用工具。
    #      这点很关键：如果靠 Agent 自觉调用质控工具，
    #      它偶尔漏调一次，错误结论就直接发出去了。
    # =====================================================================
    qc = run_all_checks(start, end, period=metrics)
    quality_flags = qc["flags"]
    quality_text = qc["text"]

    # =====================================================================
    # 步骤 4：分析（LLM）—— 输入含质控硬约束，从源头防止归因污染
    # =====================================================================
    analyst_input = _build_analyst_input(period, metrics, changes, quality_text, focus)
    insights, revision_rounds = _run_analyst_with_revisions(
        analyst_input, metrics, quality_text, quality_flags, max_revisions,
        period=period,
    )

    # =====================================================================
    # 步骤 5：策略（LLM）—— 必须引用分析结论
    # =====================================================================
    actions = _run_planner(period, metrics, quality_text, insights)

    # =====================================================================
    # 步骤 6：评审（确定性规则 + LLM 语义）
    # =====================================================================
    critique = critic_review(
        period=period,
        metrics=metrics,
        quality_text=quality_text,
        quality_flags=quality_flags,
        insights=insights,
        actions=actions,
        sample_days=metrics.get("sample_days", 0),
        min_days=settings.MIN_CONFIDENCE_SAMPLE_DAYS,
    )

    # =====================================================================
    # 步骤 7：组装（叙述文字由确定性模板渲染，保证数字与结论一致）
    # =====================================================================
    headline = _build_headline(metrics, changes, quality_flags)
    narrative = _build_narrative(period, metrics, changes, insights, actions,
                                 quality_text, critique, quality_flags, headline)

    return WeeklyReview(
        period=period,
        headline=headline,
        metrics=_flatten_metrics(metrics, changes),
        insights=insights,
        actions=actions,
        narrative=narrative,
        quality_flags=[
            {"code": f["code"], "level": f["level"], "message": f["message"]}
            for f in quality_flags
        ],
        critique=critique,
        revision_rounds=revision_rounds,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# ---------------------------------------------------------------------------
# 内部：各步骤实现
# ---------------------------------------------------------------------------

def _build_analyst_input(period: str, metrics: dict, changes: dict,
                         quality_text: str, focus: str | None) -> str:
    """组装给分析 Agent 的输入。质控文本放在最前面，确保它无法忽略。"""
    lines = [
        quality_text,                      # ← 硬约束放最前
        "=" * 60,
        f"【复盘区间】{period}",
        f"（样本天数 {metrics.get('sample_days')} 天，已剔除大促等特殊日期）",
        "",
        "【本周核心指标】",
        _fmt_metrics(metrics),
        "",
        "【环比变化（对比上一周）】",
    ]
    name_map = {
        "gmv_gross": "成交额", "gmv_net": "净成交额", "paid_orders": "订单数",
        "aov": "客单价", "pay_cvr": "支付转化率", "visitors": "访客数",
        "refund_rate": "退款率", "gross_margin_rate": "毛利率", "ctr": "点击率",
    }
    for k, label in name_map.items():
        v = changes.get(k)
        if v is None:
            continue
        lines.append(f"  - {label}: {v:+.2%}")

    if focus:
        lines += ["", f"【本次复盘重点关注】{focus}"]

    lines += [
        "",
        "请按职责要求，先用工具核实数据（含质控检查），"
        "再产出 3~5 条归因明确、证据充分的 Insight。",
    ]
    return "\n".join(lines)


def _extract_structured(result: dict, model_cls, many: bool):
    """
    从 LangChain Agent 的返回中取出结构化结果。

    create_agent 配合 response_format 时，结构化对象在
    result["structured_response"]。这里做容错处理，并在失败时给出清晰报错，
    避免"返回了但拿不到"这种难排查的情况。
    """
    resp = result.get("structured_response")
    if resp is None:
        last = result.get("messages", [])
        tail = last[-1].content if last else "(无消息)"
        raise RuntimeError(
            f"Agent 未返回结构化结果（期望 {model_cls.__name__}）。"
            f"模型最后输出：{str(tail)[:400]}"
        )
    if many:
        if isinstance(resp, model_cls):
            return [resp]
        return list(resp)
    return resp


def _run_analyst_single(prompt: str) -> list[Insight]:
    """执行一次分析 Agent 调用，返回 Insight 列表。"""
    result = get_analyst().invoke({"messages": [{"role": "user", "content": prompt}]})
    insights = _extract_structured(result, Insight, many=True)
    return insights if isinstance(insights, list) else [insights]


def _run_analyst_with_revisions(analyst_input: str, metrics: dict,
                                quality_text: str, quality_flags: list[dict],
                                max_revisions: int, period: str):
    """
    运行分析 Agent，并在评审发现硬问题时打回重做。

    ★ 重做时把「质控标记 + 上一轮的问题」一起回灌，
      而不是让它自由重来 —— 否则它很可能犯同样的错。
    """
    from app.quality.rules import (
        check_confounder_acknowledgement, check_insight_text,
    )

    prompt = analyst_input
    insights: list[Insight] = []
    rounds = 0

    for attempt in range(max_revisions + 1):
        insights = _run_analyst_single(prompt)

        # 只检查与 Insight 相关的硬规则（行动建议还没生成，不查它）
        problems = check_insight_text(
            insights, quality_flags, metrics.get("sample_days", 0),
            settings.MIN_CONFIDENCE_SAMPLE_DAYS,
        )
        problems += check_confounder_acknowledgement(insights, quality_flags)

        if not problems or attempt == max_revisions:
            rounds = attempt
            break

        rounds = attempt + 1
        prompt = (
            f"{analyst_input}\n\n"
            + "=" * 60 + "\n"
            + "⚠️ 你上一轮的输出未通过校验，请针对性修正后重新输出。\n\n"
            + "【上一轮的问题】\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\n\n【上一轮结论（仅作参考，可推翻）】\n"
            + _fmt_insights(insights)
            + "\n\n请重新产出完整的 Insight 列表。"
        )

    return insights, rounds


def _run_planner(period: str, metrics: dict, quality_text: str,
                 insights: list[Insight]) -> list[ActionProposal]:
    """运行策略 Agent。"""
    prompt = "\n".join([
        quality_text,
        "=" * 60,
        f"【复盘区间】{period}",
        "",
        "【本周核心指标】",
        _fmt_metrics(metrics),
        "",
        "【分析结论（你的建议必须引用其中的条目）】",
        _fmt_insights(insights),
        "",
        "请据此产出 2~4 条行动建议；若确实无需干预，只输出一条 no_action。",
    ])
    result = get_planner().invoke({"messages": [{"role": "user", "content": prompt}]})
    actions = _extract_structured(result, ActionProposal, many=True)
    return actions if isinstance(actions, list) else [actions]


def _build_headline(metrics: dict, changes: dict, quality_flags: list[dict]) -> str:
    """
    生成一句话总览。由代码生成而非 LLM —— 保证它永远不会说错数字。
    """
    parts = []
    gmv_chg = changes.get("gmv_gross")
    if gmv_chg is not None:
        parts.append(f"成交额 {metrics['gmv_gross']:,.0f} 元（环比 {gmv_chg:+.1%}）")

    majors = [f for f in quality_flags if f["level"] == "error"]
    if majors:
        parts.append(f"存在 {len(majors)} 项需优先处理的数据问题（{majors[0]['code']}）")
    elif any(f["level"] == "warning" for f in quality_flags):
        parts.append("存在需关注的经营异常，详见质控标记")

    return "；".join(parts) if parts else "本周无显著异常"


def _flatten_metrics(metrics: dict, changes: dict) -> dict:
    """把指标与环比合并成扁平 dict，方便前端直接渲染。"""
    out = {}
    for k, v in metrics.items():
        if k in ("period", "excluded_special"):
            continue
        out[k] = v
        if k in changes:
            out[f"{k}_change_pct"] = changes[k]
    return out


def _build_narrative(period: str, metrics: dict, changes: dict,
                     insights: list[Insight], actions: list[ActionProposal],
                     quality_text: str, critique, quality_flags: list[dict],
                     headline: str) -> str:
    """
    渲染 Markdown 复盘正文。

    ★ 注意：正文的骨架和数字由代码渲染，LLM 只贡献 Insight 与 Action 的内容。
      这样做的好处是"报告里的数字永远与指标层一致"，
      不会出现模型把 123013 写成 12313 的情况。
    """
    lines = [
        f"# 经营周复盘：{period}",
        "",
        f"> {headline}",
        "",
        "## 一、核心指标",
        "",
        "| 指标 | 本周 | 环比 |",
        "| --- | --- | --- |",
    ]
    rows = [
        ("成交额(GMV)", "gmv_gross", "num"),
        ("净成交额", "gmv_net", "num"),
        ("订单数", "paid_orders", "int"),
        ("客单价", "aov", "num"),
        ("支付转化率", "pay_cvr", "pct"),
        ("访客数", "visitors", "int"),
        ("退款率", "refund_rate", "pct"),
        ("毛利率", "gross_margin_rate", "pct"),
    ]
    for label, key, kind in rows:
        if key not in metrics:
            continue
        v = metrics[key]
        if kind == "pct":
            cur = f"{v:.2%}"
        elif kind == "int":
            cur = f"{v:,}"
        else:
            cur = f"{v:,.2f}"
        chg = changes.get(key)
        chg_s = f"{chg:+.1%}" if chg is not None else "—"
        lines.append(f"| {label} | {cur} | {chg_s} |")

    lines += [
        "",
        "## 二、数据质控",
        "",
        quality_text,
        "",
        "## 三、归因分析",
        "",
    ]
    if insights:
        for i, ins in enumerate(insights, 1):
            flag = " ⚠️受污染" if ins.is_confounded else ""
            lines.append(f"**{i}. [{ins.metric}] {ins.description}**{flag}")
            lines.append("")
            chg = f"{ins.change_pct:+.2%}" if ins.change_pct is not None else "—"
            lines.append(f"- 当前值 {ins.current_value}，上期 {ins.previous_value}，变化 {chg}")
            lines.append(f"- 置信度：{ins.confidence}")
            if ins.confounders:
                lines.append(f"- 污染因素：{'；'.join(ins.confounders)}")
            if ins.evidence:
                lines.append(f"- 证据：{'；'.join(ins.evidence)}")
            lines.append("")
    else:
        lines += ["（无）", ""]

    lines += ["## 四、行动建议", ""]
    if actions:
        for i, a in enumerate(actions, 1):
            lines.append(f"**{i}. [{a.priority}] {a.summary}**")
            lines.append("")
            lines.append(f"- 类型：`{a.action_type}`　对象：{a.target}")
            lines.append(f"- 依据：{a.rationale}")
            lines.append(f"- 预期影响：{a.expected_impact}")
            lines.append(f"- 风险：{a.risk}")
            lines.append(f"- 需人工审批：{'是' if a.needs_approval else '否'}")
            lines.append("")
    else:
        lines += ["（无）", ""]

    lines += ["## 五、评审意见", ""]
    if critique:
        status = "✅ 通过" if critique.passed else "❌ 未通过"
        lines.append(f"**评审结论：{status}**")
        lines.append("")
        lines.append(f"> {critique.verdict}")
        lines.append("")
        if critique.problems:
            lines.append("发现的问题：")
            lines.append("")
            for p in critique.problems:
                lines.append(f"- {p}")
            lines.append("")

    lines += [
        "---",
        "",
        f"*本报告由 EC-Monica 于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 生成。*",
        "*核心指标由指标层直接计算（不经 LLM），归因与建议由 LLM 生成并经双层评审。*",
    ]
    return "\n".join(lines)
