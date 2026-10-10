"""
app/workflow/engine.py
======================
★★ 主引擎：把意图识别、条件路由、工具循环、上下文工程、评审串成完整工作流。

整体数据流（对应规格的每一环）：

    用户请求
       │
       ├─ ① 意图识别（规则优先，LLM 兜底）
       │
       ├─ ② 条件路由 ─┬─ need_tools=False → 【直接生成】一次 LLM 调用 → 输出
       │              └─ need_tools=True  → ③
       │
       ├─ ③ 数据预处理：先把"基础指标 + 质控"算好并注入 Context
       │      ★ 注意：这一步在循环**之前**做，不是等模型来调。
       │        因为无论什么分析任务，基础指标与数据质控都必须先有 ——
       │        质控尤其不能依赖模型自觉调用（它偶尔会漏，而漏一次结论就错了）
       │
       ├─ ④ 工具循环：选工具 →（超时重试）执行 → 预处理算环比 →
       │     注入 Context → LLM 子结论 → 循环
       │
       ├─ ⑤ 汇总生成：基于压缩后的全部前轮结论 + 范文模板 → 最终输出
       │
       ├─ ⑥ 评审：确定性规则 + LLM 语义 →（不通过且未超轮次）回到 ⑤
       │
       └─ ⑦ 降级：工具全挂时，明确标注"未经数据验证"后直接生成
"""
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.config import settings
from app.context.builder import ContextBuilder
from app.context.compressor import compress_injected_data
from app.llm import LLMNotConfigured
from app.metrics.compute import latest_complete_week, period_compare
from app.quality.checks import run_all_checks
from app.quality.rules import check_confounder_acknowledgement, check_insight_text
from app.schemas import Critique, Insight
from app.workflow.intent import Intent, IntentResult, classify
from app.workflow.loop import LoopResult, run_tool_loop
from app.workflow.preprocess import enrich_period_compare
from app.workflow.util import safe_progress


@dataclass
class AgentResponse:
    """一次 Agent 调用的完整结果（含全部可观测信息）。"""

    # --- 主要产物 ---
    content: str = ""
    intent: IntentResult | None = None

    # --- 执行过程 ---
    loop: LoopResult | None = None
    context_tokens: int = 0
    elapsed_ms: float = 0.0

    # --- 质量 ---
    critique: Critique | None = None
    revision_rounds: int = 0
    degraded: bool = False
    time_mismatch: bool = False

    # --- 附加信息 ---
    metrics: dict = field(default_factory=dict)
    quality_flags: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    generated_at: str = ""

    def meta(self) -> dict:
        """执行元信息（可观测性：上线后排查问题的唯一依据）。"""
        return {
            "intent": self.intent.intent.value if self.intent else None,
            "intent_method": self.intent.method if self.intent else None,
            "intent_confidence": self.intent.confidence if self.intent else None,
            "need_tools": self.intent.need_tools if self.intent else None,
            "capabilities": self.intent.capabilities if self.intent else [],
            "route": self.intent.route if self.intent else None,
            "tool_calls": self.loop.tool_calls if self.loop else [],
            "rounds": self.loop.rounds if self.loop else 0,
            "tool_errors": self.loop.tool_errors if self.loop else [],
            "degraded": self.degraded,
            "time_mismatch": self.time_mismatch,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "context_tokens": self.context_tokens,
            "critique_passed": self.critique.passed if self.critique else None,
            "revision_rounds": self.revision_rounds,
            "warnings": self.warnings,
        }


# ---------------------------------------------------------------------------
# 会话历史（多轮对话支持：用于携带前轮结论）
# ---------------------------------------------------------------------------

