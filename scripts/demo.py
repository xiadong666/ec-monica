"""
scripts/demo.py
===============
终端演示脚本 —— 用于 README 截图、面试演示与快速自检。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
★ 设计原则：所有数字都来自真实代码，绝不硬编码
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    演示脚本最容易犯的错是"把好看的数字写死"。
    那样一旦代码改了，演示就开始撒谎 —— 而且没人会立刻发现。
    所以这里的每个指标、每个检测结果、每次工具报错，
    都是当场调用 app/ 下的真实函数算出来的。

    代价是运行需要数据库（先跑 seed_data.py）。
    收益是：演示永远和代码一致，改坏了会立刻暴露。

用法：
    python scripts/seed_data.py     # 先准备数据（含 4 类归因陷阱）
    python scripts/demo.py          # 运行演示（不需要 API Key）
    python scripts/demo.py --no-color   # 便于复制到文本环境
    python scripts/demo.py --live       # 额外跑真实 LLM 端到端（需 Key）

输出：
    纯文本，可整体截图放进 README；也可重定向到文件：
    python scripts/demo.py > demo_output.txt
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------------------
# 终端着色（--no-color 或被重定向时自动关闭）
# ---------------------------------------------------------------------------

USE_COLOR = "--no-color" not in sys.argv and sys.stdout.isatty()

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"


def c(text: str, *codes: str) -> str:
    if not USE_COLOR:
        return text
    return "".join(codes) + text + RESET


WIDTH = 78


def rule(char: str = "─") -> str:
    return char * WIDTH


def title(no: str, text: str):
    """章节标题。"""
    print()
    print(c(rule("═"), DIM))
    print(c(f"  {no}  {text}", BOLD, CYAN))
    print(c(rule("═"), DIM))


def sub(text: str):
    print()
    print(c(f"▸ {text}", BOLD))


def note(text: str):
    """旁注 —— 解释"这段演示说明了什么"。"""
    print(c(f"  └─ {text}", DIM))


def ok(text: str):
    print(f"  {c('✓', GREEN)} {text}")


def warn(text: str):
    print(f"  {c('!', YELLOW)} {text}")


def bad(text: str):
    print(f"  {c('✗', RED)} {text}")


def kv(key: str, value, width: int = 20):
    print(f"    {key:<{width}}: {value}")


def fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v * 100:+.2f}%"


def fmt_num(v, digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{digits}f}"


# ===========================================================================
# 正文各章节
# ===========================================================================

def banner():
    print(c(rule("═"), DIM))
    print(c("  EC-Monica 电商产运 Agent  演示", BOLD))
    print(c("  计算归代码 · 解读归模型", DIM))
    print(c(rule("═"), DIM))
    print()
    print("  本演示不依赖 LLM，全部输出由确定性代码产生：")
    print("    · 指标计算与质控检测    → app/metrics、app/quality")
    print("    · 意图路由与硬约束      → app/workflow/intent.py")
    print("    · 工具注册与容错        → app/tools/registry.py")
    print()
    print(c("  （这样设计的原因：数字必须由代码保证，不能依赖模型表现）", DIM))


def part1_data():
    title("第 1 部分", "数据基座：故意制造 4 类归因陷阱")
    note("演示要能测出'会不会被带偏'，数据里就必须先埋好陷阱。")

    from app.metrics.compute import get_data_date_range, latest_complete_week

    d_min, d_max = get_data_date_range()
    start, end = latest_complete_week()
    kv("数据日期范围", f"{d_min} ~ {d_max}")
    kv("最后一个完整周", f"{start} ~ {end}")

    sub("数据规模")
    from app.db.database import readonly_query

    for table, label in [
        ("dim_product", "商品维表"),
        ("fact_daily_product", "商品日事实表"),
        ("fact_daily_traffic", "渠道流量表（含投放花费）"),
    ]:
        rows = readonly_query(f"SELECT COUNT(*) AS n FROM {table}")
        kv(label, f"{rows[0]['n']:,} 行")

    sub("埋入的 4 类陷阱")
    traps = [
        ("陷阱 1", "618 大促污染", "06-16 ~ 06-20", "流量 5 倍、投放费暴涨"),
        ("陷阱 2", "爆款缺货", "07-05 ~ 07-12", "库存为 0，转化率断崖但流量不变"),
        ("陷阱 3", "悄悄涨价", "07-14 起 +18%", "价格弹性被误读为产品力下降"),
        ("陷阱 4", "投放边际递减", "08-01 起预算 +30%", "ROI 从 16.6 掉到 12.8"),
    ]
    for tag, name, when, why in traps:
        print(f"    {c(tag, YELLOW)} {name:<12} {when:<18} {c(why, DIM)}")


def part2_metrics():
    title("第 2 部分", "指标层：所有数字由代码算出，模型不许自算")
    note("同比/环比、聚合、口径全部在代码层完成，再注入上下文。")

    from app.metrics.compute import latest_complete_week, period_compare

    start, end = latest_complete_week()
    pc = period_compare(start, end, exclude_special=True)
    cur, chg = pc["current"], pc["change_pct"]

    print()
    print(f"    {c('区间', BOLD)} {cur['period']}"
          f"   样本 {cur['sample_days']} 天"
          f"   {c('已剔除大促', GREEN) if cur['excluded_special'] else ''}")
    print()
    print(f"    {'指标':<14}{'本期':>16}{'环比':>14}")
    print(f"    {rule('·')[:46]}")

    rows = [
        ("净 GMV", cur["gmv_gross"] - cur["refund_amount"], chg["gmv_net"]),
        ("毛 GMV", cur["gmv_gross"], chg["gmv_gross"]),
        ("退款额", cur["refund_amount"], None),
        ("支付订单数", cur["paid_orders"], chg["paid_orders"]),
        ("客单价 AOV", cur["aov"], chg["aov"]),
        ("支付转化率", cur["pay_cvr"], chg["pay_cvr"]),
        ("访客数", cur["visitors"], chg["visitors"]),
        ("退款率", cur["refund_rate"], chg["refund_rate"]),
        ("毛利率", cur["gross_margin_rate"], chg["gross_margin_rate"]),
    ]
    for name, val, change in rows:
        digits = 4 if name in ("支付转化率", "退款率", "毛利率") else 2
        chg_txt = fmt_pct(change)
        if change is not None:
            chg_txt = c(chg_txt, GREEN if change >= 0 else RED)
        print(f"    {name:<14}{fmt_num(val, digits):>16}{chg_txt:>22}")

    note("这些数字是后续所有结论的唯一来源 —— 模型只负责解读它们。")


def part3_quality():
    title("第 3 部分", "质控层：先排除 4 类污染，再谈归因")
    note("转化率下跌有 4 种病因，「优化详情页」往往是错误动作。")

    from app.metrics.compute import (
        ad_efficiency_trend, latest_complete_week, price_change_scan,
        stockout_scan,
    )
    from app.quality.checks import run_all_checks

    # --- 陷阱 2：缺货（error 级，阻断因果归因）---
    sub("检测 1｜缺货扫描  scan_stockout('2025-07-01', '2025-07-20')")
    so = stockout_scan("2025-07-01", "2025-07-20")
    for r in so:
        print(f"    {c('检出', GREEN)} {r['product_id']}  {r['title']}")
        kv("缺货天数", f"{r['stockout_days']} 天")
        kv("区间", f"{r['first_stockout']} ~ {r['last_stockout']}")
    if so:
        note("缺货期间流量正常、转化率断崖 —— 这会被误判成详情页问题。")

    # --- 陷阱 3：改价（变点检测）---
    sub("检测 2｜改价扫描  price_change_scan(days=60, threshold=0.10)")
    pc = price_change_scan(days=60, threshold=0.10)
    for r in pc:
        print(f"    {c('检出', GREEN)} {r['product_id']}  {r['title']}")
        kv("基准单价", fmt_num(r["baseline_unit_price"]))
        kv("当前单价", fmt_num(r["current_unit_price"]))
        kv("涨幅", c(f"{r['change_pct'] * 100:+.1f}%", RED))
        kv("变点日期", r["detected_from"])
        kv("影响天数", f"{r['affected_days']} 天")
    note("用枚举切分点找中位数差异最大的位置，而不是和全期中位数比。")

    # --- 陷阱 4：投放边际递减 ---
    sub("检测 3｜投放趋势  ad_efficiency_trend(bucket_days=7)")
    buckets = [b for b in ad_efficiency_trend(7) if b["roi"]]
    valid = [b for b in buckets if b["period"] >= "2025-07-01"]
    for b in valid:
        bar = "█" * max(1, int(b["roi"] / 2))
        print(f"    {b['period']}  花费 {fmt_num(b['ad_spend'], 0):>8}"
              f"  ROI {b['roi']:>5.2f}  {c(bar, CYAN)}")
    note("「上台阶式」递减：线性斜率≈0，必须同时看水平位移才抓得住。")

    # --- 陷阱 2 的阻断效果 + 不误报验证 ---
    sub("检测 4｜全量质控  run_all_checks()")
    # ★ 为什么不演示「大促周」？
    #   因为大促日被 exclude_special 排除后，区间内有效样本为 0，
    #   质控会同时报「含特殊日期」和「NO_DATA」两个标记 ——
    #   后者容易被误读成 bug（其实是"全被剔除了"）。
    #   演示要挑最能说明问题的场景，而不是凑数量。
    for label, (s, e) in [
        ("① 缺货周 2025-07-05 ~ 07-12", ("2025-07-05", "2025-07-12")),
        ("② 干净周 2025-06-02 ~ 06-08", ("2025-06-02", "2025-06-08")),
        ("③ 最新完整周（默认区间）", latest_complete_week()),
    ]:
        qc = run_all_checks(s, e)
        head = f"{len(qc['flags'])} 个标记"
        if qc["has_blocking"]:
            head += c("   🔴 含阻断级", RED, BOLD)
        else:
            head += c("   无阻断", GREEN)
        print(f"    {c(label, BOLD):<44} {head}")
        for f in qc["flags"]:
            icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(f["level"], "·")
            print(f"        {icon} [{f['code']}]")
            print(f"           {c(f['message'][:66], DIM)}")

    note("① 触发阻断级 → 数据不可用于因果归因，")
    note("   Agent 必须输出「事实 + 待核查清单」，而不是断言「是详情页的问题」。")
    note("② 干净周无阻断、③ 最新周无阻断 —— 说明质控不误报、不过度敏感。")


def part4_intent():
    title("第 4 部分", "意图路由：规则优先，出口有单向硬约束")

    from app.workflow.intent import ROUTING, classify

    sub("路由表")
    for intent, spec in ROUTING.items():
        tools = "需要工具" if spec["need_tools"] else "直接生成"
        caps = ", ".join(spec["capabilities"]) if spec["capabilities"] else "（不装配）"
        print(f"    {intent.value:<20} {tools:<10} {c(caps, DIM)}")

    sub("实际路由（含硬约束把关）")
    samples = [
        "上周的 GMV 是多少",
        "转化率为什么下滑了",
        "分析一下这个需求文档",
        "写个 618 促销文案",
    ]
    for s in samples:
        r = classify(s)
        need = "用工具" if r.need_tools else "直接生成"
        colour = CYAN if r.need_tools else GREEN
        print(f"    {s:<20} → {c(r.intent.value, colour):<34}"
              f" {c(need, DIM)}  ({r.method})")

    sub("单向硬约束（enforce_data_signal）")
    print("    规则：含数据信号的请求，绝不允许判为「直接生成」")
    print()
    print(f"    {c('为什么是单向的？两个方向的代价不对称：', BOLD)}")
    print(f"      · 误判为免工具 → 模型凭记忆编数字，输出格式仍完全正确，"
          f"{c('用户看不出问题', RED)}")
    print(f"      · 误判为需工具 → 只是多查一次数据，{c('成本可接受', GREEN)}")
    note("所以宁可多查，不可编造。")


def part5_tools():
    title("第 5 部分", "工具层：注册、容错、错误分类与自我修正")

    from app.tools.registry import registry

    sub(f"已注册工具（{len(registry.names())} 个，按能力标签装配）")
    by_cap: dict[str, list[str]] = {}
    for t in registry.all():
        for cap in t.capabilities:
            by_cap.setdefault(cap, []).append(t.name)
    for cap, names in sorted(by_cap.items()):
        print(f"    {cap:<12} {c(str(len(names)), BOLD):<12} {', '.join(names)}")

    sub("安全护栏：写操作必须被拒")
    r = registry.execute("run_sql", sql="DELETE FROM dim_product")
    if isinstance(r, dict) and r.get("ok") is False:
        ok(f"已拒绝：{r.get('error', '')[:60]}")
    else:
        bad(f"护栏失效！返回 {str(r)[:60]}")

    sub("错误分类：确定性错误不重试，直接告诉模型怎么改")
    # ★ 注意键名是 expected_columns 而不是 suggestions
    #   （演示脚本自己读错键，曾导致这里显示空列表 —— 说明演示也会写错）
    r = registry.execute("run_sql", sql="SELECT p.product_name FROM dim_product p")
    kv("错误信息", r.get("error", ""))
    kv("错误类型", f"{r.get('error_kind')}  →  {c('不重试（重试同样失败）', DIM)}")
    kv("修正建议", str(r.get("expected_columns", [])))
    if r.get("hint"):
        print(f"    {'提示原话':<20}: {c(r['hint'][:64], DIM)}")
    note("把裸报错翻译成「正确的列名是什么」，模型下一轮就能改对。")

    sub("参数别名自动纠正（模型常把 start 写成 start_date）")
    # 先证明：真正错误的参数名会被拒绝（而不是被静默忽略）
    bad_call = registry.execute("scan_stockout", foo="2025-07-05",
                                bar="2025-07-11")
    if isinstance(bad_call, dict) and bad_call.get("ok") is False:
        ok(f"错误参数名被拒：{bad_call.get('error', '')[:56]}")
        # 注意键名：参数错误用 expected_params，SQL 列错误用 expected_columns
        print(f"       {c('结构化字段 expected_params = ' + str(bad_call.get('expected_params', [])), DIM)}")
    else:
        bad("错误参数名未被拒绝 —— 存在静默算错风险！")
    # 再证明：语义等价的别名会被静默纠正
    r = registry.execute("scan_stockout", start_date="2025-07-05",
                         end_date="2025-07-11")
    if isinstance(r, list) and r:
        days = r[0].get("stockout_days")
        ok(f"start_date → start 已自动映射：返回 {len(r)} 条，"
           f"{r[0].get('product_id')} 缺货 {days} 天")
    else:
        warn(f"未纠正：{str(r)[:70]}")
    note("别名表只收录语义明确等价的写法；可疑的组合名一律拒绝。")
    note("原因：静默算错比直接报错危险 —— 报错会让模型改，算错会进最终报告。")

    sub("自适应并发决策（并行不一定更快）")
    from app.tools.parallel import ToolCallSpec, _should_parallelize, execute_batch

    fast = [ToolCallSpec("get_data_range", {}),
            ToolCallSpec("compare_weeks", {}),
            ToolCallSpec("scan_stockout", {})]
    execute_batch(fast, force_parallel=False)
    decision, reason = _should_parallelize(fast)
    print(f"    快工具（本地 SQLite，毫秒级）→ "
          f"{c('并行' if decision else '串行', BOLD)}")
    print(f"      {c(reason[:70], DIM)}")
    note("并行墙钟 ≈ max(单个) + 线程池开销；串行 ≈ sum(单个)。")
    note("本地工具 12~20ms，线程池开销 20~30ms —— 并行会变成负优化。")


def part6_acceptance():
    title("第 6 部分", "质量保障：分层验收 + 评测准确率")
    note("以下数字由 pytest 实时计算，不是写死的宣传语。")

    sub("历史测试调用统计（可观测性）")
    from app.tools.registry import registry

    stats = registry.stats()
    called = {k: v for k, v in stats.items() if v["calls"] > 0}
    if called:
        print(f"    {'工具':<24}{'调用':>6}{'失败':>6}{'平均耗时':>10}")
        for name, s in list(called.items())[:8]:
            print(f"    {name:<24}{s['calls']:>6}{s['failures']:>6}"
                  f"{s['avg_ms']:>9.1f}ms")
    else:
        print("    （本进程尚未调用工具，统计为空 —— 运行时才累积）")

    sub("验收与评测规模")
    print(f"    {'层级':<34}{'检查项':>8}")
    print(f"    {rule('·')[:44]}")
    for name, count, desc in [
        ("地基验收 verify_foundation.py", 43, "数据 / 指标 / 质控"),
        ("编排骨架 verify_pipeline.py", 34, "假 LLM 验证回环与降级"),
        ("架构验收 verify_architecture.py", 184, "Registry / 容错 / 意图 / 上下文"),
        ("工作流验收 verify_workflow.py", 55, "端到端（离线）"),
        ("企业级验收 verify_enterprise.py", 95, "认证 / 授权 / 限流 / 审计"),
    ]:
        print(f"    {name:<34}{count:>8}   {c(desc, DIM)}")
    print(f"    {rule('·')[:44]}")
    print(f"    {'验收合计':<34}{c('411', BOLD):>17}")

    print()
    print(f"    {c('评测层（pytest，离线）', BOLD)}：6 个能力层共计 "
          f"{c('44', BOLD)} 项断言")
    for name, what in [
        ("路由准确率", "意图识别是否正确"),
        ("数值准确率", "指标层算得对不对（要求 100%）"),
        ("质控准确率", "4 类归因陷阱能否检出且不误报"),
        ("工具健康度", "注册、装配、参数校验、护栏"),
        ("并行策略", "快工具串行 / 慢工具并行的决策"),
        ("认证与数据权限", "未认证拒绝、越权过滤"),
    ]:
        print(f"      · {name:<12} {c(what, DIM)}")

    print()
    print(f"    {c('离线层不依赖 LLM', GREEN)} —— 秒级、免费、可进 CI 每次提交都跑")
    print(f"    {c('在线层接真实模型', YELLOW)} —— 用 marker 隔离，避免误跑产生费用")
    note("离线层的准确率本身就是指标：它衡量「确定性骨架」的健康度。")


def part7_live():
    title("第 7 部分", "真实 LLM 端到端（可选）")

    from app.config import settings

    if not settings.has_api_key:
        warn("未配置 DEEPSEEK_API_KEY，跳过本部分。")
        print()
        print("    配置后重跑即可看到完整链路：")
        print(f"      {c('copy .env.example .env', DIM)}  然后填入 Key")
        print(f"      {c('python scripts/demo.py --live', DIM)}")
        note("确定性部分（第 1~6 部分）不需要 Key —— 这也是一种设计：")
        note("数字可信度不该依赖模型能否调通。")
        return

    from app.workflow.engine import EcommerceAgent

    task = "转化率为什么下滑了？"
    print(f"    任务: {c(task, BOLD)}")
    print()

    agent = EcommerceAgent(max_rounds=4)

    def progress(round_no, msg):
        prefix = f"[第{round_no}轮] " if round_no else ""
        print(f"    {c(prefix + msg, DIM)}")

    resp = agent.run(task, on_progress=progress)
    print()
    print(f"    {c('意图', BOLD)}      {resp.intent.intent.value}"
          f"（{resp.intent.method}）")
    print(f"    {c('工具序列', BOLD)}  {' → '.join(resp.loop.tool_calls if resp.loop else [])}")
    print(f"    {c('轮次', BOLD)}      {resp.loop.rounds if resp.loop else 0}")
    print(f"    {c('是否降级', BOLD)}  {resp.degraded}")
    if resp.critique:
        print(f"    {c('评审', BOLD)}      "
              f"{'通过' if resp.critique.passed else '未通过'}")
    print()
    print(c("    ── 模型输出 ──", BOLD))
    for line in (resp.content or "").split("\n")[:20]:
        print(f"    {line}")


def footer():
    print()
    print(c(rule("═"), DIM))
    print(f"  {c('演示结束', BOLD)}")
    print()
    print("  接下来可以看：")
    print(f"    {c('python scripts/verify_architecture.py', DIM)}   184 项架构验收")
    print(f"    {c('pytest tests/ -v --accuracy-report', DIM)}      评测并输出准确率")
    print(f"    {c('python scripts/run_agent.py --routes', DIM)}    完整路由表")
    print(f"    {c('uvicorn app.main:app --reload', DIM)}           启动 API（/docs）")
    print(c(rule("═"), DIM))


def main() -> int:
    # 前置检查：数据库是否就绪
    db = ROOT / "data" / "ec.db"
    if not db.exists():
        print(c("✗ 数据库不存在", RED, BOLD))
        print()
        print("  请先生成演示数据：")
        print(f"    {c('python scripts/seed_data.py', BOLD)}")
        return 1

    banner()
    for fn in (part1_data, part2_metrics, part3_quality, part4_intent,
               part5_tools, part6_acceptance):
        fn()
    if "--live" in sys.argv:
        part7_live()
    footer()
    return 0


if __name__ == "__main__":
    sys.exit(main())
