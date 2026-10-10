"""
tests/conftest.py
=================
pytest 全局配置与共享 fixture。

★ 为什么 addoption / configure 钩子必须放在 conftest.py？
  因为 pytest 在**解析命令行参数**时就要知道有哪些自定义选项，
  而那时它还没有导入测试模块（test_*.py）。

  实测踩过的坑：把 pytest_addoption 写在 tests/test_eval.py 里，
  结果 `pytest --accuracy-report` 直接报
  "unrecognized arguments"，退出码 4（usage error）。
  钩子文件必须是被 pytest 提前加载的 conftest.py。

  这类问题的特征是：**代码看起来完全正确，但钩子从未被调用** ——
  因为它被放在了错误的生命周期阶段。
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# 命令行选项
# ---------------------------------------------------------------------------

def pytest_addoption(parser):
    parser.addoption(
        "--accuracy-report", action="store_true", default=False,
        help="在测试结束后输出完整准确率报告，并写入 reports/accuracy_*.json",
    )
    parser.addoption(
        "--min-accuracy", action="store", type=float, default=None,
        help="准确率门槛（0~1）。低于该值时 CI 失败。",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "offline: 不依赖 LLM 的离线评测")
    config.addinivalue_line("markers", "live: 需要真实 LLM 的在线评测")


# ---------------------------------------------------------------------------
# 准确率报告收集
# ---------------------------------------------------------------------------

# ★ 为什么用「环境变量 + JSON 文件」而不是模块级列表？
#
#   踩过的坑：最初用模块级 list（REPORTS）收集报告，
#   但 pytest 把 conftest.py 作为**顶层模块 `conftest`** 导入，
#   而测试模块用 `from tests.conftest import register_report` 导入 ——
#   这是**两个不同的模块对象**，各自持有一份 REPORTS，
#   于是测试注册的报告在汇总时完全看不到（表现为"报告为空，静默无输出"）。
#
#   根本原因：同一个文件通过不同路径被导入了两次。
#   解决办法：不依赖"同一个模块对象"，改用**进程级共享状态**。
#   这里用环境变量传报告 JSON 路径，用文件做中转 ——
#   它天然跨模块、跨进程，而且顺带留下可归档的产物。

_ACCURACY_DIR_ENV = "EC_ACCURACY_DIR"


def _accuracy_dir() -> Path:
    """报告输出目录（也可由环境变量指定，便于 CI 收集）。"""
    d = os.getenv(_ACCURACY_DIR_ENV, "")
    path = Path(d) if d else (ROOT / "reports")
    path.mkdir(parents=True, exist_ok=True)
    return path


def pytest_sessionstart(session):
    """
    ★ 测试开始前清理上一次运行的报告文件。

    踩过的坑：报告是落盘的，如果不清空，
    第二次运行时汇总会**把上一次的 layer_*.json 也读进来** ——
    表现为报告里出现重复的能力层，准确率被重复计算。

    这类"陈旧状态污染"的问题在 CI 里尤其隐蔽：
    因为 CI 每次都是干净环境，本地跑两次才会看到。
    所以清理必须显式做，不能依赖"环境是干净的"这个假设。
    """
    d = _accuracy_dir()
    removed = 0
    for f in list(d.glob("layer_*.json")) + list(d.glob("accuracy_summary.json")):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    if removed:
        print(f"\n[评测] 已清理上一次的报告文件 {removed} 个")


def register_report(report) -> None:
    """
    测试模块调用它注册报告。

    ★ 直接落盘而不是存内存：见上面的说明 ——
      内存态会因模块重复导入而丢失，落盘则不会。
      同时这也让 CI 能直接把 reports/ 目录作为 artifact 上传。
    """
    path = _accuracy_dir() / f"layer_{report.layer}.json"
    path.write_text(
        json.dumps(report.to_json(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _load_reports() -> list:
    """从磁盘读回所有已注册的报告。"""
    from tests.eval_runner import EvalReport

    reports = []
    for f in sorted(_accuracy_dir().glob("layer_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        reports.append(_report_from_json(data))
    return reports


def _report_from_json(data: dict):
    """
    从 JSON 还原成 EvalReport（用于统一渲染）。

    ★ 只还原渲染需要的字段，不还原完整对象 ——
      汇总阶段只需要 layer/passed/total/accuracy 这些统计量。
    """
    from tests.eval_runner import CaseResult, EvalReport

    rep = EvalReport(layer=data.get("layer", "unknown"))
    for c in data.get("cases", []):
        r = CaseResult(case_id=c.get("id", "?"), question="",
                       exams="", weight=c.get("weight", 1.0))
        r.scores = c.get("scores", {})
        r.error = c.get("error", "")
        rep.results.append(r)
    return rep


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """在 pytest 输出末尾打印准确率报告。"""
    reports = _load_reports()
    if not reports:
        return

    show = config.getoption("--accuracy-report")
    min_acc = config.getoption("--min-accuracy")

    terminalreporter.write_sep("=", "EC-Monica 评测准确率汇总")

    overall_pass = 0
    overall_total = 0
    weighted_num = 0.0
    weighted_den = 0.0

    for report in reports:
        overall_pass += report.passed
        overall_total += report.total
        weighted_num += sum(r.weight for r in report.results if r.passed)
        weighted_den += sum(r.weight for r in report.results)
        terminalreporter.write_line(
            f"  {report.layer:<24} {report.passed:>3}/{report.total:<3} "
            f"准确率 {report.accuracy:>7.1%}   加权 {report.weighted_accuracy:>7.1%}"
        )
        if show:
            terminalreporter.write_line(report.render())

    acc = overall_pass / overall_total if overall_total else 0.0
    wacc = weighted_num / weighted_den if weighted_den else 0.0

    terminalreporter.write_line("")
    terminalreporter.write_line(
        f"  {'★ 总体':<24} {overall_pass:>3}/{overall_total:<3} "
        f"准确率 {acc:>7.1%}   加权 {wacc:>7.1%}"
    )

    # 写入汇总 JSON（供 CI 上传 artifact / 画趋势图）
    summary = {
        "overall": {
            "passed": overall_pass,
            "total": overall_total,
            "accuracy": round(acc, 4),
            "weighted_accuracy": round(wacc, 4),
        },
        "layers": [
            {
                "layer": r.layer,
                "passed": r.passed,
                "total": r.total,
                "accuracy": round(r.accuracy, 4),
                "weighted_accuracy": round(r.weighted_accuracy, 4),
            }
            for r in reports
        ],
    }
    out = _accuracy_dir() / "accuracy_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    terminalreporter.write_line("")
    terminalreporter.write_line(f"  报告已写入：{out}")

    # 门槛检查
    if min_acc is not None:
        terminalreporter.write_line("")
        if acc < min_acc:
            terminalreporter.write_line(
                f"  ❌ 准确率 {acc:.1%} 低于门槛 {min_acc:.1%}")
            config._ec_accuracy_failed = True
        else:
            terminalreporter.write_line(
                f"  ✅ 准确率 {acc:.1%} 达到门槛 {min_acc:.1%}")
    terminalreporter.write_sep("=")


def pytest_sessionfinish(session, exitstatus):
    """
    门槛未达标时把退出码改为非零。

    ★ 为什么不在 pytest_terminal_summary 里直接 sys.exit？
      因为那时退出码已经定了。正确做法是在 sessionfinish 里改。
    """
    config = session.config
    if getattr(config, "_ec_accuracy_failed", False) and exitstatus == 0:
        session.exitstatus = 1


# ---------------------------------------------------------------------------
# 共享 fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def eval_dir() -> Path:
    """评估集目录。"""
    return ROOT / "tests" / "eval"


@pytest.fixture(scope="session")
def has_api_key() -> bool:
    """是否配置了 API Key（决定 live 层能否运行）。"""
    return bool(os.getenv("DEEPSEEK_API_KEY", "").strip())


@pytest.fixture(scope="session")
def db_ready() -> bool:
    """数据库是否就绪并有数据。"""
    try:
        from app.db.database import readonly_query
        rows = readonly_query("SELECT COUNT(*) AS n FROM dim_product")
        return bool(rows and rows[0]["n"] > 0)
    except Exception:                                       # noqa: BLE001
        return False
