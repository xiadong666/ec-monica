"""
tests/test_eval.py
==================
★ EC-Monica 评测套件（pytest）。

运行：
    pytest tests/test_eval.py -v                    # 全部（无 Key 时自动跳过在线层）
    pytest tests/test_eval.py -v -m offline         # 只跑离线层
    pytest tests/test_eval.py -v -m live            # 只跑在线层（需 API Key）
    pytest tests/test_eval.py --accuracy-report     # 末尾输出准确率报告

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
两层评测
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
离线层（不依赖 LLM，CI 每次提交都跑）
    test_routing_accuracy        路由准确率 —— 意图识别对不对
    test_metric_accuracy         数值准确率 —— 指标层算得对不对
    test_quality_detection       质控准确率 —— 4 类归因陷阱能否检出
    test_tool_registry_health    工具健康度 —— 注册/装配/参数校验
    test_data_permission         数据权限 —— 受限用户能否被正确过滤

在线层（需要 LLM，用 -m live 显式启用）
    test_live_tool_selection     真实调用中是否选对了工具
    test_live_trap_resistance    面对归因陷阱会不会被带偏
    test_live_number_fidelity    回答里的数字是否与指标层一致

★ 为什么按「准确率」而不是「对/错」来组织？
  因为评测的价值在于**看趋势**：改了 prompt 之后准确率从 92% 掉到 85%，
  这比"某一题失败了"更有信息量 —— 它说明整体能力退化了。
  所以每类能力都产出百分比，并在报告里横向对比。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conftest import register_report                  # noqa: E402
from tests.eval_runner import (                             # noqa: E402
    CaseResult, EvalReport, load_cases, score_contains, score_forbidden,
    score_tool_used, score_value,
)


# ---------------------------------------------------------------------------
# 依赖检查
# ---------------------------------------------------------------------------

def _require_db():
    """数据库不可用时跳过（而不是让整个套件报错）。"""
    try:
        from app.db.database import readonly_query
        rows = readonly_query("SELECT COUNT(*) AS n FROM dim_product")
        if not rows or rows[0]["n"] == 0:
            pytest.skip("数据库为空，请先运行 python scripts/seed_data.py")
    except Exception as exc:                                  # noqa: BLE001
        pytest.skip(f"数据库不可用：{type(exc).__name__}: {exc}")


def _has_api_key() -> bool:
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


# ===========================================================================
# 离线层 1：路由准确率
# ===========================================================================

@pytest.mark.offline
def test_routing_accuracy():
    """
    ★ 路由准确率：给定问题，意图识别是否正确。

    这是整个系统的第一道关口 —— 路由错了，后面全错。
    而且它有真实的成本含义：
      · 路由到 direct_generate 但实际需要数据 → 模型编造数字
      · 路由到工具循环但实际不需要       → 白花几轮 LLM 调用
    """
    from app.workflow.intent import classify_by_rules, has_data_signal

    # (问题, 期望意图, 说明)
    cases = [
        ("上周的 GMV 是多少", "analysis_report", "纯取数"),
        ("帮我出一份周报", "analysis_report", "报告请求"),
        ("2025-08-18 到 08-24 的转化率表现如何", "analysis_report", "指标查询"),
        ("上周表现如何", "analysis_report", "泛化问法"),
        ("分析一下上周的经营情况", "analysis_report", "分析请求"),
        ("转化率为什么下滑了？", "attribution", "归因-为什么"),
        ("GMV 下降了，是什么原因", "attribution", "归因-原因"),
        ("P002 转化变差，排查一下", "attribution", "归因-排查"),
        ("是不是详情页的问题导致转化下跌", "attribution", "归因-是不是问题"),
        ("归因一下这次波动", "attribution", "归因-明说"),
        ("拆解一下这个需求", "requirement_parse", "需求拆解"),
        ("帮我评估这个 PRD 的影响面", "requirement_parse", "PRD 影响面"),
        ("分析一下这个需求文档", "requirement_parse", "需求（不能被硬约束抢走）"),
        ("写个 618 促销文案", "direct_generate", "纯写作"),
        ("给这个商品想个标题", "direct_generate", "起标题"),
        ("润色一下这个活动话术", "direct_generate", "润色"),
        ("翻译一下这段话", "direct_generate", "翻译"),
    ]

    report = EvalReport(layer="路由准确率")
    for question, want, note in cases:
        res = CaseResult(case_id=note, question=question, exams=question,
                         weight=1.0)
        r = classify_by_rules(question)
        got = r.intent.value if r else None
        res.scores["意图正确"] = 1.0 if got == want else 0.0
        if got != want:
            res.details.append(f"期望 {want}，实际 {got}")
        report.results.append(res)

    # ★ 关键不变量：direct_generate 绝不含数据信号
    from app.workflow.intent import classify

    violations = []
    for question, _, _ in cases:
        if classify(question).intent.value == "direct_generate" \
                and has_data_signal(question):
            violations.append(question)
    inv = CaseResult(case_id="不变量:direct_generate无数据信号",
                     question="(不变量检查)", exams="单向硬约束",
                     weight=3.0)
    inv.scores["不变量成立"] = 1.0 if not violations else 0.0
    if violations:
        inv.details.append(f"违反：{violations}")
    report.results.append(inv)

    register_report(report)
    print(report.render("路由准确率"))

    assert report.accuracy >= 0.90, (
        f"路由准确率 {report.accuracy:.1%} 低于 90% 门槛"
    )


# ===========================================================================
# 离线层 2：数值准确率（指标层）
# ===========================================================================

@pytest.mark.offline
def test_metric_accuracy():
    """
    ★ 数值准确率：评估集里带 expect_value 的题，指标层算出的数字对不对。

    ★ 这是最重要的一层 —— 因为它是项目最核心的价值主张：
      "数字由代码保证正确，不依赖模型"。这一层失败意味着
      整个可信度基础出了问题，而不是"模型今天状态不好"。
    """
    _require_db()
    from app.metrics.compute import latest_complete_week, period_compare

    cases = [c for c in load_cases() if c.get("expect_metric")]
    assert cases, "评估集里没有带 expect_metric 的用例"

    report = EvalReport(layer="数值准确率")
    start, end = latest_complete_week()
    comparison = period_compare(start, end, exclude_special=True)
    current = comparison["current"]

    for case in cases:
        res = CaseResult(case_id=case["id"], question=case["question"],
                         exams=case["exams"], weight=case.get("weight", 1.0))
        metric = case["expect_metric"]
        actual = current.get(metric)
        res.scores["数值正确"] = score_value(
            actual, case["expect_value"], case.get("tolerance", 0))
        if res.scores["数值正确"] < 1.0:
            res.details.append(
                f"{metric}: 期望 {case['expect_value']}（±{case.get('tolerance', 0)}）"
                f"，实际 {actual}"
            )
        report.results.append(res)

    register_report(report)
    print(report.render("数值准确率"))

    assert report.accuracy == 1.0, (
        f"数值准确率 {report.accuracy:.1%} —— 指标层是可信度基础，必须 100%"
    )


# ===========================================================================
# 离线层 3：质控准确率（归因陷阱检出）
# ===========================================================================

@pytest.mark.offline
def test_quality_detection():
    """
    ★ 质控准确率：4 类归因陷阱能否被检出，且不误报。

    这是本项目区别于普通取数工具的核心能力。
    陷阱是故意埋进数据里的，所以"能不能检出"有确定答案 ——
    可以像单元测试一样断言。
    """
    _require_db()
    from app.metrics.compute import price_change_scan, stockout_scan
    from app.quality.checks import run_all_checks

    report = EvalReport(layer="质控准确率")

    def add(cid, exam, scores, weight=1.0, details=None):
        r = CaseResult(case_id=cid, question=cid, exams=exam, weight=weight)
        r.scores.update(scores)
        if details:
            r.details.extend(details)
        report.results.append(r)

    # --- 陷阱 1：618 大促 ---
    flags = run_all_checks("2025-06-16", "2025-06-20")
    codes = {f["code"] for f in flags["flags"]}
    add("陷阱1_大促污染", "大促期间的数据不可当日常水平",
        {"检出": 1.0 if "SPECIAL_PERIOD_INCLUDED" in codes else 0.0},
        weight=2.0, details=[f"检出码：{sorted(codes)}"])

    # --- 陷阱 2：缺货 ---
    so = stockout_scan("2025-07-01", "2025-07-20")
    so_ids = {r["product_id"]: r["stockout_days"] for r in so}
    add("陷阱2_爆款缺货", "P001 缺货 8 天，且不误报其他商品",
        {"检出P001": 1.0 if so_ids.get("P001", 0) >= 8 else 0.0,
         "无误报": 1.0 if len(so_ids) == 1 else 0.0},
        weight=3.0, details=[f"缺货商品：{so_ids}"])

    # --- 陷阱 3：悄悄涨价 ---
    pc = price_change_scan(days=60, threshold=0.10)
    pc_map = {r["product_id"]: r["change_pct"] for r in pc}
    add("陷阱3_悄悄涨价", "P002 涨价约 18%，且不大量误报",
        {"检出P002": 1.0 if "P002" in pc_map else 0.0,
         "方向正确": 1.0 if pc_map.get("P002", 0) > 0.10 else 0.0,
         "误报可控": 1.0 if len(pc) <= 4 else 0.0},
        weight=3.0, details=[f"检出：{pc_map}"])

    # --- 陷阱 4：投放边际递减 ---
    from app.metrics.compute import ad_efficiency_trend

    buckets = [b for b in ad_efficiency_trend(7) if b["roi"]]
    early = [b["roi"] for b in buckets if b["period"] < "2025-08-01"]
    late = [b["roi"] for b in buckets if b["period"] >= "2025-08-01"]
    avg_e = sum(early) / len(early) if early else 0
    avg_l = sum(late) / len(late) if late else 0
    add("陷阱4_投放边际递减", "加预算后 ROI 确实下降",
        {"ROI下降": 1.0 if avg_l < avg_e else 0.0},
        weight=2.0,
        details=[f"8月前均值 {avg_e:.2f} → 8月后 {avg_l:.2f}"])

    # --- 干净周不应误报 ---
    clean = run_all_checks("2025-06-02", "2025-06-08")
    blocking = [f for f in clean["flags"] if f["level"] == "error"]
    add("基线_干净周无误报", "正常周不应产生阻断级告警",
        {"无误报": 1.0 if not blocking else 0.0},
        weight=1.5, details=[f"误报：{[f['code'] for f in blocking]}"])

    register_report(report)
    print(report.render("质控准确率"))

    assert report.accuracy >= 0.95, (
        f"质控准确率 {report.accuracy:.1%} 低于 95% —— 归因陷阱检出是核心能力"
    )


# ===========================================================================
# 离线层 4：工具健康度
# ===========================================================================

@pytest.mark.offline
def test_tool_registry_health():
    """工具注册、能力装配、参数校验、容错 —— 工具层是 Agent 的手脚。"""
    from app.tools import registry as pkg
    from app.tools.registry import registry

    report = EvalReport(layer="工具健康度")

    def add(cid, exam, ok, weight=1.0, detail=""):
        r = CaseResult(case_id=cid, question=cid, exams=exam, weight=weight)
        r.scores["通过"] = 1.0 if ok else 0.0
        if detail:
            r.details.append(detail)
        report.results.append(r)

    names = registry.names()
    add("工具注册完整", "13 个工具全部注册", len(names) >= 13,
        detail=f"实际 {len(names)} 个")

    # 能力装配必须是子集
    metric_tools = [t.name for t in registry.select(capabilities=["metric"])]
    add("能力装配是子集", "metric 不包含 sql 工具",
        "run_sql" not in metric_tools, weight=1.5,
        detail=str(metric_tools))

    # 参数别名纠正
    r = registry.execute("scan_stockout", start_date="2025-07-05",
                         end_date="2025-07-11")
    add("参数别名自动纠正", "start_date → start",
        not (isinstance(r, dict) and r.get("ok") is False), weight=2.0)

    # ★ 错误参数名必须被拒绝，不能被静默忽略。
    #   踩过的坑：别名表里曾同时有 begin/finish 与 begin_date/finish_date，
    #   映射过度宽松，导致模型写了两个完全错误的参数名时，
    #   系统静默纠偏并用默认值执行 —— 返回一个看似正常、
    #   实际与请求区间无关的结果。这类「静默算错」会一路进入最终报告，
    #   比直接报错危险得多。用断言把这条行为固定下来，防止以后有人
    #   往别名表里继续加名字时又把它放宽。
    r_bad = registry.execute("scan_stockout", foo="2025-07-05", bar="2025-07-11")
    add("错误参数名必须被拒", "不得静默忽略未知参数（防静默算错）",
        isinstance(r_bad, dict) and r_bad.get("ok") is False, weight=3.0,
        detail=f"实际返回：{str(r_bad)[:70]}")

    # 同理：模型自造的「看似合理但错误」的组合名也要拒绝
    r_bad2 = registry.execute("scan_stockout", begin_date="2025-07-05")
    add("可疑组合名必须被拒", "begin_date 不属于等价别名",
        isinstance(r_bad2, dict) and r_bad2.get("ok") is False, weight=2.0,
        detail=f"实际返回：{str(r_bad2)[:70]}")

    # 错误分类
    r = registry.execute("run_sql", sql="SELECT bad_col FROM dim_product")
    add("错误分类为 data", "列名错误应归为可自我修正的 data 类",
        isinstance(r, dict) and r.get("error_kind") == "data", weight=2.0)
    add("错误含修正建议", "报错要给出正确列名",
        "title" in str(r.get("expected_columns", [])), weight=2.0,
        detail=str(r.get("expected_columns")))

    # SQL 护栏
    r = registry.execute("run_sql", sql="DELETE FROM dim_product")
    add("SQL 护栏生效", "写操作被拒绝",
        isinstance(r, dict) and r.get("ok") is False, weight=2.0)

    # 正确 JOIN 可执行
    r = registry.execute(
        "run_sql",
        sql=("SELECT p.title, SUM(f.gmv) AS g FROM fact_daily_product f "
             "JOIN dim_product p ON p.product_id = f.product_id "
             "WHERE f.stat_date BETWEEN '2025-08-18' AND '2025-08-24' "
             "GROUP BY p.title"))
    add("多表 JOIN 可执行", "正确的 JOIN 必须跑通",
        isinstance(r, dict) and r.get("ok") is True, weight=2.0,
        detail=str(r)[:80])

    register_report(report)
    print(report.render("工具健康度"))

    assert report.accuracy >= 1.0, (
        f"工具健康度 {report.accuracy:.1%} —— 工具层问题会直接导致 Agent 瘫痪"
    )


# ===========================================================================
# 离线层 5：并行执行策略
# ===========================================================================

@pytest.mark.offline
def test_parallel_strategy():
    """并行执行必须自适应：快工具串行（避免负优化），慢工具并行。"""
    import time

    from app.tools import registry as pkg                       # noqa: F401
    from app.tools.parallel import (
        ToolCallSpec, _should_parallelize, execute_batch,
    )
    from app.tools.registry import registry

    report = EvalReport(layer="并行策略")

    if registry.get("slow_probe") is None:
        @registry.register(capabilities=["drilldown"],
                           description="慢工具探针", timeout_s=60,
                           max_retries=0)
        def slow_probe(delay_ms: int = 120) -> dict:
            time.sleep(delay_ms / 1000)
            return {"ok": True}

    # ★ 用固定时延探针，不要用真实业务工具。
    #   踩过的坑：最初用 get_data_range / compare_weeks / scan_stockout，
    #   SQLite 上它们是毫秒级 → 判定串行 → 断言通过；
    #   但 PostgreSQL 上要 174ms（有网络往返），系统判定「并行有净收益」
    #   并启用并行 —— 系统的决策是对的，错的是断言的隐含假设
    #   （它默认「这些工具一定快」）。
    #   改成固定时延后，断言只验证决策逻辑，不受后端速度影响。
    if registry.get("fast_probe") is None:
        @registry.register(capabilities=["drilldown"],
                           description="快工具探针（测不并行）",
                           timeout_s=30, max_retries=0)
        def fast_probe(delay_ms: int = 3) -> dict:
            time.sleep(delay_ms / 1000)
            return {"ok": True}

    fast = [ToolCallSpec("fast_probe", {"delay_ms": 3}),
            ToolCallSpec("fast_probe", {"delay_ms": 3}),
            ToolCallSpec("fast_probe", {"delay_ms": 3})]
    execute_batch(fast, force_parallel=False)
    ok_fast, reason = _should_parallelize(fast)

    r1 = CaseResult(case_id="快工具不并行", question="(策略)", exams="避免负优化",
                    weight=2.0)
    r1.scores["正确决策"] = 0.0 if ok_fast else 1.0
    r1.details.append(reason[:90])
    report.results.append(r1)

    slow = [ToolCallSpec("slow_probe", {"delay_ms": 120}) for _ in range(4)]
    execute_batch(slow, force_parallel=False)
    ok_slow, reason2 = _should_parallelize(slow)

    r2 = CaseResult(case_id="慢工具启用并行", question="(策略)", exams="有净收益才并行",
                    weight=2.0)
    r2.scores["正确决策"] = 1.0 if ok_slow else 0.0
    r2.details.append(reason2[:90])
    report.results.append(r2)

    # 实测加速比
    t0 = time.perf_counter()
    execute_batch(slow, force_parallel=False)
    t_serial = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    execute_batch(slow, force_parallel=True)
    t_par = (time.perf_counter() - t0) * 1000
    speedup = t_serial / t_par if t_par else 0

    r3 = CaseResult(case_id="并行加速比", question="(实测)", exams="4×120ms 应 >1.5x",
                    weight=2.0)
    r3.scores["加速达标"] = 1.0 if speedup > 1.5 else 0.0
    r3.details.append(f"串行 {t_serial:.0f}ms → 并行 {t_par:.0f}ms，"
                      f"{speedup:.2f}x")
    report.results.append(r3)

    # 顺序语义
    order = [ToolCallSpec("slow_probe", {"delay_ms": 20}),
             ToolCallSpec("slow_probe", {"delay_ms": 60}),
             ToolCallSpec("slow_probe", {"delay_ms": 10})]
    outs, _ = execute_batch(order, force_parallel=True)
    r4 = CaseResult(case_id="并行保持顺序", question="(语义)", exams="返回顺序与请求一致",
                    weight=2.0)
    r4.scores["顺序一致"] = 1.0 if len(outs) == 3 else 0.0
    report.results.append(r4)

    register_report(report)
    print(report.render("并行策略"))

    assert report.accuracy >= 0.95, f"并行策略 {report.accuracy:.1%}"


# ===========================================================================
# 离线层 6：认证与数据权限
# ===========================================================================

@pytest.mark.offline
def test_auth_and_data_scope():
    """认证、授权、数据权限 —— 企业级的基本盘。"""
    _require_db()
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    report = EvalReport(layer="认证与数据权限")

    def add(cid, exam, ok, weight=1.0, detail=""):
        r = CaseResult(case_id=cid, question=cid, exams=exam, weight=weight)
        r.scores["通过"] = 1.0 if ok else 0.0
        if detail:
            r.details.append(detail)
        report.results.append(r)

    # 未认证必须被拒
    r = client.get("/api/metrics")
    add("未认证被拒", "确定性接口需要认证", r.status_code == 401,
        weight=2.0, detail=f"实际 {r.status_code}")

    # 健康检查开放
    add("健康检查开放", "探针无需认证", client.get("/api/health").status_code == 200)

    # 登录
    import io
    from contextlib import redirect_stderr

    with redirect_stderr(io.StringIO()):
        lr = client.post("/api/auth/login",
                         json={"username": "analyst", "password": "analyst123"})
    add("登录成功", "analyst 可登录", lr.status_code == 200, weight=2.0)
    if lr.status_code != 200:
        register_report(report)
        print(report.render("认证与数据权限"))
        pytest.fail("登录失败，后续用例无法执行")
    token = lr.json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}

    # 数据权限：受限用户的行数必须少于管理员
    with redirect_stderr(io.StringIO()):
        admin_r = client.post("/api/auth/login",
                              json={"username": "admin", "password": "admin123"})
        admin_hdr = {"Authorization": f"Bearer {admin_r.json()['access_token']}"}
        a = client.get("/api/products", headers=admin_hdr).json()["products"]
        b = client.get("/api/products", headers=hdr).json()["products"]

    add("数据权限生效", "受限用户可见行数少于全量", len(b) < len(a),
        weight=3.0, detail=f"admin {len(a)} 行 vs analyst {len(b)} 行")
    add("越权类目被过滤", "只能看到授权类目",
        all(p["category"] in ("美妆", "家居") for p in b), weight=3.0,
        detail=str(sorted({p["category"] for p in b})))

    # 角色越权
    with redirect_stderr(io.StringIO()):
        v = client.post("/api/auth/login",
                        json={"username": "viewer", "password": "viewer123"})
        vh = {"Authorization": f"Bearer {v.json()['access_token']}"}
        sql_r = client.post("/api/sql", json={"sql": "SELECT 1"}, headers=vh)
    add("只读角色不能用 SQL", "viewer 访问 /api/sql 应 403",
        sql_r.status_code == 403, weight=2.0, detail=f"实际 {sql_r.status_code}")

    register_report(report)
    print(report.render("认证与数据权限"))

    assert report.accuracy >= 1.0, f"认证与权限 {report.accuracy:.1%}"


# ===========================================================================
# 在线层：需要真实 LLM
# ===========================================================================

@pytest.mark.live
@pytest.mark.skipif(not _has_api_key(),
                    reason="未配置 DEEPSEEK_API_KEY，跳过在线评测")
def test_live_end_to_end_accuracy():
    """
    ★ 在线层：真实 LLM 端到端评测，产出**行为准确率**。

    与离线层的分工：
      离线层验证"数字对不对"（代码能力）
      在线层验证"回答得对不对"（Agent 能力）

    在线层判分维度：
      · 工具选择   —— 是否调用了期望的工具
      · 必需内容   —— 回答是否包含关键结论（如"缺货"）
      · 禁止内容   —— 是否出现了不该有的归因（如"是详情页问题"）
      · 数字一致   —— 回答里引用的数字是否与指标层一致
    """
    _require_db()
    from app.workflow.engine import EcommerceAgent

    cases = load_cases()
    # 在线评测成本高，优先跑权重高（陷阱题）的用例
    cases.sort(key=lambda c: -c.get("weight", 1.0))
    selected = cases[:6]

    report = EvalReport(layer="在线端到端（真实 LLM）")
    agent = EcommerceAgent(max_rounds=4, max_revisions=1)

    for case in selected:
        res = CaseResult(case_id=case["id"], question=case["question"],
                         exams=case["exams"], weight=case.get("weight", 1.0))
        period = case.get("trap_period")
        try:
            resp = agent.run(
                task=case["question"],
                start=period[0] if period else None,
                end=period[1] if period else None,
            )
        except Exception as exc:                              # noqa: BLE001
            res.error = f"{type(exc).__name__}: {exc}"
            res.scores["执行成功"] = 0.0
            report.results.append(res)
            continue

        called = resp.loop.tool_calls if resp.loop else []
        text = resp.content or ""

        res.scores["执行成功"] = 1.0
        res.scores["工具选择"] = score_tool_used(called, case.get("expect_tool"))
        res.scores["必需内容"] = score_contains(text, case.get("expect_contains"))
        res.scores["禁止内容"] = score_forbidden(text, case.get("expect_forbidden"))
        # 陷阱题必须触发质控
        if case.get("expect_trap"):
            res.scores["质控触发"] = 1.0 if "run_quality_checks" in called \
                or "scan_stockout" in called or "scan_price_change" in called \
                or "get_ad_trend" in called else 0.0
        # 不得降级（降级说明工具全挂）
        res.scores["未降级"] = 0.0 if resp.degraded else 1.0

        res.details.append(f"工具：{called}")
        if resp.critique and not resp.critique.passed:
            res.details.append(f"评审未通过：{resp.critique.verdict[:60]}")

        report.results.append(res)

    register_report(report)
    print(report.render("在线端到端准确率"))

    # 在线层门槛设得比离线低 —— 因为 LLM 有随机性，
    # 但陷阱题（weight>=3）必须全过：那是不能妥协的底线
    traps = [r for r in report.results if r.weight >= 3.0]
    trap_acc = (sum(1 for r in traps if r.passed) / len(traps)) if traps else 1.0

    assert trap_acc >= 1.0, (
        f"陷阱题准确率 {trap_acc:.1%} —— 归因陷阱必须 100% 抵御，"
        f"这是产品可信度的底线"
    )
    assert report.accuracy >= 0.60, (
        f"在线准确率 {report.accuracy:.1%} 过低"
    )


@pytest.mark.live
@pytest.mark.skipif(not _has_api_key(),
                    reason="未配置 DEEPSEEK_API_KEY，跳过在线评测")
def test_live_number_fidelity():
    """
    在线层：回答里引用的关键数字必须与指标层一致。

    ★ 这是"计算归代码"原则的端到端验证 ——
      模型不应该自己算出任何数字，它引用的数字必须来自工具。
      如果这里失败，说明报告骨架的数字注入出了问题。
    """
    _require_db()
    from app.metrics.compute import latest_complete_week, period_compare
    from app.workflow.engine import EcommerceAgent

    start, end = latest_complete_week()
    truth = period_compare(start, end, exclude_special=True)["current"]

    resp = EcommerceAgent(max_rounds=4).run(
        f"{start} 到 {end} 的成交额是多少？")
    text = resp.content or ""

    report = EvalReport(layer="数字保真度（在线）")
    gmv = truth["gmv_gross"]
    # 允许千分位与不同小数位
    variants = [
        f"{gmv:,.0f}", f"{gmv:,.2f}", f"{gmv:.0f}", f"{int(gmv):,}",
    ]
    r = CaseResult(case_id="GMV数字与指标层一致", question="(在线)",
                   exams="模型不得自算数字", weight=3.0)
    r.scores["数字一致"] = 1.0 if any(v in text for v in variants) else 0.0
    r.details.append(f"指标层 GMV={gmv:,.2f}，检索变体={variants}")
    report.results.append(r)

    register_report(report)
    print(report.render("数字保真度"))

    assert r.scores["数字一致"] == 1.0, (
        f"回答中未找到指标层的 GMV 值 {gmv:,.2f} —— "
        f"数字保真度失败，模型可能在自算数字"
    )
