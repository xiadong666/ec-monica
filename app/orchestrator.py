"""
app/orchestrator.py
===================
LangGraph 编排版本（对照实现）。

★ 为什么同时提供两份编排代码？

    services/weekly_review.py  ：普通 Python 控制流（生产用）
    app/orchestrator.py（本文件）：LangGraph 图编排（对照学习用）

    两者的业务逻辑完全一致，区别只在「怎么组织控制流」。

    这么做的目的：让你直观看到 —— 当流程本身是确定性的时候，
    LangGraph 带来的额外抽象**是负担还是收益**。

    本项目的结论是：确定性流程用普通控制流更清晰。
    LangGraph 真正的用武之地是「让模型决定下一步走哪个节点」的场景，
    例如：根据数据质量动态决定要不要跳过某个分析步骤、
         根据任务类型路由到不同的专家 Agent。

    你把两种写法都跑一遍，就能对"什么时候该上图编排"形成自己的判断 ——
    这比记住 LangGraph 的 API 重要得多，也是面试里真正能拉开差距的地方。

图的拓扑（含条件边与回环）：

    START → prepare(取数+质控) ─┬─ 有 blocking ─→ limited(受限分析) → END
                                 └─ 正常 ────────→ analyst → planner → critic
                                                                        │
                                              ┌── 不通过且未超轮次 ───────┘
                                              │        （带批评意见回到 analyst）
                                              └── 通过 / 超轮次 ─→ render → END
"""
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from app.config import settings
from app.metrics.compute import latest_complete_week, period_compare
from app.quality.checks import run_all_checks
from app.schemas import ActionProposal, Critique, Insight


def _keep_last(_old, new):
    """状态合并策略：新值覆盖旧值（用于重做时覆盖上一轮结果）。"""
    return new


class ReviewState(TypedDict, total=False):
    """周复盘共享状态。total=False 允许逐步填充。"""

    # --- 输入 ---
    start: str
    end: str
    focus: str
    max_revisions: int

    # --- 确定性产物 ---
    period: str
    metrics: dict
    changes: dict
    quality_flags: list
    quality_text: str
    has_blocking: bool

    # --- LLM 产物 ---
    analyst_input: str
    insights: Annotated[list[Insight], _keep_last]
    actions: Annotated[list[ActionProposal], _keep_last]
    critique: Annotated[Critique | None, _keep_last]

    # --- 循环控制 ---
    revisions: Annotated[int, _keep_last]
    critic_feedback: Annotated[str, _keep_last]
    headline: str


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------

def prepare_node(state: ReviewState) -> dict:
    """
    节点1：确定性准备（不调用任何 LLM）。

    取数、算指标、跑质控全部在这里完成。
    好处：这一步零成本、零随机性，且失败时错误信息非常明确。
    """
    start, end = state.get("start"), state.get("end")
    if not (start and end):
        start, end = latest_complete_week()
    period = f"{start} ~ {end}"

    comparison = period_compare(start, end, exclude_special=True)
    metrics = comparison["current"]
    qc = run_all_checks(start, end, period=metrics)

    return {
        "start": start,
        "end": end,
        "period": period,
        "metrics": metrics,
        "changes": comparison["change_pct"],
        "quality_flags": [
            {"code": f["code"], "level": f["level"], "message": f["message"]}
            for f in qc["flags"]
        ],
        "quality_text": qc["text"],
        "has_blocking": qc["has_blocking"],
        "revisions": 0,
    }


def route_after_prepare(state: ReviewState) -> str:
    """
    ★ 条件边：这是 LangGraph 真正有价值的场景 —— 
      流程走向取决于**运行时数据**（有没有 blocking 级质控问题），
      而不是写死的顺序。
    """
    return "limited" if state.get("has_blocking") else "analyst"


def limited_node(state: ReviewState) -> dict:
    """
    受限分析节点：存在 blocking 级数据问题时走这条路。

    此时不允许做"策略有效/无效"的因果归因，
    只输出问题本身与建议的下一步核查动作。
    """
    qc = state["quality_text"]
    period = state["period"]

    insights = [
        Insight(
            metric="data_quality",
            description=(
                f"本期数据存在阻断级质量问题，已跳过因果归因。"
                f"质控报告：{state['quality_flags'][0]['message'][:120]}"
            ),
            current_value=float(len(state["quality_flags"])),
            evidence=[f"run_quality_checks({period})"],
            confidence="high",
            is_confounded=True,
            confounders=[f["code"] for f in state["quality_flags"]],
        )
    ]
    actions = [
        ActionProposal(
            action_type="no_action",
            target="数据质量",
            summary="先修复数据质量问题，暂不做经营策略调整",
            rationale=(
                "本期存在阻断级数据问题（如缺货 / 大促污染），"
                "此时任何经营归因都不可靠，基于其调整策略反而会带来错误动作。"
            ),
            expected_impact="避免依据被污染的数据做出错误决策",
            risk="若该问题实际不影响经营，会延迟一次正常的策略调整窗口",
            priority="P0",
            needs_approval=True,
        )
    ]
    critique = Critique(
        passed=True,
        problems=[],
        must_fix=[],
        verdict="因存在阻断级数据质量问题，本流程按设计走「受限归因」路径，"
                "未进行因果推断 —— 这是正确的行为，而非缺陷。",
    )
    return {
        "insights": insights,
        "actions": actions,
        "critique": critique,
        "headline": f"数据存在阻断级问题，本期仅出质控报告：{qc[:80]}...",
    }


