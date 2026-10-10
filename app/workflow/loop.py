"""
app/workflow/loop.py
====================
★★★ 工具循环执行器 —— 整个 Agent 的骨架。

对应规格要求：
    「选工具循环执行 → 数据预处理计算同比/环比指标后注入 Context →
      LLM 生成子结论 → 循环直至完成 → 汇总报告」
    「工具层封装超时重试与错误分类，失败时 Fallback 直接生成」

一轮循环的精确流程：

    ① 规划：LLM 看当前 Context，决定"下一步调哪些工具、带什么参数"
             （或判断信息已足够，直接结束）
    ② 执行：Registry 执行工具（含超时 + 按错误类型重试）
    ③ 预处理：把原始返回值算好同比/环比，加工成可注入形态  ★ 关键
    ④ 子结论：LLM 基于"预处理后的数据 + 压缩历史"产出一轮子结论
    ⑤ 滚动：把本轮压进 Context，回到 ①

★ 三个设计要点，都是踩过坑之后的选择：

  1. **规划与执行分离**（而不是让 LangChain 的 agent 自己跑）
     因为规格明确要求"数据预处理后注入 Context"这一步。
     如果用现成的 ReAct agent，你就插不进第 ③ 步 ——
     工具结果会直接以原始形态进入消息历史，模型还得自己算环比。
     显式循环多写几十行，换来的是数据流的完全可控。

  2. **每轮都产出子结论，而不是最后一次性总结**
     这是长任务不跑偏的关键：每轮把"已确认什么"固定下来，
     下一轮基于它继续，避免模型在最后汇总时遗忘中间发现。

  3. **降级（Fallback）不是异常处理，是一等公民**
     工具全挂时不能直接报错。正确行为是：
     明确告知"以下结论未经数据验证"，然后基于已有信息给出可用输出。
     一个带免责声明的答案，比一个 500 错误有用得多。
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from app.context.builder import ContextBuilder, RoundRecord
from app.tools.parallel import ToolCallSpec, execute_batch
from app.tools.registry import RegisteredTool, registry
from app.workflow.preprocess import extract_key_numbers, preprocess_tool_result


# ---------------------------------------------------------------------------
# 规划器的输出结构
# ---------------------------------------------------------------------------

class ToolCall(BaseModel):
    """一次工具调用。"""

    tool: str = Field(description="要调用的工具名，必须来自给定列表")
    args: dict = Field(default_factory=dict, description="调用参数")
    purpose: str = Field(default="", description="调用目的，一句话说明想获取什么")


class PlanDecision(BaseModel):
    """规划结果：要么继续调工具，要么结束循环。"""

    done: bool = Field(
        description="信息是否已足够。true 表示不再需要工具，可以汇总输出"
    )
    reason: str = Field(default="", description="结束或继续的理由")
    calls: list[ToolCall] = Field(
        default_factory=list, description="要执行的工具调用（done=false 时至少一个）"
    )


# ---------------------------------------------------------------------------
# 循环结果
# ---------------------------------------------------------------------------

@dataclass
class LoopResult:
    """循环执行的最终结果。"""

    rounds: int = 0
    tool_calls: list[str] = field(default_factory=list)     # 调用过的工具名（按序）
    tool_errors: list[dict] = field(default_factory=list)   # 工具错误记录
    conclusions: list[str] = field(default_factory=list)    # 每轮子结论
    degraded: bool = False                                  # 是否走了降级路径
    degrade_reason: str = ""
    elapsed_ms: float = 0.0
    context_tokens: int = 0
    history: list[RoundRecord] = field(default_factory=list)
    # 每轮的并行执行指标（用于量化并行收益，可观测性）
    parallel_metrics: list[dict] = field(default_factory=list)

    def parallel_summary(self) -> dict:
        """汇总各轮执行模式的收益，便于监控与汇报。"""
        if not self.parallel_metrics:
            return {"rounds": 0}
        par = [m for m in self.parallel_metrics if m.get("parallel")]
        return {
            "rounds": len(self.parallel_metrics),
            "parallel_rounds": len(par),
            "serial_rounds": len(self.parallel_metrics) - len(par),
            "total_wall_ms": round(sum(m["wall_ms"] for m in self.parallel_metrics), 1),
            "total_net_saved_ms": round(
                sum(m.get("net_saved_ms", 0.0) for m in self.parallel_metrics), 1),
            "avg_speedup_when_parallel": (
                round(sum(m["speedup"] for m in par) / len(par), 2) if par else None
            ),
        }

    def summary(self) -> dict:
        return {
            "rounds": self.rounds,
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "degraded": self.degraded,
            "degrade_reason": self.degrade_reason,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "context_tokens": self.context_tokens,
            "parallel": self.parallel_summary(),
        }


# ---------------------------------------------------------------------------
# 规划器
# ---------------------------------------------------------------------------

PLANNER_SYSTEM = """你是一个工具调用规划器。你的任务是：根据当前已掌握的信息，决定下一步需要调用哪些工具，或者判断信息已经足够。

