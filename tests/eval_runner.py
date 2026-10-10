"""
tests/eval_runner.py
====================
★ 评估框架：把 golden set 变成可自动判分、可输出准确率的测试。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么评测要分「离线」和「在线」两层？
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    离线层（不需要 LLM，秒级、免费、100% 可复现）：
        · 路由准确率    —— 意图识别是否正确
        · 工具准确率    —— 该调的工具有没有调到
        · 数值准确率    —— 指标层算出的数字对不对
        · 质控准确率    —— 4 类归因陷阱能否检出

        这一层能进 CI 每次提交都跑，因为它不依赖外部服务。
        而且它覆盖了"数字可信度"这个项目最核心的价值主张 ——
        即使不接 LLM，这部分正确性也必须守住。

    在线层（需要 LLM，分钟级、花钱、有随机性）：
        · 端到端行为   —— 是否真的按纪律作答、是否引用真实数字
        · 陷阱抵御     —— 面对归因陷阱会不会被带偏

        这一层用 pytest marker 隔离，只在有 API Key 时跑。

    ★ 关键设计：**离线层的准确率本身就是有价值的指标**。
      它衡量的是"确定性骨架"的健康度。当改了 prompt、换了模型、
      或者重构了指标层时，离线准确率下降就说明出了问题 ——
      而且能立刻定位到是哪一类能力退化。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL_DIR = ROOT / "tests" / "eval"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    """单个用例的判分结果。"""

    case_id: str
    question: str
    exams: str
    weight: float = 1.0
    # 各维度得分（0~1）
    scores: dict[str, float] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def score(self) -> float:
        """该用例的综合得分（各维度平均）。"""
        if not self.scores:
            return 0.0
        return sum(self.scores.values()) / len(self.scores)

    @property
    def passed(self) -> bool:
        """全部维度满分才算通过（评测要严格）。"""
        return bool(self.scores) and all(v >= 1.0 for v in self.scores.values())


@dataclass
class EvalReport:
    """整轮评测报告。"""

    layer: str = "offline"
    results: list[CaseResult] = field(default_factory=list)

    # --- 汇总指标 ---
    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def accuracy(self) -> float:
        """准确率 = 通过用例数 / 用例总数。"""
        return self.passed / self.total if self.total else 0.0

    @property
    def weighted_accuracy(self) -> float:
        """
        加权准确率。

        ★ 为什么还要加权？
          陷阱题权重是普通题的 1.5~3 倍 —— 因为"会不会被归因陷阱带偏"
          比"能不能取对数"更能区分系统能力。
          只看未加权准确率，会让"取数全对但归因全错"的系统看起来还不错。
        """
        w_total = sum(r.weight for r in self.results)
        if not w_total:
            return 0.0
        w_passed = sum(r.weight for r in self.results if r.passed)
        return w_passed / w_total

    @property
    def mean_score(self) -> float:
        """平均得分（部分正确也给分，用于观察趋势）。"""
        if not self.results:
            return 0.0
        return sum(r.score for r in self.results) / len(self.results)

    def dimension_accuracy(self) -> dict[str, float]:
        """各维度的准确率（哪一类能力弱，一眼可见）。"""
        dims: dict[str, list[float]] = {}
        for r in self.results:
            for dim, val in r.scores.items():
                dims.setdefault(dim, []).append(val)
        return {d: sum(v) / len(v) for d, v in dims.items()}

    def by_weight_class(self) -> dict[str, float]:
        """按题型分组（基础题 / 陷阱题）。"""
        groups: dict[str, list[CaseResult]] = {"基础题": [], "陷阱题": []}
        for r in self.results:
            groups["陷阱题" if r.weight >= 3.0 else "基础题"].append(r)
        return {
            name: (sum(1 for r in rs if r.passed) / len(rs) if rs else 0.0)
            for name, rs in groups.items()
        }

    # --- 输出 ---
    def render(self, title: str = "评测报告") -> str:
        """渲染成可读报告（CI 日志里直接输出）。"""
        lines = [
            "",
            "=" * 78,
            f"{title}（{self.layer}）",
            "=" * 78,
            f"  用例总数      : {self.total}",
            f"  通过          : {self.passed}",
            f"  ❯ 准确率      : {self.accuracy:.1%}",
            f"  ❯ 加权准确率  : {self.weighted_accuracy:.1%}   "
            f"（陷阱题权重 1.5~3x）",
            f"  平均得分      : {self.mean_score:.1%}   （部分正确也给分）",
            "",
            "  ── 分维度准确率 ──",
        ]
        for dim, val in sorted(self.dimension_accuracy().items()):
            bar = "█" * int(val * 20) + "░" * (20 - int(val * 20))
            lines.append(f"    {dim:<16} {bar} {val:>6.1%}")

        lines += ["", "  ── 分题型准确率 ──"]
        for name, val in self.by_weight_class().items():
            lines.append(f"    {name:<16} {val:>6.1%}")

        lines += ["", "  ── 逐题结果 ──"]
        for r in self.results:
            icon = "✓" if r.passed else "✗"
            w = f"w={r.weight:g}"
            lines.append(f"    [{icon}] {r.case_id:<24} {w:<6} "
                         f"得分={r.score:>5.0%}  {r.exams[:38]}")
            for d in r.details:
                lines.append(f"          {d}")
            if r.error:
                lines.append(f"          ⚠ {r.error}")

        # 失败用例单独列出，便于定位
        failed = [r for r in self.results if not r.passed]
        if failed:
            lines += ["", "  ── 未通过用例明细 ──"]
            for r in failed:
                lines.append(f"    ✗ {r.case_id}")
                for dim, val in r.scores.items():
                    if val < 1.0:
                        lines.append(f"        {dim}: {val:.0%}")

        lines += ["", "=" * 78, f"  准确率 {self.accuracy:.1%}", "=" * 78, ""]
        return "\n".join(lines)

    def to_json(self) -> dict:
        """结构化输出（供 CI 存档 / 趋势追踪）。"""
        return {
            "layer": self.layer,
            "total": self.total,
            "passed": self.passed,
            "accuracy": round(self.accuracy, 4),
            "weighted_accuracy": round(self.weighted_accuracy, 4),
            "mean_score": round(self.mean_score, 4),
            "by_dimension": {k: round(v, 4)
                             for k, v in self.dimension_accuracy().items()},
            "by_class": {k: round(v, 4)
                         for k, v in self.by_weight_class().items()},
            "cases": [
                {
                    "id": r.case_id, "passed": r.passed,
                    "score": round(r.score, 4), "weight": r.weight,
                    "scores": {k: round(v, 4) for k, v in r.scores.items()},
                    "error": r.error,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# golden set 加载
# ---------------------------------------------------------------------------

def load_cases(filename: str = "golden_set.json") -> list[dict]:
    """
    加载评估集。

    ★ 加载时做结构校验：必填字段缺失要立刻报错，
      而不是等到判分时才因为 KeyError 崩掉 ——
      评估集是"标准答案"，它本身错了会让所有结论失效。
    """
    path = EVAL_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"评估集不存在：{path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("cases", [])

    required = ("id", "question", "exams")
    for i, case in enumerate(cases):
        missing = [k for k in required if k not in case]
        if missing:
            raise ValueError(
                f"评估集第 {i + 1} 条缺少必填字段 {missing}："
                f"{case.get('id', '(无 id)')}"
            )
        # weight 缺省为 1.0
        case.setdefault("weight", 1.0)
    return cases


# ---------------------------------------------------------------------------
# 判分工具
# ---------------------------------------------------------------------------

def score_value(actual: Any, expected: Any, tolerance: float = 0) -> float:
    """
    数值比对。返回 0~1。

    ★ 为什么必须支持 tolerance？
      浮点运算的求和顺序不同会带来极小差异（如 123012.88 vs 123012.879999），
      用等值比较会误判为失败。但 tolerance 也不能太大 ——
      否则"数字算错了但差得不多"会蒙混过关。
      所以 tolerance 由每题显式指定（金额给 1 元，比率给 0.0005）。
    """
    if expected is None:
        return 1.0                      # 该题不考数值
    if actual is None:
        return 0.0

    if isinstance(expected, str):
        return 1.0 if str(actual) == expected else 0.0

    if isinstance(expected, (list, tuple)):
        if not isinstance(actual, (list, tuple)):
            return 0.0
        return 1.0 if list(actual) == list(expected) else 0.0

    try:
        a, e = float(actual), float(expected)
    except (TypeError, ValueError):
        return 0.0
    return 1.0 if abs(a - e) <= tolerance else 0.0


def score_tool_used(called: list[str], expected: str | None) -> float:
    """
    工具调用比对。

    ★ 判分宽严的取舍：
      只要求"调用了期望的工具"，不要求"只调用了它" ——
      因为多查几个相关工具是合理的（甚至更好），
      而漏调关键工具才是真问题。
    """
    if not expected:
        return 1.0
    return 1.0 if expected in called else 0.0


def score_contains(text: str, keywords: list[str] | None) -> float:
    """关键词命中率（用于行为类断言，如"必须提到缺货"）。"""
    if not keywords:
        return 1.0
    if not text:
        return 0.0
    hit = sum(1 for k in keywords if k in text)
    return hit / len(keywords)


def score_forbidden(text: str, forbidden: list[str] | None) -> float:
    """禁止词（出现即 0 分）。用于"不得归因为详情页"这类断言。"""
    if not forbidden:
        return 1.0
    if not text:
        return 1.0
    return 0.0 if any(f in text for f in forbidden) else 1.0