class SessionHistory:
    """
    会话级滚动历史。

    与 ContextBuilder 的内部滚动历史不同，这里跨**多次请求**保留结论，
    用于支撑规格里的"携带压缩后的前轮结论滚动输入"在多轮对话中的延续。
    """

    def __init__(self, max_turns: int = 5):
        self.max_turns = max_turns
        self.turns: list[dict] = []

    def add(self, task: str, conclusion: str, intent: str) -> None:
        self.turns.append({
            "task": task,
            "conclusion": conclusion[:400],
            "intent": intent,
            "at": datetime.now().strftime("%H:%M:%S"),
        })
        if len(self.turns) > self.max_turns:
            self.turns = self.turns[-self.max_turns:]

    def render(self) -> str:
        if not self.turns:
            return ""
        lines = ["【本次会话已完成的任务（供参考，避免重复劳动）】"]
        # 近详远略
        total = len(self.turns)
        for i, t in enumerate(self.turns):
            recency = total - 1 - i
            limit = 400 if recency == 0 else (150 if recency <= 2 else 60)
            lines.append(f"- [{t['intent']}] {t['task'][:60]} → {t['conclusion'][:limit]}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 引擎
# ---------------------------------------------------------------------------

class EcommerceAgent:
    """EC-Monica 主引擎。"""

    def __init__(self, max_rounds: int = 6, max_revisions: int = 1):
        self.max_rounds = max_rounds
        self.max_revisions = max_revisions

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self, task: str, focus: str | None = None,
            start: str | None = None, end: str | None = None,
            history: SessionHistory | None = None,
            on_progress=None) -> AgentResponse:
        """
        执行一次完整请求。

        参数：
            task    : 用户请求
            focus   : 额外关注点（可选）
            start/end: 指定分析区间（留空取最后一个完整自然周）
            history : 会话历史（多轮场景）
            on_progress: 进度回调，支持 `fn(msg)` 或 `fn(round_no, msg)` 两种签名
                         （由 safe_progress 自适应，回调内部异常不会影响任务）
        """
        started = time.perf_counter()
        resp = AgentResponse(generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        # 快速失败：未配置 API Key 时立刻抛出，由 API 层转成 503 指引。
        # 不这样做的话，请求会一路走到汇总阶段才失败，
        # 中途还会产生"工具调用成功、指标已算好"的假象。
        if not settings.has_api_key:
            raise LLMNotConfigured(
                "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入 Key。\n"
                "提示：不配 Key 也可以使用确定性接口"
                "（/api/metrics、/api/quality、/api/products、/api/channels）。"
            )

        def notify(msg: str):
            safe_progress(on_progress, 0, msg)

        # =============================================================
        # ① 意图识别
        # =============================================================
        intent = classify(task)
        resp.intent = intent
        notify(f"意图：{intent.intent.value}（{intent.method}，置信度 {intent.confidence:.2f}）")

        if intent.confidence < 0.5:
            resp.warnings.append(
                f"意图识别置信度较低（{intent.confidence:.2f}），已按"
                f"「{intent.intent.value}」处理。如结果不符预期请明确说明任务类型。"
            )

        # =============================================================
        # ② 条件路由：直接生成 vs 工具循环
        # =============================================================
        if not intent.need_tools:
            notify("路由：直接生成（无需工具）")
            resp.content = self._direct_generate(task, intent, history)
            resp.elapsed_ms = (time.perf_counter() - started) * 1000
            resp.critique = Critique(
                passed=True, problems=[], must_fix=[],
                verdict="直接生成任务，无需数据验证，不适用数据类评审。",
            )
            return resp

        notify(f"路由：工具循环（装配能力 {intent.capabilities}）")

        # =============================================================
        # ③ 数据预处理 + 注入 Context（在循环之前，不依赖模型自觉调用）
        # =============================================================
        ctx = ContextBuilder(role=intent.intent.value, template=intent.route or "analysis_report")
        ctx.set_task(task + (f"\n补充关注点：{focus}" if focus else ""))

        period = None
        try:
            period, metrics, quality = self._prepare_data(ctx, start, end)
        except Exception as e:                          # noqa: BLE001
            # 连基础数据都拿不到 → 直接降级，但要说清楚原因
            notify(f"基础数据准备失败：{type(e).__name__}")
            resp.warnings.append(f"基础数据准备失败：{type(e).__name__}: {e}")
            resp.degraded = True
            resp.content = self._synthesize(
                ctx, task, intent, history,
                degrade_reason=f"基础数据准备失败（{e}）",
            )["content"]
            resp.elapsed_ms = (time.perf_counter() - started) * 1000
            return resp

        resp.metrics = metrics
        resp.quality_flags = quality["flags"]
        notify(f"已注入预处理数据：样本 {metrics.get('sample_days')} 天，"
               f"质控 {len(quality['flags'])} 项")

        # ★ 时间错位检测：用户问的时间段与数据实际区间不一致时必须显式声明。
        #   这一条不能只靠 prompt 里一句泛泛的"无数据要指出" ——
        #   实测那句话强度不够，模型照样把一年前的数据当"上周"汇报，
        #   而且报告格式完全正常，用户根本看不出问题。
        #   所以检测出错位后，把警告放在 notes 的第一位（最高优先级），
        #   并在评审环节强制校验模型有没有照做。
        from app.context.builder import detect_time_mismatch
        _ps, _pe = period.split(" ~ ")[0], period.split(" ~ ")[-1]
        mismatch = detect_time_mismatch(_ps, _pe)
        resp.time_mismatch = mismatch["mismatch"]
        if mismatch["mismatch"]:
            ctx.notes.insert(0, mismatch["banner"])
            resp.warnings.append(
                "时间错位：数据库无用户所问时间段的数据，"
                "已要求模型在回答开头显式声明实际使用的区间。"
            )
            notify("检测到时间错位，已注入强制声明要求")

        # 会话历史也注入（跨请求的滚动结论）
        if history:
            h = history.render()
            if h:
                ctx.add_note(h)

        # =============================================================
        # ④ 工具循环
        # =============================================================
        loop_result = run_tool_loop(
            task=task, capabilities=intent.capabilities,
            context=ctx, max_rounds=self.max_rounds, on_progress=on_progress,
        )
        resp.loop = loop_result
        resp.degraded = loop_result.degraded
        notify(f"循环结束：{loop_result.rounds} 轮，"
               f"工具调用 {len(loop_result.tool_calls)} 次，"
               f"降级={loop_result.degraded}")

        # =============================================================
        # ⑤ 汇总生成（含 ⑥ 评审回环）
        # =============================================================
        synth = self._synthesize_with_critique(
            ctx, task, intent, metrics, quality, history,
            degrade_reason=loop_result.degrade_reason if loop_result.degraded else "",
            on_progress=on_progress,
        )
        resp.content = synth["content"]
        resp.critique = synth["critique"]
        resp.revision_rounds = synth["revisions"]
        resp.warnings.extend(synth["warnings"])

        # =============================================================
        # 收尾
        # =============================================================
        resp.context_tokens = ctx.token_stats()["estimated_tokens"]
        resp.elapsed_ms = (time.perf_counter() - started) * 1000

        if history:
            history.add(task, resp.content, intent.intent.value)

        return resp

    # ------------------------------------------------------------------
    # ③ 数据预处理
    # ------------------------------------------------------------------

    def _prepare_data(self, ctx: ContextBuilder,
                      start: str | None, end: str | None):
        """
        准备基础数据并注入 Context。

        ★ 这里体现"数据预处理"的真正含义：
          不是把原始数据丢给模型，而是**先由代码算好同比/环比**，
          再以加工后的形态注入。模型拿到的是结论性的数字，不是待计算的原料。
        """
        if not (start and end):
            start, end = latest_complete_week()
        period = f"{start} ~ {end}"

        # 基础指标 + 环比（代码计算，模型不参与）
        comparison = period_compare(start, end, exclude_special=True)
        enriched = enrich_period_compare(comparison)
        ctx.inject_metrics(enriched, title=f"核心指标（{period}，已算好同比/环比）")

        # 数据质控（代码运行，不依赖模型是否调用工具）
        qc = run_all_checks(start, end, period=comparison["current"])
        ctx.inject_data("数据质控结果（硬约束）", {
            "has_blocking": qc["has_blocking"],
            "flags": [
                {"code": f["code"], "level": f["level"], "message": f["message"]}
                for f in qc["flags"]
            ],
        })
        if qc["has_blocking"]:
            ctx.add_note(
                "⚠️ 数据存在阻断级质量问题（见质控结果）。"
                "本次不得给出「策略有效/无效」类因果结论，"
                "应优先说明数据问题并给出待核查清单。"
            )

        return period, comparison["current"], qc

    # ------------------------------------------------------------------
    # ⑤ 汇总生成 + ⑥ 评审回环
    # ------------------------------------------------------------------

    def _synthesize_with_critique(self, ctx: ContextBuilder, task: str,
                                  intent: IntentResult, metrics: dict,
                                  quality: dict, history,
                                  degrade_reason: str = "",
                                  on_progress=None) -> dict:
        """汇总生成，并在评审不通过时重新生成。"""
        content = ""
        critique: Critique | None = None
        warnings: list[str] = []
        revisions = 0
        feedback = ""

        for attempt in range(self.max_revisions + 1):
            result = self._synthesize(
                ctx, task, intent, history,
                degrade_reason=degrade_reason, feedback=feedback,
            )
            content = result["content"]

            # 评审
            critique = self._review(content, task, intent, metrics, quality, ctx)
            safe_progress(
                on_progress, attempt + 1,
                f"评审（第{attempt + 1}次）：{'通过' if critique.passed else '未通过'}",
            )
            if critique.passed or attempt >= self.max_revisions:
                revisions = attempt
                break

            revisions = attempt + 1
            feedback = (
                "【上一版输出未通过评审，请针对性修正】\n"
                + "\n".join(f"  - {p}" for p in critique.must_fix or critique.problems)
            )

        if critique and not critique.passed:
            warnings.append(
                "输出未通过评审：" + "；".join((critique.problems or ["详见评审意见"])[:2])
            )

        return {"content": content, "critique": critique,
                "revisions": revisions, "warnings": warnings}

    def _synthesize(self, ctx: ContextBuilder, task: str, intent: IntentResult,
                    history, degrade_reason: str = "", feedback: str = "") -> dict:
        """
        汇总生成最终输出。

        ★ 降级路径的处理是这里的关键：
          当工具全挂时，不报错，而是在 prompt 顶部明确声明
          "以下内容未经数据验证"，让模型和用户都清楚这份输出的可信边界。
        """
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.llm import build_llm

        context_text = ctx.build()

        # 降级声明放在最前面 —— 位置决定服从度
        prefix = ""
        if degrade_reason:
            prefix = (
                "⚠️【重要：本次输出未能完成数据验证】\n"
                f"原因：{degrade_reason}\n"
                "因此：\n"
                "- 你不掌握真实数据，**不得编造任何具体数字**；\n"
                "- 输出必须以「本次未能获取到完整数据」开头，说明哪些信息缺失；\n"
                "- 可以给出基于通用经验的分析框架与建议，但必须标注为「待数据验证」；\n"
                "- 明确列出需要补充哪些数据才能给出确定结论。\n"
                + "=" * 60 + "\n\n"
            )

        if feedback:
            prefix += feedback + "\n" + "=" * 60 + "\n\n"

        if history and history.render():
            prefix += history.render() + "\n" + "=" * 60 + "\n\n"

        try:
            resp = build_llm(temperature=0.3).invoke([
                SystemMessage(content=context_text),
                HumanMessage(content=(
                    prefix
                    + f"【最终任务】\n{task}\n\n"
                    + "请基于上面提供的数据与已完成的工作，"
                      "按模板要求产出最终输出。"
                      + ("若存在阻断级数据质量问题，必须先说明。"
                         if ctx.notes else "")
                )),
            ])
            return {"content": (resp.content or "").strip()}
        except LLMNotConfigured:
            # 配置错误不兜底：必须向上抛，让 API 层返回明确的 503 指引。
            # 否则用户拿到的是"一份看似正常的报告"，而实际上模型根本没被调用 —— 
            # 这比直接报错危险得多。
            raise
        except Exception as err:                        # noqa: BLE001
            # 最终生成失败：仍要给出可用的东西，而不是抛异常
            return {"content": self._emergency_output(ctx, task, err)}

    def _emergency_output(self, ctx: ContextBuilder, task: str, err: Exception) -> str:
        """
        最后兜底：连 LLM 都调不通时，输出纯确定性内容。

        此时至少把**已经算好的指标**给出来 —— 这些数字是可信的，
        比一个空白的错误页面有价值得多。
        """
        lines = [
            "# 数据摘要（LLM 生成失败，以下为系统直接计算的确定性结果）",
            "",
            f"- 任务：{task}",
            f"- 失败原因：{type(err).__name__}: {err}",
            "",
            "## 已计算的核心指标",
            "",
            "```",
            compress_injected_data(ctx.metrics_block.get("data", {})),
            "```",
            "",
        ]
        for title, data in ctx.data_blocks:
            lines += [f"## {title}", "", "```",
                      compress_injected_data(data), "```", ""]
        if ctx.rounds:
            lines += ["## 已完成的工具调用结论", "", ctx.render_history(), ""]
        lines += [
            "---",
            "*以上内容由系统确定性计算得出，未经 LLM 加工，也未生成分析结论。*",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # ⑥ 评审
    # ------------------------------------------------------------------

    def _review(self, content: str, task: str, intent: IntentResult,
                metrics: dict, quality: dict,
                ctx: ContextBuilder | None = None) -> Critique:
        """
        评审：确定性规则先行，LLM 语义审查补充。

        注意：这里的确定性规则主要检查**质量纪律**——
        数据有阻断问题时，输出里不能出现因果结论；
        样本不足时，不能出现"趋势"表述；
        时间错位时必须显式声明实际使用的区间。
        """
        problems: list[str] = []
        has_blocking = quality.get("has_blocking", False)
        sample_days = metrics.get("sample_days", 0)
        min_days = settings.MIN_CONFIDENCE_SAMPLE_DAYS

        # --- 确定性检查 0：时间错位必须被显式声明 ---
        #   这是最容易被忽略、也最会误导用户的一条：
        #   报告格式完全正常，但对应的是一年前的行情。
        #   所以这里用硬规则校验，而不是指望模型自觉。
        if ctx and any("时间错位警告" in n for n in ctx.notes):
            head = content[:400]
            declared = any(
                k in head for k in ("注意", "说明", "数据范围", "无数据",
                                    "数据止于", "最新区间", "错位")
            )
            # 更硬的判据：必须出现数据实际区间的日期或"无数据"字样
            has_range = any(
                k in content for k in ("数据止于", "无该时间段", "超出数据范围",
                                       "最新区间", "无数据")
            )
            if not (declared and has_range):
                problems.append(
                    "存在时间错位（用户问的时间段数据库中无数据），"
                    "但输出开头未明确声明实际使用的数据区间。"
                    "必须让用户知道这个答案对应的不是他问的时间段。"
                )

        # --- 确定性检查 1：阻断纪律（词汇 + 结构双重要求）---
        #   ★ 只查"没出现禁用词"是不够的：一份空洞的、什么都没说的输出
        #     同样能通过词汇检查，但对用户毫无价值。
        #     阻断纪律的完整要求是「事实陈述 + 待核查清单 + 修复优先」三段结构，
        #     所以这里既要查禁用词，也要查结构是否齐备。
        if has_blocking:
            import re
            causal_patterns = [
                r"策略(有效|成功|见效|奏效|失败|无效|不佳)",
                r"运营(做得好|到位|有效)",
                r"投放(效果很好|很成功|效果不佳)",
                r"(证明|表明|说明).{0,12}(策略|运营|投放).{0,6}(有效|成功|失败|无效)",
                r"归因于.{0,10}(策略|运营|投放)",
            ]
            for pat in causal_patterns:
                m = re.search(pat, content)
                if m:
                    problems.append(
                        f"[阻断纪律] 数据存在阻断级质量问题，但输出中出现了因果结论"
                        f"「{m.group(0)}」。只允许事实陈述与待核查清单，"
                        f"不得使用策略有效性判断。"
                    )
                    break

            # --- 结构要求 ①：必须声明数据不可用于因果归因 ---
            if not any(k in content for k in
                       ("不能给出因果", "不可用于因果", "不做因果", "无法归因",
                        "阻断级", "不能判定", "不应据此")):
                problems.append(
                    "[阻断纪律] 未声明「本期数据不可用于因果归因」。"
                    "存在阻断级问题时必须明确告知读者结论的适用范围。"
                )

            # --- 结构要求 ②：必须给出待核查清单 ---
            if not any(k in content for k in
                       ("待核查", "核查清单", "需确认", "待确认", "待排查", "建议核查")):
                problems.append(
                    "[阻断纪律] 缺少「待核查清单」。"
                    "在阻断级问题下，正确输出是列出需人工确认的事项，"
                    "而不是只给一组数据。"
                )

            # --- 结构要求 ③：error 级质控问题必须被提及 ---
            #   ★ 只对 error 级做强制要求，不对 warning 级。
            #     实测踩过的坑：最初把 warning 级（投放边际递减）也纳入强制提及，
            #     结果一份结构完整、只聚焦于真正阻断问题（缺货）的合格报告
            #     因为"没提投放"而被判不通过 —— 评审过严会让人反复重做，
            #     最后为了讨好评审而在报告里堆砌无关内容，反而降低质量。
            #     **阻断纪律应该只服务于"哪些问题必须先讲清楚"，
            #       而不是强制报告覆盖所有被检测到的现象。**
            flag_topics = {
                "STOCKOUT_FOUND": ("缺货", "断货", "库存"),
                "PRICE_CHANGE_DETECTED": ("改价", "涨价", "价格"),
                "SPECIAL_PERIOD_INCLUDED": ("大促", "特殊日期", "618"),
                "CHANNEL_MIX_SHIFT": ("流量结构", "渠道结构", "占比"),
                "NO_DATA": ("无数据", "没有数据"),
            }
            for flag in quality.get("flags", []):
                # 只有 error 级才有强制提及的效力
                if flag.get("level") != "error":
                    continue
                keywords = flag_topics.get(flag.get("code"))
                if keywords and not any(k in content for k in keywords):
                    problems.append(
                        f"[阻断纪律] 质控以 error 级报告了 {flag['code']}，"
                        f"但输出中完全未提及（未出现任一关键词：{keywords}）—— "
                        f"阻断级问题必须优先呈现，不能被绕过。"
                    )

            # --- 结构要求 ④：必须说明先修复阻断问题 ---
            if not any(k in content for k in
                       ("先", "优先", "修复", "补货", "处理")):
                problems.append(
                    "[阻断纪律] 未指出应先解决阻断级数据问题。"
                    "在数据不可信的前提下谈论经营调整，方向可能是错的。"
                )

        # --- 确定性检查 2：样本不足时不得使用趋势表述 ---
        if sample_days < min_days:
            for w in ("趋势", "持续下滑", "持续上升", "稳步"):
                if w in content:
                    problems.append(
                        f"样本仅 {sample_days} 天（<{min_days} 天），"
                        f"输出中不应使用「{w}」这类趋势表述。"
                    )
                    break

        # --- LLM 语义审查 ---
        semantic: Critique | None = None
        try:
            from app.agents.critic import llm_review
            semantic = llm_review(
                period=metrics.get("period", "?"),
                metrics=metrics,
                quality_text=compress_injected_data(
                    quality.get("text", "") if isinstance(quality.get("text"), str)
                    else {"flags": quality.get("flags", [])}),
                insights=[Insight(
                    metric="report",
                    description=content[:1200],
                    current_value=0.0,
                    evidence=["本次输出正文"],
                    confidence="medium",
                )],
                actions=[],
            )
        except Exception as e:                          # noqa: BLE001
            semantic = Critique(
                passed=True, problems=[], must_fix=[],
                verdict=f"[提示] LLM 语义评审未执行（{type(e).__name__}），"
                        f"本次仅通过确定性规则校验。",
            )

        all_problems = problems + semantic.problems
        return Critique(
            passed=(not problems) and semantic.passed,
            problems=all_problems,
            must_fix=list(dict.fromkeys(problems + semantic.must_fix)),
            verdict=semantic.verdict,
        )

    # ------------------------------------------------------------------
    # 直接生成路径
    # ------------------------------------------------------------------

    def _direct_generate(self, task: str, intent: IntentResult,
                         history) -> str:
        """无需工具的直接生成（文案、草稿等）。"""
        from langchain_core.messages import HumanMessage, SystemMessage

        from app.context.templates import BUSINESS_RULES, ROLE_PROMPTS, TEMPLATES
        from app.llm import build_llm

        system = "\n\n".join([
            ROLE_PROMPTS.get(intent.intent.value, ROLE_PROMPTS["copywriting"]),
            BUSINESS_RULES,
            TEMPLATES.get("copywriting", ""),
        ])
        user = task
        if history and history.render():
            user = history.render() + "\n\n" + task

        try:
            resp = build_llm(temperature=0.7).invoke([
                SystemMessage(content=system),
                HumanMessage(content=user),
            ])
            return (resp.content or "").strip()
        except Exception as e:                          # noqa: BLE001
            return (
                f"# 生成失败\n\n"
                f"任务：{task}\n\n"
                f"原因：{type(e).__name__}: {e}\n\n"
                f"请检查 API Key 配置与网络连接。"
            )


# 默认引擎实例
agent = EcommerceAgent()