【决策原则】
1. 先看数据结构再取数：不确定表结构时先调 describe_tables；
2. 先确认数据范围再取数：调 get_data_range 避免查询不存在的区间；
3. **涉及转化率、销售额等异常波动的问题，必须先调 run_quality_checks**
   —— 缺货、改价、大促污染会让你的归因完全跑偏；
4. 指标类问题优先用专门的指标工具（compare_weeks 等），
   不要用 run_sql 自己算 —— 自算容易漏口径；
5. 一次可以调多个工具（互不依赖时并行更高效）；
6. **已经拿到的数据不要重复查**。检查"已完成的工作"部分 ——
   `describe_tables` / `get_data_range` 这类**元信息工具整个流程只需调一次**，
   重复调用纯属浪费轮次（实测出现过 5 轮里调 4 次 describe_tables 的情况）；
7. **交叉维度问题用 run_sql**。固定指标工具只覆盖"按商品"和"按渠道"两个单维度，
   若用户要的是**交叉组合**（如"按类目 × 渠道"）或工具没预设的切法，
   直接用 run_sql 写一次 JOIN 解决，不要靠反复调单维度工具去拼凑；
8. 信息足够时立即 done=true 结束，不要为了多调而多调；
9. 最多 {max_rounds} 轮。已进行 {current_round} 轮，请合理规划剩余轮次。

【可用工具】
{tools}

严格输出 JSON，不要输出其他内容。"""


def _format_tool_list(tools: list[RegisteredTool]) -> str:
    lines = []
    for t in tools:
        lines.append(f"- {t.name}: {t.description}")
    return "\n".join(lines)


def _planner_llm():
    from app.llm import structured_llm
    # 必须用 structured_llm（内部走 function_calling），
    # 不能用 with_structured_output 默认模式 —— DeepSeek 不支持 json_schema
    return structured_llm(PlanDecision, temperature=0.0)


def plan_next(context_text: str, tools: list[RegisteredTool],
              current_round: int, max_rounds: int,
              last_errors: list[str] | None = None) -> PlanDecision:
    """
    规划下一步。

    失败时（模型输出不合规 / 网络问题）返回 done=True 结束循环 ——
    规划失败时**继续循环是危险的**（可能在原地打转），
    结束循环并让上层汇总已有信息是更安全的选择。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    system = PLANNER_SYSTEM.format(
        max_rounds=max_rounds, current_round=current_round,
        tools=_format_tool_list(tools),
    )
    user = context_text

    if last_errors:
        user += (
            "\n\n【上一轮工具调用失败】\n"
            + "\n".join(f"  - {e}" for e in last_errors)
            + "\n请根据错误类型调整：数据类错误请改查询条件，"
              "权限类错误请换用其他工具。"
        )

    try:
        decision = _planner_llm().invoke([
            SystemMessage(content=system),
            HumanMessage(content=user),
        ])
    except Exception as e:                              # noqa: BLE001
        return PlanDecision(
            done=True,
            reason=f"规划器调用失败（{type(e).__name__}: {e}），"
                   f"结束循环并基于已有信息汇总。",
            calls=[],
        )

    # 校验工具名合法性：模型可能编造不存在的工具
    valid = {t.name for t in tools}
    clean_calls = []
    for c in decision.calls:
        if c.tool in valid:
            clean_calls.append(c)
        else:
            # 编造的工具名不要丢掉信息，转成一条提示
            last_errors = (last_errors or []) + [
                f"模型请求了不存在的工具「{c.tool}」，已忽略。"
            ]
    decision.calls = clean_calls

    if not decision.done and not decision.calls:
        decision.done = True
        decision.reason = (decision.reason or "") + "（无有效工具调用，结束循环）"

    return decision


