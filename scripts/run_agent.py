"""
scripts/run_agent.py
====================
命令行调用 EC-Monica。

用法：
    # 自动意图识别（推荐）
    python scripts/run_agent.py "上周的 GMV 是多少"
    python scripts/run_agent.py "转化率为什么下滑了"
    python scripts/run_agent.py "拆解这个需求：加一个加购转化率指标"

    # 指定分析区间
    python scripts/run_agent.py "分析这段时间的经营情况" --start 2025-07-01 --end 2025-07-07

    # 需求解析（结构化输出）
    python scripts/run_agent.py --requirement "订单状态要加一个部分退款状态"

    # 查看已注册工具与意图路由表
    python scripts/run_agent.py --tools
    python scripts/run_agent.py --routes

    # 只跑确定性部分（不需要 API Key）
    python scripts/run_agent.py --dry-run "上周GMV多少"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings          # noqa: E402


def print_progress(round_no: int, msg: str) -> None:
    print(f"    [进度] {msg}")


def cmd_tools() -> int:
    """列出已注册工具。"""
    from app.tools import registry

    print("=" * 78)
    print(f"已注册工具 {len(registry.names())} 个")
    print("=" * 78)
    for t in registry.all():
        print(f"\n■ {t.name}")
        print(f"    能力标签 : {t.capabilities}")
        print(f"    超时/重试: {t.timeout_s}s / {t.max_retries} 次")
        print(f"    说明     : {t.description[:150]}")
    return 0


def cmd_routes() -> int:
    """打印意图路由表。"""
    from app.workflow.intent import ROUTING, classify_by_rules

    print("=" * 78)
    print("意图路由表（意图 → 是否需要工具 / 装配能力 / 路由目标）")
    print("=" * 78)
    for intent, spec in ROUTING.items():
        print(f"\n■ {intent.value}")
        print(f"    需要工具 : {spec['need_tools']}")
        print(f"    装配能力 : {spec['capabilities'] or '（不装配，直接生成）'}")
        print(f"    路由目标 : {spec['route']}")
        print(f"    输出模板 : {spec['template']}")

    print()
    print("=" * 78)
    print("规则识别命中测试")
    print("=" * 78)
    samples = [
        "上周 GMV 是多少", "转化率为什么下滑", "拆解这个需求文档",
        "写个 618 促销文案", "随便聊聊天", "帮我看看这个",
    ]
    for s in samples:
        r = classify_by_rules(s)
        tag = f"{r.intent.value}（{r.confidence:.2f}）" if r else "规则未命中 → 交 LLM 兜底"
        print(f"    {s:<22} → {tag}")
    return 0


def cmd_dry_run(task: str) -> int:
    """
    只跑确定性部分：意图识别 + 数据预处理 + 质控。

    不需要 API Key —— 用于快速确认"数据侧是否正常"。
    """
    from app.metrics.compute import latest_complete_week, period_compare
    from app.quality.checks import run_all_checks
    from app.workflow.intent import classify_by_rules
    from app.workflow.preprocess import enrich_period_compare

    print(f"任务：{task}")

    print("\n【意图识别（规则）】")
    r = classify_by_rules(task)
    if r:
        print(f"  意图     : {r.intent.value}")
        print(f"  置信度   : {r.confidence}")
        print(f"  需要工具 : {r.need_tools}")
        print(f"  装配能力 : {r.capabilities}")
        print(f"  依据     : {r.reason}")
    else:
        print("  规则未命中，实际运行时会交给 LLM 兜底判定")

    start, end = latest_complete_week()
    print(f"\n【数据预处理（{start} ~ {end}）】")
    enriched = enrich_period_compare(period_compare(start, end))
    print(f"  摘要：{enriched['摘要']}")

    print("\n【数据质控】")
    qc = run_all_checks(start, end)
    print(f"  has_blocking = {qc['has_blocking']}")
    for f in qc["flags"]:
        print(f"    [{f['level']}] {f['code']}: {f['message'][:100]}")

    print("\n（dry-run 结束。以上均为确定性结果，未经 LLM 加工。）")
    return 0


def main() -> int:
    args = sys.argv[1:]

    if "--tools" in args:
        return cmd_tools()
    if "--routes" in args:
        return cmd_routes()

    # 解析 --start / --end / --focus
    def take(flag: str) -> str | None:
        if flag in args:
            i = args.index(flag)
            val = args[i + 1] if i + 1 < len(args) else None
            del args[i:i + 2]
            return val
        return None

    start = take("--start")
    end = take("--end")
    focus = take("--focus")
    is_req = "--requirement" in args
    dry = "--dry-run" in args
    if is_req:
        args.remove("--requirement")
    if dry:
        args.remove("--dry-run")

    task = " ".join(args).strip()
    if not task:
        print(__doc__)
        return 1

    if dry:
        return cmd_dry_run(task)

    # 快速失败提示
    if not settings.has_api_key:
        print("[无法运行] 未配置 DEEPSEEK_API_KEY。")
        print()
        print("  可选做法：")
        print("    1. 复制 .env.example 为 .env 并填入 Key；")
        print("    2. 用 --dry-run 只跑确定性部分（不需要 Key）；")
        print("    3. 直接调用确定性接口：/api/metrics、/api/quality。")
        return 2

    print("=" * 78)
    print(f"任务：{task}")
    if start and end:
        print(f"区间：{start} ~ {end}")
    print("=" * 78)

    if is_req:
        from app.workflow.requirement import analyze_requirement
        import json

        print("\n执行中（需求解析）...\n")
        result = analyze_requirement(task)
        if result.get("ok"):
            print(json.dumps(result["analysis"], ensure_ascii=False, indent=2))
        else:
            print(f"解析失败：{result.get('error')}")
            print(result.get("hint", ""))
        print(f"\n执行信息：{json.dumps(result.get('loop', {}), ensure_ascii=False)}")
        return 0

    from app.workflow.engine import EcommerceAgent

    agent = EcommerceAgent()
    print()
    resp = agent.run(task, focus=focus, start=start, end=end,
                     on_progress=print_progress)

    print()
    print("=" * 78)
    print("输出")
    print("=" * 78)
    print(resp.content)

    print()
    print("=" * 78)
    print("执行元信息（可观测性）")
    print("=" * 78)
    for k, v in resp.meta().items():
        print(f"  {k:<20}: {str(v)[:90]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
