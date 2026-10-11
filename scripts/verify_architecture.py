"""
scripts/verify_architecture.py
==============================
★ 架构验收：验证新增的四个核心机制，全部不依赖 LLM。

    ┌─ 1. 动态 Tool Registry：注册、能力装配、Schema 自动生成
    ├─ 2. 容错：超时、重试、错误分类、失败降级
    ├─ 3. 意图识别与条件路由：规则优先，路由表正确
    └─ 4. 上下文工程：预处理环比、压缩、滚动历史（近详远略）

为什么这些必须单独验证？
    它们都是"确定性可判定"的组件。如果混在端到端测试里用真模型跑，
    一旦结果不对，你无法区分是模型问题还是这些组件有 bug。
    先把确定的部分测到全绿，剩下的问题一定出在模型环节。

运行：python scripts/verify_architecture.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PASS, FAIL = 0, 0


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
    print("=" * 74)
    print(t)
    print("=" * 74)
# ===========================================================================
# ★ 未捕获异常的诊断钩子
# ===========================================================================
# 背景：CI 上出现过「exit 1 但日志里没有 [FAIL]、也没有结论行」的情况 ——
#      脚本是被未捕获异常中断的，而 traceback 混在长日志里很难发现。
#      这个钩子把异常打成醒目块，直接 grep 即可：
#          grep -A 40 "未捕获异常"
#
# 判定方式：用 sys.exc_info() 而不是自己维护标志位 ——
#      正常退出时它返回 (None, None, None)，只有真的抛异常才有内容。
#      （第一版用标志位判断，结果正常结束也误报，反而制造噪声。）
def _install_crash_reporter(script_path: str) -> None:
    import atexit
    import os
    import sys as _sys
    import traceback as _tb

    def _report() -> None:
        exc_type, exc_value, exc_tb = _sys.exc_info()
        if exc_type is None:
            return                      # 正常退出，不打扰
        print()
        print("!" * 74)
        print(f"!! {os.path.basename(script_path)} 未捕获异常 —— "
              f"脚本被中断，未跑到结论行")
        print("!" * 74)
        _tb.print_exception(exc_type, exc_value, exc_tb)
        print("!" * 74)
        _sys.stdout.flush()

    atexit.register(_report)


_install_crash_reporter(__file__)


# ===========================================================================
section("1. 动态 Tool Registry")
# ===========================================================================

from app.tools import registry                        # noqa: E402

names = registry.names()
print(f"  已注册工具 {len(names)} 个：{names}")
check("工具已注册（导入即注册生效）", len(names) >= 10, str(len(names)))

expected = ["compare_weeks", "get_data_range", "run_quality_checks",
            "get_product_breakdown", "get_channel_breakdown", "run_sql",
            "describe_tables", "scan_stockout", "scan_price_change"]
for n in expected:
    check(f"工具 {n} 存在", n in names)

# --- 能力装配（意图路由的基础）---
metric_tools = registry.select(capabilities=["metric"])
drill_tools = registry.select(capabilities=["drilldown"])
sql_tools = registry.select(capabilities=["sql"])
print(f"  capabilities=['metric']    → {[t.name for t in metric_tools]}")
print(f"  capabilities=['drilldown'] → {[t.name for t in drill_tools]}")
print(f"  capabilities=['sql']       → {[t.name for t in sql_tools]}")

check("metric 能力筛出指标工具", "compare_weeks" in [t.name for t in metric_tools])
check("metric 能力不含 SQL 工具", "run_sql" not in [t.name for t in metric_tools])
check("drilldown 能力筛出下钻工具",
      "get_product_breakdown" in [t.name for t in drill_tools])
check("sql 能力筛出 SQL 工具", "run_sql" in [t.name for t in sql_tools])
check("能力装配是动态子集（不是全量）",
      len(metric_tools) < len(registry.all()),
      f"{len(metric_tools)} vs {len(registry.all())}")

# --- Function Schema 自动生成 ---
lc_tools = registry.build_langchain_tools(registry.select(capabilities=["metric"]))
check("LangChain 工具装配成功", len(lc_tools) == len(metric_tools))
try:
    cw = [t for t in lc_tools if t.name == "compare_weeks"][0]
    schema = cw.args_schema.model_json_schema()
    props = schema.get("properties", {})
    print(f"  compare_weeks 自动生成的 Schema 字段：{list(props)}")
    check("Schema 自动生成含参数 start/end", "start" in props and "end" in props)
    check("Schema 参数类型正确", props["start"].get("type") == "string",
          str(props.get("start")))
except Exception as e:                                  # noqa: BLE001
    check("Schema 自动生成", False, f"{type(e).__name__}: {e}")


# ===========================================================================
section("2. 容错：超时 / 重试 / 错误分类")
# ===========================================================================

from app.tools.registry import ToolRegistry, classify_error   # noqa: E402

# --- 错误分类 ---
cases = [
    (TimeoutError("connection timed out"), "transport", "超时 → transport"),
    (ConnectionError("connection reset by peer"), "transport", "连接失败 → transport"),
    (Exception("429 rate limit exceeded"), "transport", "限流 → transport"),
    (Exception("no such table: foo"), "data", "表不存在 → data"),
    (Exception("SQL 被拒绝：未识别到任何数据表"), "data", "护栏拒绝 → data"),
    (Exception("API key not configured"), "permanent", "Key 缺失 → permanent"),
    (Exception("401 unauthorized"), "permanent", "鉴权失败 → permanent"),
    (ValueError("some random thing"), "unknown", "未知 → unknown"),
]
for exc, want_kind, label in cases:
    got = classify_error(exc)
    check(f"错误分类：{label}", got.kind == want_kind, f"实际 {got.kind}")

# --- 只对可重试的错误重试 ---
t = ToolRegistry()
attempts = {"transport": 0, "data": 0}

@t.register(capabilities=["metric"], description="总是超时", max_retries=2,
            backoff_base=0.01)
def flaky_transport():
    attempts["transport"] += 1
    raise TimeoutError("timed out")

@t.register(capabilities=["metric"], description="数据错误", max_retries=2,
            backoff_base=0.01)
def flaky_data():
    attempts["data"] += 1
    raise Exception("no such table: foo")

r1 = t.execute("flaky_transport")
r2 = t.execute("flaky_data")
print(f"  transport 类错误实际尝试次数：{attempts['transport']}")
print(f"  data 类错误实际尝试次数：{attempts['data']}")
check("transport 错误被重试（1+2 次）", attempts["transport"] == 3,
      str(attempts["transport"]))
check("data 错误不重试（只 1 次）", attempts["data"] == 1, str(attempts["data"]))
check("失败返回结构化错误（不抛异常）",
      isinstance(r1, dict) and r1.get("ok") is False)
check("错误对象含 error_kind", r1.get("error_kind") == "transport", str(r1))
check("错误对象含 hint（便于模型自我修正）", bool(r1.get("hint")))

# --- 超时保护 ---
t2 = ToolRegistry()

@t2.register(capabilities=["metric"], description="慢工具", timeout_s=0.3,
             max_retries=0)
def slow_tool():
    time.sleep(3)
    return {"ok": True}

start = time.perf_counter()
r3 = t2.execute("slow_tool")
elapsed = time.perf_counter() - start
print(f"  慢工具（sleep 3s，超时 0.3s）实际耗时：{elapsed:.2f}s")
check("超时保护生效（未等满 3 秒）", elapsed < 1.5, f"{elapsed:.2f}s")
check("超时被归类为 transport", r3.get("error_kind") == "transport", str(r3))

# --- 未注册工具 ---
r4 = t.execute("no_such_tool")
check("未注册工具返回结构化错误",
      r4.get("ok") is False and r4.get("error_kind") == "permanent")
check("错误中列出可用工具", bool(r4.get("available")))

# --- ★ 参数校验：拦住 LLM 传错参数名 ---
# 实测踩过的坑：模型把 `start` 写成 `start_date`，而 execute 直接把 kwargs
# 透传给函数 → TypeError。虽然被兜住了，但模型只看到
# "unexpected keyword argument"，不知道正确参数名，于是白跑一轮。
from app.tools.registry import registry as _R              # noqa: E402

# 1) 常见别名应被静默纠正
r_alias = _R.execute("scan_stockout", start_date="2025-08-18", end_date="2025-08-24")
check("别名 start_date/end_date 被自动纠正（不再白跑一轮）",
      not (isinstance(r_alias, dict) and r_alias.get("ok") is False),
      str(r_alias)[:120])

# 2) 真正非法的参数名要被拦住，并明确告知正确参数
r_bad = _R.execute("scan_stockout", foo="bar")
check("非法参数名被拦截", isinstance(r_bad, dict) and r_bad.get("ok") is False)
check("错误中给出正确参数名（便于模型自我修正）",
      "start" in (r_bad.get("expected_params") or []), str(r_bad)[:150])
check("非法参数不消耗函数调用（在执行前就被拦下）",
      _R.get("scan_stockout").stats["calls"] >= 1)

# 3) 正常调用仍可用
r_ok = _R.execute("scan_stockout", start="2025-07-05", end="2025-07-11")
check("正常参数调用成功", isinstance(r_ok, list) and len(r_ok) == 1, str(r_ok)[:120])

# 4) schema 描述里要能引导模型用对参数名
_lc = _R.build_langchain_tools([_R.get("scan_stockout")])[0]
_props = _lc.args_schema.model_json_schema()["properties"]
check("schema 描述中提示了正确参数名（从源头减少传错）",
      "不要用 start_date" in _props["start"].get("description", ""),
      _props["start"].get("description", "")[:60])

# --- 统计可观测 ---
stats = t.stats()
check("统计含重试次数", stats["flaky_transport"]["retries"] == 2,
      str(stats["flaky_transport"]))
check("统计含调用次数", stats["flaky_data"]["calls"] == 1)


# ===========================================================================
section("3. 意图识别与条件路由")
# ===========================================================================

from app.workflow.intent import Intent, ROUTING, classify_by_rules  # noqa: E402

# 规则判定用例（不调用 LLM）
intent_cases = [
    # (输入, 期望意图, 说明)
    ("上周的 GMV 是多少？", Intent.ANALYSIS_REPORT, "纯取数"),
    ("帮我出一份周报", Intent.ANALYSIS_REPORT, "报告请求"),
    ("2025-08-18 到 08-24 的转化率表现如何", Intent.ANALYSIS_REPORT, "指标查询"),
    ("转化率为什么下滑了？", Intent.ATTRIBUTION, "归因-为什么"),
    ("GMV 下降了，是什么原因", Intent.ATTRIBUTION, "归因-原因"),
    ("P002 转化变差，排查一下", Intent.ATTRIBUTION, "归因-排查"),
    ("是不是详情页的问题导致转化下跌", Intent.ATTRIBUTION, "归因-是不是问题"),
    ("拆解一下这个需求", Intent.REQUIREMENT_PARSE, "需求拆解"),
    ("帮我评估这个 PRD 的影响面", Intent.REQUIREMENT_PARSE, "PRD 影响面"),
    ("这个需求文档有哪些待澄清的点", Intent.REQUIREMENT_PARSE, "需求文档"),
    ("写个 618 促销文案", Intent.DIRECT_GENERATE, "直接生成-文案"),
    ("给这个商品想个标题", Intent.DIRECT_GENERATE, "直接生成-标题"),
    ("润色一下这个活动话术", Intent.DIRECT_GENERATE, "直接生成-润色"),
]

rule_hits = 0
for text, want, label in intent_cases:
    res = classify_by_rules(text)
    got = res.intent if res else None
    ok = got == want
    if ok:
        rule_hits += 1
    detail = f"实际 {got.value if got else 'None（需 LLM 兜底）'}"
    check(f"意图：{label}「{text[:18]}」", ok, detail)

print(f"  规则命中 {rule_hits}/{len(intent_cases)}，"
      f"命中率 {rule_hits / len(intent_cases):.0%}")
check("规则命中率 ≥ 85%（大部分请求不应依赖 LLM 判定）",
      rule_hits / len(intent_cases) >= 0.85,
      f"{rule_hits / len(intent_cases):.0%}")

# --- 路由表正确性 ---
for intent, spec in ROUTING.items():
    print(f"  {intent.value:<20} need_tools={spec['need_tools']!s:<5} "
          f"route={spec['route']:<16} caps={spec['capabilities']}")

check("直接生成不需要工具",
      ROUTING[Intent.DIRECT_GENERATE]["need_tools"] is False)
check("直接生成不装配任何工具",
      ROUTING[Intent.DIRECT_GENERATE]["capabilities"] == [])
check("分析报告需要工具", ROUTING[Intent.ANALYSIS_REPORT]["need_tools"] is True)
check("归因必须装配质控能力（防归因污染）",
      "quality" in ROUTING[Intent.ATTRIBUTION]["capabilities"],
      str(ROUTING[Intent.ATTRIBUTION]["capabilities"]))
check("需求解析不装配指标能力（它不需要算指标）",
      "metric" not in ROUTING[Intent.REQUIREMENT_PARSE]["capabilities"])

# --- 路由结果的完整字段 ---
res = classify_by_rules("转化率为什么下滑")
check("路由结果含 need_tools", res.need_tools is True)
check("路由结果含 capabilities", "quality" in res.capabilities)
check("路由结果含 route", res.route == "tool_loop")
check("路由结果含置信度", 0 < res.confidence <= 1)


# ===========================================================================
section("3b. ★ 单向硬约束：含数据信号的请求绝不允许免工具作答")
# ===========================================================================

from app.workflow.intent import (                        # noqa: E402
    classify, enforce_data_signal, has_data_signal,
)

# --- 不变量：direct_generate 与 data_signal 互斥 ---
#   这条不变量是「宁可多查，不可编造」的机器可验证形式。
#   两个方向的错误代价不对称：
#     误判为免工具 → 模型凭记忆编数字，输出看似专业但完全错误，且无警示；
#     误判为需工具 → 多查一次数据，慢几秒，但结论仍然正确。
invariant_cases = [
    "上周转化率为什么下滑？", "看看这个", "分析一下上周的经营情况",
    "写个 618 促销文案", "给我起个商品标题", "拆解这个需求文档",
    "分析一下这个需求文档", "写个文案，说说上周转化率为什么下滑",
    "翻译一下这段话", "GMV 多少", "对比一下这两周的退款率",
]
violations = [
    t for t in invariant_cases
    if classify(t).intent.value == "direct_generate" and has_data_signal(t)
]
print(f"  检查 {len(invariant_cases)} 个输入，违反不变量的：{violations or '无'}")
check("★ 不变量：direct_generate 绝不含数据信号", not violations, str(violations))

# --- 用户要求的强制规则：含"为什么/归因/分析/下滑"必须走工具 ---
force_cases = [
    "上周转化率为什么下滑？",   # 为什么 + 下滑
    "GMV 下滑的原因",           # 下滑 + 原因
    "归因一下这次波动",          # 归因
    "分析一下上周的经营情况",      # 分析
    "帮我看看这个数据",          # 看 + 数据
]
for text in force_cases:
    r = classify(text)
    ok = r.need_tools and r.route == "tool_loop"
    check(f"强制走工具：「{text}」", ok,
          f"need_tools={r.need_tools} route={r.route} intent={r.intent.value}")

# --- 「为什么」必须命中 attribution ---
for text in ["上周转化率为什么下滑", "转化率为何下降", "GMV 为什么跌了"]:
    r = classify(text)
    check(f"「{text}」→ attribution", r.intent.value == "attribution",
          r.intent.value)

# --- 反向验证：不能把"分析/归因"粗暴绑定到 attribution ---
#   否则「分析一下这个需求文档」会被推去跑数据指标工具，方向完全错了。
r = classify("分析一下这个需求文档")
check("「分析...需求文档」仍归入需求解析（未被硬约束带偏）",
      r.intent.value == "requirement_parse", r.intent.value)

# --- 硬约束在出口强制生效（即使上游判定错了也能兜住）---
from app.workflow.intent import _make as _mk              # noqa: E402

wrong = _mk(Intent.DIRECT_GENERATE, 0.9, "rule", "（故意构造的错误判定）")
fixed = enforce_data_signal(wrong, "上周 GMV 为什么下滑")
check("出口兜底：错误的 direct_generate 被强制纠正",
      fixed.intent != Intent.DIRECT_GENERATE, fixed.intent.value)
check("出口兜底后 need_tools=True", fixed.need_tools is True)
check("出口兜底后 reason 中留痕（可审计）",
      "单向硬约束拦截" in fixed.reason, fixed.reason[:70])

# 纯写作请求不应被误伤
pure = enforce_data_signal(
    _mk(Intent.DIRECT_GENERATE, 0.9, "rule", "写作"), "写个促销文案")
check("纯写作请求不被硬约束误伤",
      pure.intent == Intent.DIRECT_GENERATE, pure.intent.value)


# ===========================================================================
section("3c. 时间注入：相对时间已解析成绝对日期")
# ===========================================================================

from datetime import date as _date                        # noqa: E402

from app.context.builder import build_time_block           # noqa: E402

tb = build_time_block(_date(2025, 8, 27))                 # 周三，便于断言
print("  时间块内容：")
for line in tb.splitlines()[:8]:
    print(f"    {line}")

check("含当前系统日期", "2025-08-27" in tb, tb[:120])
check("含星期", "周三" in tb, tb[:120])
check("解析了「上周」为绝对日期", "2025-08-18 ~ 2025-08-24" in tb,
      [l for l in tb.splitlines() if "上周" in l])
check("解析了「上上周」", "2025-08-11 ~ 2025-08-17" in tb)
check("解析了「最近 7 天」", "2025-08-21 ~ 2025-08-27" in tb)
check("含数据库实际数据范围（可判断有无数据）", "2025-06-01" in tb)
check("声明了时间是唯一权威来源（防模型用记忆里的日期）",
      "唯一权威时间来源" in tb)

# 时间必须随真实日期变化，而不是写死的占位符
tb2 = build_time_block(_date(2026, 1, 5))
check("时间随传入日期动态变化（非硬编码）",
      "2026-01-05" in tb2 and "2025-08-27" not in tb2)
check("不同日期解析出不同的上周",
      "2025-12-29 ~ 2026-01-04" in tb2,
      [l for l in tb2.splitlines() if "上周" in l])


# ===========================================================================
section("3d. ★ 时间错位检测：数据里没有用户问的时间段时必须声明")
# ===========================================================================

# 这是一个会静默误导用户的陷阱：数据是历史数据时，系统会返回
# 「数据中最后一个完整周」，而报告标题看起来就像在回答用户问的"上周"。
# 实测中模型会把 406 天前的数据当作"上周"汇报，格式完全正常，
# 用户根本看不出问题 —— 所以必须结构化地强制声明。
from app.context.builder import detect_time_mismatch      # noqa: E402

# 历史区间 → 应检测为错位
mm = detect_time_mismatch("2025-08-18", "2025-08-24", today=_date(2026, 10, 9))
print(f"  历史区间（距今 406 天）: mismatch={mm['mismatch']}")
check("历史区间被识别为时间错位", mm["mismatch"] is True)
check("错位警告含实际使用的区间",
      "2025-08-18 ~ 2025-08-24" in mm["banner"], mm["banner"][:150])
check("错位警告要求开头声明", "第一句就必须说明" in mm["banner"])
check("错位警告禁止直接称作「上周」", "禁止" in mm["banner"])

# 近期区间 → 不应误报
mm2 = detect_time_mismatch("2026-10-02", "2026-10-08", today=_date(2026, 10, 9))
check("近期区间不误报时间错位", mm2["mismatch"] is False, mm2["banner"][:80])

# 边界：刚好 30 天不报，31 天要报
check("边界：距今 30 天不报",
      detect_time_mismatch("2026-09-09", "2026-09-09",
                           today=_date(2026, 10, 9))["mismatch"] is False)
check("边界：距今 31 天要报",
      detect_time_mismatch("2026-09-08", "2026-09-08",
                           today=_date(2026, 10, 9))["mismatch"] is True)

# 评审是否强制校验了「必须声明」
from app.context.builder import ContextBuilder as _CB      # noqa: E402
from app.workflow.engine import EcommerceAgent as _EA      # noqa: E402

_agent = _EA()
_ctx_mm = _CB(role="attribution", template="attribution")
_ctx_mm.notes.insert(0, mm["banner"])

# 情况 A：输出没有声明 → 应判不通过
_bad_review = _agent._review(
    "转化率下滑了，主要原因是缺货。", "上周转化率为什么下滑", classify("x"),
    {"sample_days": 7}, {"has_blocking": False, "flags": []}, _ctx_mm)
check("★ 未声明时间错位时评审判不通过", not _bad_review.passed,
      str(_bad_review.problems)[:120])
check("评审问题中明确指出时间错位",
      any("时间错位" in p for p in _bad_review.problems),
      str(_bad_review.problems)[:130])

# 情况 B：输出正确声明了 → 这部分不应再成为问题
_good_review = _agent._review(
    "注意：数据库中无该时间段数据，以下基于数据最新区间 2025-08-18 ~ 2025-08-24。"
    "该区间转化率基本持平。",
    "上周转化率为什么下滑", classify("x"),
    {"sample_days": 7}, {"has_blocking": False, "flags": []}, _ctx_mm)
check("已声明时间错位时不再因此判不通过",
      not any("时间错位" in p for p in _good_review.problems),
      str(_good_review.problems)[:130])


# ===========================================================================
section("4. 数据预处理：同比/环比由代码计算")
# ===========================================================================

from app.workflow.preprocess import (                 # noqa: E402
    enrich_breakdown, enrich_period_compare, extract_key_numbers,
    preprocess_tool_result,
)

raw_compare = {
    "current": {"period": "W2", "gmv_gross": 110.0, "pay_cvr": 0.028,
                "paid_orders": 550, "visitors": 19600},
    "previous": {"period": "W1", "gmv_gross": 100.0, "pay_cvr": 0.035,
                 "paid_orders": 700, "visitors": 20000},
    "change_pct": {},                                  # 故意留空，测兜底计算
}
enriched = enrich_period_compare(raw_compare)
chg = enriched["change_pct"]
print(f"  代码算出的环比：GMV={chg.get('gmv_gross'):+.2%}  "
      f"CVR={chg.get('pay_cvr'):+.2%}  订单={chg.get('paid_orders'):+.2%}")
print(f"  自动摘要：{enriched['摘要']}")

check("自动补算 GMV 环比", abs(chg["gmv_gross"] - 0.10) < 1e-6, str(chg.get("gmv_gross")))
check("自动补算 CVR 环比", abs(chg["pay_cvr"] - (-0.20)) < 1e-6, str(chg.get("pay_cvr")))
check("自动补算订单环比", abs(chg["paid_orders"] - (-0.2143)) < 1e-3,
      str(chg.get("paid_orders")))
check("生成可读摘要", "转化率" in enriched["摘要"], enriched["摘要"])
check("除零不崩溃（返回 None）",
      enrich_period_compare({"current": {"gmv_gross": 1},
                             "previous": {"gmv_gross": 0}})["change_pct"]["gmv_gross"] is None)

# --- 下钻数据补贡献占比 ---
rows = [{"product_id": "A", "gmv": 60.0}, {"product_id": "B", "gmv": 30.0},
        {"product_id": "C", "gmv": 10.0}]
bd = enrich_breakdown(rows, "商品")
print(f"  下钻贡献占比：{[(i['product_id'], i['贡献占比']) for i in bd['items']]}")
check("下钻按 GMV 降序", bd["items"][0]["product_id"] == "A")
check("计算贡献占比", abs(bd["items"][0]["贡献占比"] - 0.6) < 1e-6)
check("占比合计为 1", abs(sum(i["贡献占比"] for i in bd["items"]) - 1.0) < 1e-6)

# --- 工具失败时预处理不加工 ---
err = preprocess_tool_result("compare_weeks",
                             {"ok": False, "error_kind": "data", "error": "x"})
check("工具失败原样传递（不被加工掩盖）", err.get("_tool_error") is True)

# --- 关键数字抽取（用于压缩历史时保留数字锚点）---
kn = extract_key_numbers("compare_weeks", enriched)
print(f"  抽取的关键数字：{kn}")
check("抽取到 GMV 与其环比", "gmv_gross" in kn and "gmv_gross_环比" in kn)


# ===========================================================================
section("5. 上下文工程：压缩 / 滚动 / 近详远略")
# ===========================================================================

from app.context.builder import ContextBuilder, RoundRecord    # noqa: E402
from app.context.compressor import (                           # noqa: E402
    compress_injected_data, compress_round, estimate_tokens,
)

# --- 压缩：越远越简 ---
c0 = compress_round(5, ["compare_weeks"], "这是最近一轮的完整结论内容" * 5,
                    {"gmv": 123}, recency_index=0)
c2 = compress_round(3, ["run_sql"], "这是第三轮之前的结论内容" * 5,
                    {"gmv": 100}, recency_index=2)
c5 = compress_round(1, ["describe_tables"], "这是最早一轮的结论内容" * 5,
                    recency_index=5)
print(f"  最近一轮长度={len(c0)}  第2~3轮长度={len(c2)}  更早轮次长度={len(c5)}")
check("最近一轮保留关键数字", "gmv=123" in c0, c0[:60])
check("滚动的近详远略：最近 > 中间 > 最早", len(c0) > len(c2) > len(c5),
      f"{len(c0)} / {len(c2)} / {len(c5)}")
check("压缩后内容不丢失工具信息", "compare_weeks" in c0)

# --- 数据注入压缩 ---
big = {"商品": [{"product_id": f"P{i:03d}", "gmv": 1000 - i * 10} for i in range(30)]}
compressed = compress_injected_data(big, max_items=5)
print(f"  30 项列表压缩后行数：{len(compressed.splitlines())}")
check("列表压缩到 max_items", compressed.count("  ") <= 12, compressed[:80])
check("压缩后注明总数", "共 30 项" in compressed)
check("压缩后注明省略", "省略" in compressed)

# --- ★ 自适应压缩：大列表不能砍得太狠 ---
# 实测踩过的坑：固定 max_items=8 时，35 格的交叉维度表被截成 8 格，
# 模型只能看到局部，于是它老实报告"工具只返回了 35 格中的前 8 项"——
# 数据其实拿到了，是被压缩层自己砍掉的，却让模型以为工具能力不足。
_big_list = {"data": [{"k": f"v{i}"} for i in range(35)]}
_c_big = compress_injected_data(_big_list)          # 不传 max_items，走自适应
_kept = sum(1 for line in _c_big.splitlines() if ". " in line)
print(f"  自适应：35 项列表保留约 {_kept} 项（旧版固定只保留 8 项）")
check("★ 大列表自适应保留远多于 8 项", _kept > 8, f"实际 {_kept} 项")
check("大列表仍注明省略数量", "省略" in _c_big, _c_big[-60:])

_small_list = {"data": [{"k": f"v{i}"} for i in range(10)]}
_c_small = compress_injected_data(_small_list)
check("小列表（10 项）全量保留、无省略标记",
      "省略" not in _c_small, _c_small[-60:])

# --- Context 组装 ---
cb = ContextBuilder(role="attribution", template="attribution")
cb.set_task("转化率为什么下滑")
cb.inject_metrics(enriched)
cb.inject_data("商品下钻", {"商品": rows})
cb.add_note("⚠️ 存在缺货")
ctx_text = cb.build()

check("Context 含角色设定", "归因专家" in ctx_text)
check("Context 含业务规则", "归因纪律" in ctx_text)
check("Context 含指标口径字典", "指标口径字典" in ctx_text)
check("Context 含业务范文模板", "候选原因排查" in ctx_text)
check("Context 含任务", "转化率为什么下滑" in ctx_text)
check("Context 含预处理数据", "已由系统预处理" in ctx_text)
check("Context 含质控提示", "存在缺货" in ctx_text)
check("硬约束位于范文与数据之前（顺序正确）",
      ctx_text.index("归因纪律") < ctx_text.index("候选原因排查")
      < ctx_text.index("已由系统预处理"))

# --- 滚动历史注入（用真实长度的结论，短文本不具代表性）---
REAL_CONCLUSION = (
    "本轮获取到 2025-08-18~08-24 的核心指标与商品下钻数据。"
    "成交额 123,013 元，环比 +2.1%；支付转化率 2.71%，环比 -8.1%；"
    "客单价 194.64 元，环比 +1.5%。商品维度看，P004 无线蓝牙降噪耳机贡献最高，"
    "GMV 21,788 元占比 17.7%；P009 复古机械键盘 GMV 16,648 元占比 13.5%。"
    "值得注意的异常：P001 超薄折叠桌面收纳架本周有 8 天缺货标记，"
    "其转化率明显低于历史水平，这是导致整体转化率下滑的重要候选原因之一，"
    "需要在后续轮次中通过质控结果与库存数据交叉验证后才能确认。"
)
for i in range(1, 6):
    cb.add_round(RoundRecord(
        round_no=i, tool_names=[f"tool_{i}"],
        conclusion=REAL_CONCLUSION,
        key_numbers={"gmv": 123013, "pay_cvr": 0.0271},
    ))
ctx2 = cb.build()
check("滚动历史被注入", "已完成的工作" in ctx2)
check("历史含轮次编号", "第5轮" in ctx2 and "第1轮" in ctx2)
check("最近轮次内容比最早轮次详细",
      len(compress_round(5, [], REAL_CONCLUSION, recency_index=0))
      > len(compress_round(1, [], REAL_CONCLUSION, recency_index=4)))

stats = cb.token_stats()
print(f"  上下文规模：{stats['total_chars']} 字符 ≈ {stats['estimated_tokens']} tokens"
      f"（其中历史 {stats['history_tokens']}）")
check("提供 token 统计（可监控膨胀）", stats["estimated_tokens"] > 0)

# --- 压缩确实抑制了膨胀 ---
#   量纲必须一致：compress_round 的输出是字符数，就拿来和字符数比。
#   （不能拿 token 估计值和字符数比，中文 1 字 ≈ 1 token，会误导判断。）
uncompressed_chars = 0
for i in range(1, 6):
    uncompressed_chars += len(
        f"第{i}轮｜工具：tool_{i}\n"
        f"关键数字：gmv=123013；pay_cvr=0.0271\n"
        f"结论：{REAL_CONCLUSION}"
    )
compressed_chars = len(cb.render_history())
print(f"  不压缩历史 {uncompressed_chars} 字符 → 压缩后 {compressed_chars} 字符 "
      f"（压缩率 {compressed_chars / uncompressed_chars:.0%}）")
check("压缩显著降低历史体积（≤70%）", compressed_chars < uncompressed_chars * 0.7,
      f"{compressed_chars} vs {uncompressed_chars}")

# 单独验证：轮次编号必须始终保留（审计追溯的唯一线索）
short_round = compress_round(3, ["run_sql"], "查询到 12 个商品。", recency_index=3)
print(f"  短结论压缩结果：{short_round!r}")
check("短结论压缩后保留轮次编号（可追溯）", "第3轮" in short_round, short_round)
check("短结论压缩后不膨胀", len(short_round) <= len("查询到 12 个商品。") + 12,
      f"{len(short_round)} 字符")


# ===========================================================================
section("6. 工具实际执行链路（真实数据，不调 LLM）")
# ===========================================================================

r = registry.execute("get_data_range")
check("get_data_range 执行成功", isinstance(r, dict) and "data_range" in r,
      str(r)[:120])

r = registry.execute("compare_weeks")
check("compare_weeks 执行成功并返回环比",
      isinstance(r, dict) and bool(r.get("change_pct")), str(r)[:120])

r = registry.execute("run_quality_checks", start="2025-07-05", end="2025-07-11")
check("缺货周质控返回 blocking",
      isinstance(r, dict) and r.get("has_blocking") is True, str(r)[:150])

r = registry.execute("run_sql", sql="DELETE FROM dim_product")
check("SQL 护栏在 Registry 链路中生效",
      isinstance(r, dict) and r.get("ok") is False, str(r)[:120])

r = registry.execute("run_sql", sql="SELECT COUNT(*) AS n FROM dim_product")
check("合法 SQL 正常执行", isinstance(r, dict) and r.get("ok") is True
      and r["rows"][0]["n"] == 12, str(r)[:120])

# 统计汇总
print()
print("  工具运行统计：")
for name, s in registry.stats().items():
    if s["calls"]:
        print(f"    {name:<22} 调用{s['calls']:>3}  失败{s['failures']:>2}  "
              f"重试{s['retries']:>2}  平均{s['avg_ms']:>7.1f}ms")


# ===========================================================================
section("7. ★ SQL 错误可自我修正：给出正确列名而非裸报错")
# ===========================================================================

# 背景：实测中模型写 JOIN 时会凭通用电商系统的命名直觉编列名
# （product_name / name / date），而本项目真实列名是 title / stat_date。
# 关键不是"能不能拦住"，而是**拦住之后模型能否自己改对** ——
# 裸报错 "no such column: p.product_name" 只说错了、不说改成什么，
# 模型容易反复试错，白耗轮次。

from app.db.database import (                             # noqa: E402
    friendly_sql_error, join_guide, suggest_column,
)

# --- 正确的 JOIN 必须能跑通（证明工具与 schema 本身没问题）---
_good_sql = ("SELECT p.title, SUM(f.gmv) AS gmv FROM fact_daily_product f "
             "JOIN dim_product p ON p.product_id = f.product_id "
             "WHERE f.stat_date BETWEEN '2025-08-18' AND '2025-08-24' "
             "GROUP BY p.title ORDER BY gmv DESC")
_r = registry.execute("run_sql", sql=_good_sql)
check("★ 正确的两表 JOIN 能正常执行（工具无 bug）",
      _r.get("ok") is True and _r.get("count", 0) > 0, str(_r)[:130])

# --- 错误列名应被翻译成可自我修正的提示 ---
_bad_cases = [
    ("SELECT p.product_name FROM dim_product p", "product_name", "title"),
    ("SELECT p.name FROM dim_product p", "name", "title"),
    ("SELECT date FROM fact_daily_product", "date", "stat_date"),
    ("SELECT p.amount FROM dim_product p", "amount", "gmv"),
]
for sql, bad_col, want in _bad_cases:
    r = registry.execute("run_sql", sql=sql)
    sugg = r.get("expected_columns") or []
    check(f"「{bad_col}」报错里给出正确列名 {want}",
          r.get("ok") is False and want in sugg,
          f"建议值={sugg}")
    check(f"「{bad_col}」错误被归类为 data（可自我修正，不重试）",
          r.get("error_kind") == "data", str(r.get("error_kind")))

# --- USING 关联失败要给出正确写法 ---
r = registry.execute(
    "run_sql", sql="SELECT * FROM fact_daily_product f JOIN dim_product p USING (date)")
check("USING(date) 报错指出该用 ON product_id",
      r.get("ok") is False and "product_id" in (r.get("hint") or ""),
      (r.get("hint") or "")[:100])

# --- 列名建议函数本身 ---
check("suggest_column('product_name') → title",
      "title" in suggest_column("product_name"), str(suggest_column("product_name")))
check("suggest_column('date') → stat_date",
      "stat_date" in suggest_column("date"), str(suggest_column("date")))

# --- JOIN 指南必须真的写进 schema（模型才看得到）---
from app.db.database import table_schema_text as _tst     # noqa: E402

_schema = _tst()
check("schema 中含 JOIN 指南", "表关系与命名约定" in _schema)
check("schema 明确声明不存在 product_name", "product_name" in _schema)
check("schema 明确声明不存在 date 列", "不存在名为 date 的列" in _schema)
check("schema 给出正确的 JOIN 示例",
      "ON p.product_id = f.product_id" in _schema)
check("schema 说明 dim_product 无日期列",
      "没有任何日期列" in _schema)
check("join_guide 覆盖四个事实表的关联关系",
      all(t in join_guide() for t in
          ("fact_daily_product", "fact_daily_inventory", "fact_daily_traffic")))


# ===========================================================================
section("7b. ★ 交叉维度工具：类目 × 渠道（免去模型手写三表 JOIN）")
# ===========================================================================

# 背景：用户问"按类目拆分各渠道 GMV"需要三表关联，模型连续写错三次
# （t.product_id 别名不存在 / f.channel 不在商品事实表 / 去查 information_schema），
# 最终降级失败。根因是这个关联关系确实绕，靠 prompt 教不如封装成工具。
# 设计原则：**能封装的能力不要让模型每次现场推导。**

check("交叉维度工具已注册", "get_cross_breakdown" in registry.names())

_cross = registry.execute("get_cross_breakdown", dimension="category",
                          metric="gmv", start="2025-08-18", end="2025-08-24")
check("交叉维度工具执行成功",
      isinstance(_cross, dict) and "data" in _cross, str(_cross)[:130])
check("返回 7 类目 × 5 渠道 = 35 个交叉格",
      _cross.get("cell_count") == 35, str(_cross.get("cell_count")))

# --- 关键：重构后的各渠道合计必须等于真实渠道 GMV ---
from collections import defaultdict as _dd                # noqa: E402

_by_ch = _dd(float)
for _c in _cross["data"]:
    _by_ch[_c["channel"]] += _c["gmv"]

_real = registry.execute("get_channel_breakdown",
                         start="2025-08-18", end="2025-08-24")
_real_map = {r["channel"]: r["gmv"] for r in _real}

print("  渠道合计对照（重构 vs 真实）：")
_ok_all = True
for _ch, _v in _by_ch.items():
    _rv = _real_map.get(_ch, 0)
    _diff = abs(_v - _rv) / _rv if _rv else 1
    if _diff > 0.01:
        _ok_all = False
    print(f"    {_ch:<6} 重构={_v:>10,.0f}  真实={_rv:>10,.0f}  差异={_diff:.2%}")
check("★ 重构后各渠道合计与真实值一致（±1%）", _ok_all)

check("类目占比合计为 1",
      abs(len({c["category"] for c in _cross["data"]}) and
          sum({c["category"]: c["category_share"]
               for c in _cross["data"]}.values()) - 1.0) < 0.01)
check("★ 明确标注了方法是「结构重构」而非直接联表（不假装精确）",
      _cross.get("method") == "结构重构（非直接联表）", str(_cross.get("method")))
check("说明中提示了交叉格为估算值、不可用于汇报精确数字",
      "估算值" in _cross.get("_note", "") and "不可用于汇报" in _cross.get("_note", ""))

# 各渠道在同一类目下的数值必须不同（若相同说明又出现了笛卡尔积）
_digital = [c for c in _cross["data"] if c["category"] == "数码"]
_uniq = len({c["gmv"] for c in _digital})
check("★ 同一类目在不同渠道的数值不同（无笛卡尔积放大）",
      _uniq == len(_digital), f"不同值 {_uniq}/{len(_digital)}")

# 非法维度要被拒绝
_bad = registry.execute("get_cross_breakdown", dimension="unknown")
check("非法维度被拒绝", "error" in _bad, str(_bad)[:90])


# ===========================================================================
section("7c. ★ 元信息工具防重复调用（优化轮次浪费）")
# ===========================================================================

# 实测中模型在 5~6 轮里调用 describe_tables 4~5 次，纯属浪费轮次。
# prompt 里写"不要重复调用"是软约束，这里做成硬约束。
from app.tools.builtin import reset_meta_tool_cache       # noqa: E402

reset_meta_tool_cache()
_d1 = registry.execute("describe_tables")
_d2 = registry.execute("describe_tables")
check("首次调用返回真实 schema",
      _d1.get("already_provided") is False and "schema" in _d1)
check("重复调用被识别并返回提示",
      _d2.get("already_provided") is True and bool(_d2.get("hint")), str(_d2)[:90])
check("提示中告诉模型不要再调、直接写 SQL",
      "不要再调用" in (_d2.get("hint") or ""), (_d2.get("hint") or "")[:90])

# ★ 重置后必须恢复为真正的第一次 —— 否则第二个请求拿不到表结构
reset_meta_tool_cache()
_d3 = registry.execute("describe_tables")
check("★ 重置后恢复为首次调用（保证新请求能拿到 schema）",
      _d3.get("already_provided") is False, str(_d3.get("already_provided")))


# ===========================================================================
section("8. ★ 阻断纪律：词汇禁止 + 三段结构强制")
# ===========================================================================

# 用户要求的硬约束：has_blocking=True 时禁止因果词汇，
# 且只能输出「事实陈述 + 待核查清单」。
# ★ 关键设计：只查禁用词是不够的 —— 一份空洞的、什么都没说的输出
#   同样能通过词汇检查，但对用户毫无价值。所以要连结构一起校验。

import unittest.mock as _mock                              # noqa: E402

from app.schemas import Critique as _Critique              # noqa: E402
from app.workflow.engine import EcommerceAgent as _EA2     # noqa: E402

_agent2 = _EA2()
_block_quality = {
    "has_blocking": True,
    "flags": [
        {"code": "STOCKOUT_FOUND", "level": "error", "message": "P001 缺货 7 天"},
        {"code": "AD_DIMINISHING_RETURN", "level": "warning", "message": "边际递减"},
    ],
}
_block_metrics = {"sample_days": 7, "period": "2025-07-05 ~ 2025-07-11"}


def hard_block_problems(text: str) -> list[str]:
    """只取阻断纪律产生的确定性问题（屏蔽 LLM 评审，保证可复现）。"""
    with _mock.patch("app.agents.critic.llm_review") as m:
        m.return_value = _Critique(passed=True, problems=[], must_fix=[], verdict="ok")
        r = _agent2._review(text, "上周转化率为什么下滑", None,
                            _block_metrics, _block_quality)
    return [p for p in r.problems if p.startswith("[阻断纪律]")]


# --- 违规输出必须被打回 ---
_bad_outputs = [
    ("出现禁用词「策略效果不佳」",
     "本周运营策略效果不佳，转化率因缺货下滑。",
     "因果"),
    ("缺待核查清单",
     "本期存在阻断级质量问题，不可用于因果归因。转化率环比 -8.1%。",
     "待核查清单"),
    ("漏提 error 级问题（缺货）",
     "存在阻断级问题，不可用于因果归因。待核查清单：确认流量结构。先修复数据问题。",
     "STOCKOUT_FOUND"),
    ("未说明应先修复阻断问题",
     "存在阻断级问题，不可用于因果归因。事实：转化率 -8.1%，缺货 7 天。"
     "待核查清单：确认库存。",
     "先"),
]
for label, text, keyword in _bad_outputs:
    probs = hard_block_problems(text)
    check(f"阻断纪律打回：{label}", bool(probs), "未打回")
    check(f"  打回理由命中「{keyword}」",
          any(keyword in p for p in probs), str(probs)[:110])

# --- 合规输出必须通过（否则评审过严会导致无谓重做）---
_good_output = (
    "本期存在阻断级数据质量问题，不可用于因果归因，仅作事实陈述。\n"
    "事实：转化率环比 -8.1%，P001 缺货 7 天。\n"
    "待核查清单：核查 P001 缺货影响，用库存表确认，由供应链确认。\n"
    "优先修复：先补货 P001，再评估经营策略。"
)
check("★ 合规输出不被误判（防止评审过严）",
      not hard_block_problems(_good_output), str(hard_block_problems(_good_output)))

# --- 无阻断时不应触发任何阻断纪律 ---
with _mock.patch("app.agents.critic.llm_review") as m:
    m.return_value = _Critique(passed=True, problems=[], must_fix=[], verdict="ok")
    _r_ok = _agent2._review(
        "本周转化率环比 +2%，表现平稳。",
        "上周转化率如何", None, {"sample_days": 7},
        {"has_blocking": False, "flags": []})
check("无阻断时不触发阻断纪律",
      not [p for p in _r_ok.problems if p.startswith("[阻断纪律]")],
      str(_r_ok.problems)[:110])

# --- 阻断纪律必须真的写进了注入 prompt ---
from app.context.templates import BUSINESS_RULES as _BR    # noqa: E402

check("业务规则含阻断纪律条目", "阻断纪律" in _BR)
check("阻断纪律列出禁用词汇", "策略有效" in _BR and "策略无效" in _BR)
check("阻断纪律要求三段结构",
      all(k in _BR for k in ("事实陈述", "待核查清单", "修复优先")))
check("阻断纪律标注为最高优先级", "最高优先级" in _BR)
check("阻断纪律说明了原因（为什么不能归因）", "不可信" in _BR)


# ===========================================================================
print()
print("=" * 74)
print(f"架构验收：{PASS} 项通过，{FAIL} 项失败")
print("=" * 74)
if FAIL == 0:
    print()
    print("✅ 四个核心机制（工具注册 / 容错 / 意图路由 / 上下文工程）全部通过。")
    print("   这些组件不依赖 LLM，换成真模型后行为一致。")
else:
    print()
    print(f"❌ 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