# ---------------------------------------------------------------------------
# 子结论生成
# ---------------------------------------------------------------------------

CONCLUSION_SYSTEM = """你正在执行一个多轮数据分析任务的其中一轮。

【本轮任务】
基于本轮新获取的数据，产出一段简短的子结论（100~250 字）。

【子结论要求】
1. 只说你**从本轮数据中确实看到**的事实，不要推测；
2. 引用具体数字（数据已经过预处理，直接引用，不要自己算）；
3. 如果本轮数据出现异常或被污染（如缺货、大促），必须明确指出；
4. 如果本轮没有获取到有效数据，直接说"本轮未获取到有效数据"，不要编造；
5. **不要写行动建议** —— 建议在最终汇总时统一产出。

直接输出子结论正文，不要加"子结论："这类前缀。"""


def make_sub_conclusion(context_text: str, new_data_text: str) -> str:
    """基于本轮新数据产出子结论。失败时降级为"原始数据摘要"。"""
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm import build_llm

    try:
        resp = build_llm(temperature=0.2).invoke([
            SystemMessage(content=CONCLUSION_SYSTEM),
            HumanMessage(content=(
                f"{context_text}\n\n"
                + "=" * 60 + "\n"
                + f"【本轮新获取的数据（已预处理）】\n{new_data_text}"
            )),
        ])
        return (resp.content or "").strip()
    except Exception as e:                              # noqa: BLE001
        # 子结论生成失败不应中断循环 —— 直接把预处理数据留作记录
        return f"[子结论生成失败：{type(e).__name__}，本轮数据已注入上下文]\n{new_data_text[:300]}"


# ---------------------------------------------------------------------------
# ★ 主循环
# ---------------------------------------------------------------------------

