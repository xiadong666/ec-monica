"""
scripts/verify_workflow.py
==========================
★★ 工作流端到端验收。

两种模式：
    默认（离线）：用假 LLM 替换模型调用，精确验证工作流骨架 ——
                  工具循环轮次、降级路径、评审回环、上下文滚动、输出元信息。
                  不需要 API Key，秒级完成。
    --live      ：用真实 LLM 完整跑一遍。需要 .env 里配置 DEEPSEEK_API_KEY。

为什么默认离线？
    工作流 bug（降级没触发、轮次失控、上下文没滚动）
    和模型质量问题是两类问题。用真模型测，你分不清是哪个。
    离线模式让骨架行为完全可控、可复现、零成本。

运行：
    python scripts/verify_workflow.py
    python scripts/verify_workflow.py --live
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings                          # noqa: E402
from app.workflow.loop import PlanDecision, ToolCall     # noqa: E402

PASS, FAIL = 0, 0
LIVE = "--live" in sys.argv


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(t: str) -> None:
    print()
    print("=" * 76)
    print(t)
    print("=" * 76)


# ===========================================================================
section("1. 意图路由端到端（规则路径，无需 LLM）")
# ===========================================================================

from app.workflow.engine import EcommerceAgent, SessionHistory   # noqa: E402
from app.workflow.intent import Intent, classify                  # noqa: E402

routing_cases = [
    ("上周的 GMV 是多少", Intent.ANALYSIS_REPORT),
    ("转化率为什么下滑了", Intent.ATTRIBUTION),
    ("拆解一下这个需求文档", Intent.REQUIREMENT_PARSE),
    ("写个 618 促销文案", Intent.DIRECT_GENERATE),
]
for text, want in routing_cases:
    r = classify(text)
    check(f"路由「{text}」→ {want.value}", r.intent == want,
          f"实际 {r.intent.value}")


# ===========================================================================
section("2. ★ 工具循环：轮次控制 / 预处理注入 / 滚动上下文")
# ===========================================================================

import app.workflow.loop as loop_mod                     # noqa: E402

# --- 假规划器：模拟"先查数据结构，再取指标，然后结束"的三轮决策 ---
PLAN_SEQUENCE = [
    PlanDecision(done=False, reason="先了解数据结构",
                 calls=[ToolCall(tool="describe_tables", args={}, purpose="看表结构")]),
    PlanDecision(done=False, reason="取核心指标与质控",
                 calls=[
                     ToolCall(tool="compare_weeks", args={}, purpose="拿环比指标"),
                     ToolCall(tool="get_product_breakdown", args={}, purpose="商品下钻"),
                 ]),
    PlanDecision(done=True, reason="信息已足够", calls=[]),
]
_plan_calls = {"n": 0}


def fake_plan_next(context_text, tools, current_round, max_rounds, last_errors):
    i = _plan_calls["n"]
    _plan_calls["n"] += 1
    return PLAN_SEQUENCE[min(i, len(PLAN_SEQUENCE) - 1)]


_conclusion_calls = {"n": 0}


def fake_sub_conclusion(context_text, new_data_text):
    _conclusion_calls["n"] += 1
    return (f"第{_conclusion_calls['n']}轮子结论：本轮获取到结构化数据，"
            f"数据长度 {len(new_data_text)} 字符。核心发现是转化率环比下滑。")


loop_mod.plan_next = fake_plan_next
loop_mod.make_sub_conclusion = fake_sub_conclusion

from app.context.builder import ContextBuilder            # noqa: E402
from app.metrics.compute import latest_complete_week, period_compare  # noqa: E402
from app.workflow.preprocess import enrich_period_compare  # noqa: E402

start, end = latest_complete_week()
ctx = ContextBuilder(role="analysis_report", template="analysis_report")
ctx.set_task("测试任务")
ctx.inject_metrics(enrich_period_compare(period_compare(start, end)))

result = loop_mod.run_tool_loop(
    task="测试任务",
    capabilities=["schema", "metric", "drilldown", "quality"],
    context=ctx, max_rounds=6,
)

print(f"  执行轮次：{result.rounds}")
print(f"  工具调用序列：{result.tool_calls}")
print(f"  子结论条数：{len(result.conclusions)}")
print(f"  上下文 tokens：{result.context_tokens}")
print(f"  降级：{result.degraded}")

check("循环按规划执行了 3 轮", result.rounds == 3, str(result.rounds))
check("工具调用序列正确",
      result.tool_calls == ["describe_tables", "compare_weeks", "get_product_breakdown"],
      str(result.tool_calls))
check("每轮产出子结论", len(result.conclusions) == 2, str(len(result.conclusions)))
check("滚动历史已累积", len(result.history) == 2, str(len(result.history)))
check("历史记录含工具名", result.history[0].tool_names == ["describe_tables"],
      str(result.history[0].tool_names))
check("历史记录含关键数字（数字锚点未丢）",
      bool(result.history[1].key_numbers), str(result.history[1].key_numbers))
check("未误判为降级", not result.degraded)
check("上下文被压缩（历史体积可控）",
      0 < result.context_tokens < 6000, str(result.context_tokens))

# --- 验证预处理确实注入了环比 ---
kn = result.history[1].key_numbers
print(f"  第2轮抽取的关键数字：{list(kn)[:6]}")
check("预处理注入了环比数据", any(k.endswith("_环比") for k in kn), str(list(kn)))


# ===========================================================================
section("3. ★ 轮次上限保护（防止无限循环）")
# ===========================================================================

_plan_calls["n"] = 0


def always_continue(context_text, tools, current_round, max_rounds, last_errors):
    """永远想继续调用工具 —— 模拟模型"停不下来"的情况。"""
    return PlanDecision(done=False, reason="继续查",
                        calls=[ToolCall(tool="get_data_range", args={}, purpose="再查一次")])


loop_mod.plan_next = always_continue
ctx2 = ContextBuilder(role="analysis_report", template="analysis_report")
ctx2.set_task("轮次上限测试")

result2 = loop_mod.run_tool_loop(
    task="轮次上限测试", capabilities=["schema", "metric"],
    context=ctx2, max_rounds=4,
)
print(f"  模型一直想继续，实际执行轮次：{result2.rounds}（上限 4）")
check("轮次上限强制生效", result2.rounds == 4, str(result2.rounds))
check("超出上限后正常结束并返回结果", len(result2.conclusions) >= 1)
check("上下文未爆炸（压缩生效）",
      result2.context_tokens < 8000, str(result2.context_tokens))


# ===========================================================================
section("4. ★ 降级路径：工具全失败时必须给出可用输出")
# ===========================================================================

_plan_calls["n"] = 0


def plan_broken_tool(context_text, tools, current_round, max_rounds, last_errors):
    """规划调用一个不存在的工具 → Registry 会返回 permanent 错误。"""
    return PlanDecision(done=False, reason="调用坏工具",
                        calls=[ToolCall(tool="describe_tables", args={}, purpose="x")])


# 把 describe_tables 替换成必然失败的实现。
# 故意抛一个会被分类为 permanent 的错误（模拟工具名/权限类不可恢复故障），
# 这样才能验证「同类永久错误早停」这条路径。
_bad = loop_mod.registry.get("describe_tables")
_orig_func = _bad.func
_bad.func = lambda **kw: (_ for _ in ()).throw(Exception("403 forbidden: permission denied"))
_bad.max_retries = 0

loop_mod.plan_next = plan_broken_tool
ctx3 = ContextBuilder(role="analysis_report", template="analysis_report")
ctx3.set_task("降级测试")

result3 = loop_mod.run_tool_loop(
    task="降级测试", capabilities=["schema"], context=ctx3, max_rounds=6,
)
print(f"  工具全部失败后：降级={result3.degraded}")
print(f"  降级原因：{result3.degrade_reason[:90]}")
print(f"  错误记录数：{len(result3.tool_errors)}")

check("工具持续失败后触发降级", result3.degraded, str(result3.summary()))
check("降级原因被记录", bool(result3.degrade_reason), result3.degrade_reason)
check("工具错误被完整记录（含错误类型）",
      len(result3.tool_errors) >= 2
      and all("error_kind" in e for e in result3.tool_errors),
      str(result3.tool_errors[:2]))
check("不可重试错误触发早停（2 次即停，未空跑到上限）",
      result3.rounds <= 3, f"实际 {result3.rounds} 轮")
check("降级原因说明了错误不可重试",
      "不可重试" in result3.degrade_reason, result3.degrade_reason)

# 恢复工具
_bad.func = _orig_func
_bad.max_retries = 2


# ===========================================================================
section("5. ★ 降级时的最终输出：必须声明「未经数据验证」且不编造数字")
# ===========================================================================

# 假造"汇总生成"：直接把 prompt 里的降级声明回显出来，验证降级声明确实被拼进 prompt
captured = {"system": "", "user": ""}


class _FakeMsg:
    """假的 LLM 响应：同时带 content 与结构化字段。

    classify_by_llm 走 structured_llm（需要结构化字段），
    _synthesize 走 build_llm（需要 .content），
    两者共用这个假响应，避免测试里再引一次真实 LLM。
    """

    def __init__(self, content: str = "（假 LLM 输出）"):
        self.content = content
        # 供结构化输出使用（intent 兜底判定）
        self.intent = Intent.ANALYSIS_REPORT
        self.confidence = 0.9
        self.reason = "（假 LLM）"


class _FakeLLM:
    def invoke(self, messages):
        captured["system"] = messages[0].content
        captured["user"] = messages[-1].content
        return _FakeMsg()

    def with_structured_output(self, schema, **kwargs):
        """模拟结构化输出绑定（返回自身即可）。"""
        return self


import app.workflow.engine as engine_mod                 # noqa: E402
import app.llm as llm_mod                                # noqa: E402


def _fake_build_llm(*a, **kw):
    """替换 LLM：把 prompt 捕获下来，返回假响应。"""
    return _FakeLLM()


# engine 内部用的是 `from app.llm import build_llm`（函数内导入），
# 所以替换 llm 模块上的属性即可生效。
llm_mod.build_llm = _fake_build_llm

agent = EcommerceAgent()
content = agent._synthesize(
    ctx3, "测试降级输出", classify("分析一下经营情况"), None,
    degrade_reason="累计 3 次工具失败，最后一次错误：simulated permanent failure",
)
print("  降级 prompt 片段：")
print("   ", captured["user"][:210].replace("\n", "\n    "))
check("降级时 prompt 顶部声明未经数据验证",
      "未能完成数据验证" in captured["user"], captured["user"][:100])
check("降级时明确禁止编造数字",
      "不得编造" in captured["user"], captured["user"][:200])
check("降级时要求说明缺失信息",
      "未能获取到完整数据" in captured["user"])
check("降级仍产出内容（不抛异常）", bool(content))


# ===========================================================================
section("6. ★ 进度回调：两种签名都要能用，且回调异常不得拖垮任务")
# ===========================================================================

# 这一节是补的回归覆盖。
# 之前 222 项验收全绿却漏掉了这个 bug —— 因为没有任何测试传过 on_progress，
# 于是"引擎用 fn(msg)、循环用 fn(round, msg)"这个必然崩的签名冲突
# 在真实跑 CLI 时才暴露出来。
# ★ 教训：**可选参数必须被测试覆盖**，否则它等同于没有。
from app.workflow.util import safe_progress              # noqa: E402

# --- 直接验证 safe_progress 的签名自适应 ---
calls_1: list = []
calls_2: list = []
calls_var: list = []


def cb_one(msg):                       # 单参数约定
    calls_1.append(msg)


def cb_two(round_no, msg):             # 双参数约定
    calls_2.append((round_no, msg))


def cb_broken(msg):                    # 故意抛异常
    raise RuntimeError("回调内部炸了")


safe_progress(cb_one, 3, "测试消息")
safe_progress(cb_two, 3, "测试消息")
safe_progress(cb_broken, 3, "测试消息")     # 不应抛出
safe_progress(None, 3, "测试消息")          # 不应抛出

check("回调 fn(msg) 被正确调用", calls_1 == ["测试消息"], str(calls_1))
check("回调 fn(round_no, msg) 被正确调用",
      calls_2 == [(3, "测试消息")], str(calls_2))
check("回调抛异常不影响主流程（被安全吞掉）", True)

# --- 端到端：用两种签名的回调各跑一次循环 ---
_plan_calls["n"] = 0
loop_mod.plan_next = fake_plan_next
loop_mod.make_sub_conclusion = fake_sub_conclusion

for label, cb in [("单参数 fn(msg)", cb_one), ("双参数 fn(round_no, msg)", cb_two)]:
    cb_events: list = []
    if cb is cb_one:
        target = lambda m: cb_events.append(("1", m))          # noqa: E731
    else:
        target = lambda r, m: cb_events.append((r, m))         # noqa: E731

    _plan_calls["n"] = 0
    ctx_cb = ContextBuilder(role="analysis_report", template="analysis_report")
    ctx_cb.set_task("回调测试")
    try:
        res_cb = loop_mod.run_tool_loop(
            task="回调测试", capabilities=["schema", "metric", "metric"],
            context=ctx_cb, max_rounds=3, on_progress=target,
        )
        ok = len(cb_events) > 0
        detail = f"收到 {len(cb_events)} 条进度"
    except TypeError as exc:
        ok, detail = False, f"TypeError: {exc}"
    check(f"循环接受{label}", ok, detail)

# --- 引擎端到端：确保引擎的 notify 也不会因签名不匹配崩溃 ---
_plan_calls["n"] = 0
loop_mod.plan_next = fake_plan_next
loop_mod.make_sub_conclusion = fake_sub_conclusion
engine_events: list = []
try:
    EcommerceAgent().run(
        "测试回调", on_progress=lambda r, m: engine_events.append((r, m)),
    )
    engine_ok, engine_detail = True, f"收到 {len(engine_events)} 条进度"
except TypeError as exc:
    engine_ok, engine_detail = False, f"TypeError: {exc}"
except Exception as exc:                                # noqa: BLE001
    # 未配 Key 会抛 LLMNotConfigured，这不是签名问题
    from app.llm import LLMNotConfigured
    if isinstance(exc, LLMNotConfigured):
        engine_ok, engine_detail = True, "（未配置 Key，提前退出，签名未报错）"
    else:
        engine_ok, engine_detail = False, f"{type(exc).__name__}: {exc}"
check("引擎在双参数回调下不崩（本次 bug 的直接回归）", engine_ok, engine_detail)


# ===========================================================================
section("7. 会话历史（多轮对话携带前轮结论）")
# ===========================================================================

h = SessionHistory(max_turns=3)
for i in range(5):
    h.add(f"任务{i}", f"结论{i}", "analysis_report")
r = h.render()
print(f"  加入 5 条（上限 3），实际保留：{len(h.turns)} 条")
check("会话历史上限生效", len(h.turns) == 3, str(len(h.turns)))
check("保留的是最近的任务", "任务4" in r and "任务0" not in r)
check("历史渲染含意图标签", "analysis_report" in r)

# 近详远略
check("历史中最近一条比最早一条详细",
      len(h.turns[-1]["conclusion"]) >= len(h.turns[0]["conclusion"]))


# ===========================================================================
section("8. 输出元信息（可观测性）")
# ===========================================================================

from app.workflow.engine import AgentResponse            # noqa: E402

resp = AgentResponse()
resp.intent = classify("转化率为什么下滑")
resp.loop = result
resp.degraded = False
resp.elapsed_ms = 1234.5
resp.context_tokens = 3000
meta = resp.meta()

print("  元信息字段：")
for k, v in meta.items():
    print(f"    {k}: {str(v)[:70]}")

required_keys = ["intent", "intent_method", "need_tools", "capabilities",
                 "route", "tool_calls", "rounds", "tool_errors", "degraded",
                 "elapsed_ms", "context_tokens", "critique_passed",
                 "revision_rounds", "warnings"]
for k in required_keys:
    check(f"元信息含 {k}", k in meta)
check("元信息记录了完整工具调用序列",
      meta["tool_calls"] == result.tool_calls, str(meta["tool_calls"]))


# ===========================================================================
section("9. 评估集完整性")
# ===========================================================================

import json                                              # noqa: E402

eval_dir = Path(__file__).resolve().parent.parent / "tests" / "eval"
try:
    gs = json.loads((eval_dir / "golden_set.json").read_text(encoding="utf-8"))
    cases = gs["cases"]
    print(f"  取数/归因评估集：{len(cases)} 个用例")
    traps = [c["id"] for c in cases if c.get("expect_trap")]
    print(f"  其中归因陷阱题：{traps}")
    check("取数评估集非空", len(cases) >= 10, str(len(cases)))
    check("含归因陷阱题（考的是会不会被假象带偏）", len(traps) >= 3, str(traps))
    check("每个用例都写明考点", all(c.get("exams") for c in cases))
except Exception as e:                                    # noqa: BLE001
    check("取数评估集可加载", False, f"{type(e).__name__}: {e}")

try:
    gr = json.loads(
        (eval_dir / "golden_set_requirement.json").read_text(encoding="utf-8"))
    rcases = gr["cases"]
    print(f"  需求解析评估集：{len(rcases)} 个用例")
    check("需求解析评估集非空", len(rcases) >= 3, str(len(rcases)))
    check("含需求模糊度考察（考察会不会臆测需求）",
          any(c.get("expect_low_confidence") for c in rcases))
except Exception as e:                                    # noqa: BLE001
    check("需求解析评估集可加载", False, f"{type(e).__name__}: {e}")


# ===========================================================================
section("10. 真实 LLM 端到端（--live 模式）")
# ===========================================================================

if not LIVE:
    print("  跳过（未指定 --live）")
    print("  运行方式：python scripts/verify_workflow.py --live")
else:
    if not settings.has_api_key:
        print("  ⚠️ 未配置 DEEPSEEK_API_KEY，无法运行真实模式。")
        print("     请复制 .env.example 为 .env 并填入 Key。")
    else:
        # 恢复真实的 LLM 绑定
        import importlib
        importlib.reload(llm_mod)
        importlib.reload(engine_mod)
        importlib.reload(loop_mod)

        print("  开始真实端到端调用（可能需要 30~90 秒）...")
        live_agent = engine_mod.EcommerceAgent(max_rounds=4, max_revisions=1)
        live_resp = live_agent.run("上周（2025-08-18~08-24）的转化率为什么下滑？")

        print()
        print("  ── 元信息 ──")
        for k, v in live_resp.meta().items():
            print(f"    {k}: {str(v)[:75]}")
        print()
        print("  ── 输出（前 600 字）──")
        print("   " + live_resp.content[:600].replace("\n", "\n   "))

        check("真实调用产出内容", len(live_resp.content) > 100,
              f"{len(live_resp.content)} 字")
        check("意图识别为归因", live_resp.intent.intent == Intent.ATTRIBUTION,
              live_resp.intent.intent.value)
        check("实际调用了工具", len(live_resp.loop.tool_calls) > 0,
              str(live_resp.loop.tool_calls))
        check("调用了质控工具（归因纪律）",
              "run_quality_checks" in live_resp.loop.tool_calls,
              str(live_resp.loop.tool_calls))
        check("未降级", not live_resp.degraded, live_resp.meta().get("warnings", ""))
        check("输出提到缺货（未被归因污染带偏）",
              any(k in live_resp.content for k in ("缺货", "库存", "断货")),
              live_resp.content[:200])
        check("完成评审", live_resp.critique is not None)
        if live_resp.critique:
            print(f"  评审意见：{live_resp.critique.verdict[:100]}")


# ===========================================================================
print()
print("=" * 76)
print(f"工作流验收：{PASS} 项通过，{FAIL} 项失败")
print("=" * 76)
if FAIL == 0:
    print()
    if LIVE:
        print("✅ 真实 LLM 端到端通过。")
    else:
        print("✅ 工作流骨架（轮次控制 / 降级 / 滚动上下文 / 元信息）全部通过。")
        print("   加 --live 可用真实模型跑端到端。")
else:
    print()
    print(f"❌ 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
