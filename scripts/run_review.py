"""
scripts/run_review.py
=====================
命令行运行周复盘，把报告输出为 Markdown 文件。

用法：
    python scripts/run_review.py                   # 复盘中最后一个完整自然周
    python scripts/run_review.py 2025-07-01 2025-07-07
    python scripts/run_review.py --focus "转化率下滑"
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings            # noqa: E402
from app.llm import LLMNotConfigured       # noqa: E402
from app.metrics.compute import latest_complete_week  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    focus = None
    if "--focus" in args:
        i = args.index("--focus")
        focus = args[i + 1]
        args = args[:i] + args[i + 2:]

    start = args[0] if len(args) > 0 else None
    end = args[1] if len(args) > 1 else None

    if not (start and end):
        start, end = latest_complete_week()

    print(f"复盘区间：{start} ~ {end}")
    if focus:
        print(f"关注点：{focus}")
    print("-" * 70)

    try:
        from app.services.weekly_review import run_weekly_review
        review = run_weekly_review(start=start, end=end, focus=focus)
    except LLMNotConfigured as e:
        print("\n[无法运行] " + str(e))
        print("\n提示：数据层与指标层不依赖 LLM，可先运行：")
        print("    python scripts/verify_foundation.py")
        return 2
    except Exception as e:
        print(f"\n[运行失败] {type(e).__name__}: {e}")
        return 1

    # 输出报告文件
    out_dir = Path(settings.DB_PATH).parent.parent / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / f"weekly_review_{start}_{end}.md"
    out_file.write_text(review.narrative, encoding="utf-8")

    print()
    print(f"标题：{review.headline}")
    print(f"归因：{len(review.insights)} 条")
    print(f"建议：{len(review.actions)} 条")
    print(f"评审：{'通过' if review.critique and review.critique.passed else '未通过'}"
          f"（重做 {review.revision_rounds} 轮）")
    if review.critique:
        print(f"       {review.critique.verdict}")
        for p in review.critique.problems[:5]:
            print(f"       - {p}")
    print()
    print(f"报告已写入：{out_file}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