def analyst_node(state: ReviewState) -> dict:
    """节点2：分析 Agent（LLM）。把上一轮的批评意见一并回灌。"""
    from app.services.weekly_review import _build_analyst_input, _run_analyst_single

    prompt = _build_analyst_input(
        state["period"], state["metrics"], state["changes"],
        state["quality_text"], state.get("focus"),
    )
    if state.get("critic_feedback"):
        prompt += (
            "\n\n" + "=" * 60
            + "\n⚠️ 上一轮评审未通过，请针对性修正后重新输出。\n\n"
            + state["critic_feedback"]
        )

    insights = _run_analyst_single(prompt)
    return {"insights": insights, "analyst_input": prompt}


def planner_node(state: ReviewState) -> dict:
    """节点3：策略 Agent（LLM）。"""
    from app.services.weekly_review import _run_planner

    actions = _run_planner(
        state["period"], state["metrics"], state["quality_text"], state["insights"]
    )
    return {"actions": actions}


def critic_node(state: ReviewState) -> dict:
    """节点4：评审（确定性规则 + LLM 语义）。"""
    from app.agents.critic import review as critic_review

    critique = critic_review(
        period=state["period"],
        metrics=state["metrics"],
        quality_text=state["quality_text"],
        quality_flags=state["quality_flags"],
        insights=state.get("insights", []),
        actions=state.get("actions", []),
        sample_days=state["metrics"].get("sample_days", 0),
        min_days=settings.MIN_CONFIDENCE_SAMPLE_DAYS,
    )

    feedback = ""
    if not critique.passed and critique.must_fix:
        feedback = "【上一轮评审发现的问题】\n" + "\n".join(
            f"  - {p}" for p in critique.must_fix
        )

    return {
        "critique": critique,
        "critic_feedback": feedback,
        "revisions": state.get("revisions", 0) + (0 if critique.passed else 1),
    }


def route_after_critic(state: ReviewState) -> str:
    """
    ★ 回环节点：评审不通过就打回重做。

    这是整张图最有价值的一条边 —— 
    没有它，错误结论会直接进入最终报告。
    同时用 max_revisions 兜底，避免模型反复改不好导致无限循环。
    """
    critique = state.get("critique")
    if critique and not critique.passed:
        if state.get("revisions", 0) <= state.get("max_revisions", 1):
            return "analyst"
    return "render"


def render_node(state: ReviewState) -> dict:
    """节点5：渲染标题（正文由服务层统一渲染）。"""
    from app.services.weekly_review import _build_headline

    return {
        "headline": _build_headline(
            state["metrics"], state["changes"], state["quality_flags"]
        )
    }


# ---------------------------------------------------------------------------
# 构图
# ---------------------------------------------------------------------------

def build_review_graph():
    """构建并编译周复盘图。"""
    g = StateGraph(ReviewState)

    g.add_node("prepare", prepare_node)
    g.add_node("limited", limited_node)
    g.add_node("analyst", analyst_node)
    g.add_node("planner", planner_node)
    g.add_node("critic", critic_node)
    g.add_node("render", render_node)

    g.add_edge(START, "prepare")
    # 条件边：根据质控结果决定走哪条路
    g.add_conditional_edges(
        "prepare", route_after_prepare,
        {"limited": "limited", "analyst": "analyst"},
    )
    g.add_edge("limited", END)
    g.add_edge("analyst", "planner")
    g.add_edge("planner", "critic")
    # 回环：评审不通过则回到 analyst
    g.add_conditional_edges(
        "critic", route_after_critic,
        {"analyst": "analyst", "render": "render"},
    )
    g.add_edge("render", END)

    return g.compile()


_graph = None


def run_review_graph(start: str | None = None, end: str | None = None,
                     focus: str | None = None, max_revisions: int = 1) -> dict:
    """用 LangGraph 执行周复盘，返回最终状态。"""
    global _graph
    if _graph is None:
        _graph = build_review_graph()

    return _graph.invoke({
        "start": start or "",
        "end": end or "",
        "focus": focus or "",
        "max_revisions": max_revisions,
    })