def run_tool_loop(task: str, capabilities: list[str],
                  context: ContextBuilder,
                  max_rounds: int = 6,
                  on_progress=None) -> LoopResult:
    """
    执行工具循环。

    参数：
        task         : 用户任务
        capabilities : 意图路由决定的能力标签（决定装配哪些工具）
        context      : Context 构建器（须已注入指标/质控等预处理数据）
        max_rounds   : 最大轮次上限（防止无限循环）
        on_progress  : 进度回调，支持 fn(msg) 或 fn(round_no, msg) 两种签名
                       （由 safe_progress 自适应）
        max_rounds   : 最大轮次上限（防止无限循环）

    返回 LoopResult。
    """
    started = time.perf_counter()
    result = LoopResult()

    # 重置元信息工具的调用计数 —— 每次运行的"第一次"都必须是真正的第一次。
    # 忘了这一步会导致第二个请求拿不到表结构（计数在进程内累加）。
    from app.tools.builtin import reset_meta_tool_cache
    reset_meta_tool_cache()

    # --- 按能力动态装配工具（这就是"动态 Tool Registry"的落点）---
    tools = registry.select(capabilities=capabilities)
    if not tools:
        result.degraded = True
        result.degrade_reason = f"能力 {capabilities} 未匹配到任何工具"
        return result

    # 统一走签名自适应的安全回调，避免两处调用点约定不一致导致 TypeError
    from app.workflow.util import safe_progress

    def notify(msg: str):
        safe_progress(on_progress, result.rounds, msg)

    notify(f"装配工具 {len(tools)} 个：{[t.name for t in tools]}")

    last_errors: list[str] = []

    for round_no in range(1, max_rounds + 1):
        result.rounds = round_no
        context_text = context.build()

        # ---------- ① 规划 ----------
        decision = plan_next(context_text, tools, round_no, max_rounds, last_errors)
        notify(f"第{round_no}轮规划：{'结束' if decision.done else '调用工具'} "
               f"({decision.reason[:60]})")

        if decision.done or not decision.calls:
            break

        # ---------- ② 执行（★ 并行 + 超时重试 + 错误分类）----------
        #   ★ 企业级改造：同一轮内的多个工具调用互不依赖，
        #     改为并行执行。工具并行**不改变任何容错语义** ——
        #     超时、重试、错误分类、指数退避全部走同一份 registry 实现，
        #     只有并发方式不同（见 tools/parallel.py 的说明与取舍）。
        new_data: dict[str, Any] = {}        # 只装成功获取的数据
        failed_data: dict[str, Any] = {}     # 失败记录单独放
        round_tools: list[str] = []
        round_errors: list[str] = []

        specs = [ToolCallSpec(tool=c.tool, args=dict(c.args or {}))
                 for c in decision.calls]
        outcomes, par_metrics = execute_batch(specs)
        result.parallel_metrics.append(par_metrics)

        notify(f"第{round_no}轮执行 {par_metrics['calls']} 个工具："
               f"模式={par_metrics['mode']}，耗时 {par_metrics['wall_ms']:.0f}ms"
               + (f"，净省 {par_metrics['net_saved_ms']:.0f}ms"
                  if par_metrics.get("parallel") else ""))

        for outcome in outcomes:
            tool_name = outcome.spec.tool
            result.tool_calls.append(tool_name)
            round_tools.append(tool_name)

            # 工具失败：记录但不中断（其他工具的结果仍然有用）
            if not outcome.ok:
                err = {
                    "tool": tool_name,
                    "error_kind": outcome.error_kind,
                    "error": outcome.error,
                    "attempts": outcome.attempts,
                    "elapsed_ms": round(outcome.elapsed_ms, 1),
                    "parallel": par_metrics.get("parallel", False),
                }
                result.tool_errors.append(err)
                round_errors.append(
                    f"{tool_name} 失败（{outcome.error_kind}）：{outcome.error}"
                )
                failed_data[tool_name] = outcome.raw
                continue

            # ---------- ③ 预处理：算好同比/环比 ----------
            new_data[tool_name] = preprocess_tool_result(tool_name, outcome.raw)

        last_errors = round_errors

        # ★ 判断"本轮是否全部失败"必须依据 successful 的数量，
        #   而不是 new_data 是否为空 —— 失败记录曾经被混进 new_data，
        #   导致这个条件恒为假，降级逻辑形同虚设。
        if not new_data:
            notify(f"第{round_no}轮全部工具调用失败（{len(failed_data)} 个）")

            # 早停：同类永久性错误反复出现，继续重试纯属浪费
            permanent_failures = [
                e for e in result.tool_errors if e.get("error_kind") == "permanent"
            ]
            if len(permanent_failures) >= 2:
                result.degraded = True
                result.degrade_reason = (
                    f"出现连续的不可重试错误（{len(permanent_failures)} 次），"
                    f"继续调用无意义。最后一次："
                    f"{permanent_failures[-1].get('error', '')[:120]}"
                )
                break

            if len(result.tool_errors) >= 3:
                result.degraded = True
                result.degrade_reason = (
                    f"累计 {len(result.tool_errors)} 次工具失败，"
                    f"最后一次错误：{round_errors[0] if round_errors else '未知'}"
                )
                break
            continue

        # ---------- ④ 子结论 ----------
        from app.context.compressor import compress_injected_data
        new_data_text = compress_injected_data(new_data)
        conclusion = make_sub_conclusion(context_text, new_data_text)
        result.conclusions.append(conclusion)

        # ---------- ⑤ 滚动进 Context ----------
        key_numbers: dict = {}
        for tname, processed in new_data.items():
            if not tname.startswith("[失败]"):
                key_numbers.update(extract_key_numbers(tname, processed) or {})

        record = RoundRecord(
            round_no=round_no,
            tool_names=round_tools,
            conclusion=conclusion,
            key_numbers=key_numbers,
            injected_data=new_data,
            tool_errors=round_errors,
        )
        context.add_round(record)
        result.history.append(record)
        notify(f"第{round_no}轮完成，子结论 {len(conclusion)} 字")

    # --- 收尾统计 ---
    result.elapsed_ms = (time.perf_counter() - started) * 1000
    result.context_tokens = context.token_stats()["estimated_tokens"]

    # --- 降级判定：没有任何有效数据就必须降级 ---
    if not result.conclusions and not result.degraded:
        result.degraded = True
        result.degrade_reason = (
            "循环结束但未产出任何有效子结论（可能未匹配到可用工具或全部调用失败）"
        )

    return result
