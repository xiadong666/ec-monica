"""
app/context/builder.py
======================
★★ Context 构建器 —— 上下文工程的落地实现。

对应规格要求：
    「Context Engineering：注入业务范文与运营分析模板，携带压缩后的前轮结论滚动输入」

Context 的六个组成部分（按注入顺序，顺序本身也是设计）：

    ┌─ ① 角色设定        ：让模型知道"我是谁"
    ├─ ② 业务规则        ：数字纪律/归因纪律/审批纪律（硬约束）
    ├─ ③ 指标口径字典    ：每个指标怎么算，防止自行发明口径
    ├─ ④ 业务范文模板    ：对齐内部输出风格（风格对齐率的来源）
    ├─ ⑤ 数据预处理结果  ：算好的同比/环比指标（★ 不是原始数据）
    └─ ⑥ 滚动前轮结论    ：压缩后的历史轮次（近详远略）

为什么是这个顺序？
    硬约束（②③）必须放在**范文和数据之前** —— 
    模型对靠前的内容服从度更高，把纪律放在最后容易被淹没在数据里。
    而范文放中间，是因为它要"框住"后面数据的呈现方式。

★ 一个关键设计：注入的是**预处理后的指标**，不是原始数据行。
  规格里写的是"数据预处理计算同比/环比指标后注入 Context"，
  这一步是整个 Agent 数字可信度的来源：
  同比环比由代码算（永远不会错），模型只负责解读。
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.context.compressor import (
    compress_injected_data, compress_round, estimate_tokens,
)
from app.context.templates import BUSINESS_RULES, ROLE_PROMPTS, TEMPLATES
from app.metrics.definitions import EXPLAIN_FOR_AGENT


# ---------------------------------------------------------------------------
# ★ 时间注入：相对时间必须先解析成绝对日期，再交给模型
# ---------------------------------------------------------------------------

_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def build_time_block(today: date | None = None) -> str:
    """
    构造时间上下文，把"上周""本月"这类相对表达解析成绝对日期。

    ★ 为什么不能只注入一句"当前日期是 X"？

      用户问"上周转化率为什么下滑"，模型必须知道"上周"具体是哪天，
      否则它会用自己的训练数据去猜 —— 而训练数据里的"上周"是错的。

      但只给一句当前日期还不够：模型仍要做日期心算
      （今天 8/25，上周一是 8/18……），而模型算日期和算数字一样不可靠。
      本项目的一贯原则是**计算归代码**，所以这里直接把常用相对时间
      全部预先解析好，模型只需引用，不需换算。

    ★ 另一个作用：让模型能发现"用户问的时间段没有数据"。
      比如今天超过数据末日时，"上周"可能已超出数据范围，
      模型应当明确指出，而不是硬答。
    """
    today = today or date.today()
    weekday = _WEEKDAY_CN[today.weekday()]

    # 上一个完整自然周（周一~周日）
    days_since_sunday = (today.weekday() + 1) % 7
    last_week_end = today - timedelta(days=days_since_sunday or 7)
    last_week_start = last_week_end - timedelta(days=6)
    # 本周（截至今天）
    this_week_start = today - timedelta(days=today.weekday())

    lines = [
        f"当前系统日期：{today.isoformat()}（{weekday}）",
        "",
        "相对时间已为你预先解析（直接引用，不要自行推算）：",
        f"  - 今天          = {today.isoformat()}",
        f"  - 本周一        = {this_week_start.isoformat()}",
        f"  - 上周          = {last_week_start.isoformat()} ~ {last_week_end.isoformat()}",
        f"  - 上上周        = {(last_week_start - timedelta(days=7)).isoformat()}"
        f" ~ {(last_week_end - timedelta(days=7)).isoformat()}",
        f"  - 最近 7 天     = {(today - timedelta(days=6)).isoformat()} ~ {today.isoformat()}",
        f"  - 最近 30 天    = {(today - timedelta(days=29)).isoformat()} ~ {today.isoformat()}",
        f"  - 本月          = {today.replace(day=1).isoformat()} ~ {today.isoformat()}",
    ]

    # 附上数据库实际数据范围，让模型能判断"问的时间段有没有数据"
    try:
        from app.metrics.compute import get_data_date_range
        d_min, d_max = get_data_date_range()
        if d_min and d_max:
            lines += [
                "",
                f"数据库实际数据范围：{d_min} ~ {d_max}",
                "⚠️ 若用户询问的时间段超出该范围，必须明确指出"
                "「该时间段无数据」，不得用其他时间段的数据代替作答。",
            ]
    except Exception:                                   # noqa: BLE001
        pass

    lines += [
        "",
        "注意：以上日期由系统计算，是唯一权威时间来源。"
        "禁止使用你记忆中的日期或自行推算相对时间。",
    ]
    return "\n".join(lines)


def detect_time_mismatch(resolved_start: str, resolved_end: str,
                         today: date | None = None) -> dict:
    """
    ★ 检测「用户问的时间段」与「系统实际取数的区间」是否错位。

    为什么需要这个？—— 因为这里存在一个**会静默误导用户的陷阱**：

      用户问"上周转化率为什么下滑"，系统（没有指定区间时）会返回
      「数据中最后一个完整自然周」。如果数据是历史数据（比如数据止于
      2025-08-29，而今天是 2026-10-09），那么：

        用户问的"上周"    = 2026-09-28 ~ 2026-10-04
        系统实际回答的区间 = 2025-08-18 ~ 2025-08-24  ← 相差 406 天！

      而报告标题会写成"上周转化率归因（2025-08-18 ~ 2025-08-24）"，
      **看起来完全正常**，用户根本不会意识到答案对应的是一年前的行情。

      这比"返回空结果"危险得多 —— 空结果用户一眼能看出问题，
      而一个格式正确、内容详实、日期却错位的报告，用户会直接采信。

    正确行为：必须显式、醒目地声明这个错位，并让用户决定是接受
    "数据最新区间"还是提供正确的时间范围。

    返回 dict：{"mismatch": bool, "banner": str}
    """
    today = today or date.today()
    try:
        req_end = date.fromisoformat(resolved_end)
    except (ValueError, TypeError):
        return {"mismatch": False, "banner": ""}

    gap_days = (today - req_end).days

    # 数据末日距今天超过 30 天，就属于"回答的不是用户问的时间"
    if gap_days <= 30:
        return {"mismatch": False, "banner": ""}

    # 用户口中的"上周/最近"对应的真实区间，仅用于提示
    days_since_sunday = (today.weekday() + 1) % 7
    real_last_week_end = today - timedelta(days=days_since_sunday or 7)
    real_last_week_start = real_last_week_end - timedelta(days=6)

    banner = (
        "⚠️⚠️【时间错位警告 —— 必须在回答开头明确声明】\n"
        f"用户问的时间段大约是「上周」（{real_last_week_start.isoformat()} ~ "
        f"{real_last_week_end.isoformat()}），\n"
        f"但数据库**没有该时间段的数据**（数据止于 {resolved_end}，"
        f"距今已 {gap_days} 天）。\n"
        f"因此本次分析实际使用的是「数据中最后一个完整自然周」："
        f"{resolved_start} ~ {resolved_end}。\n\n"
        "你的回答必须遵守：\n"
        "  1. **开头第一句就必须说明这个错位**，例如："
        "「注意：数据库中无 2026-09-28 之后的数据，以下分析基于"
        f"数据最新区间 {resolved_start} ~ {resolved_end}」；\n"
        "  2. **禁止**把这个区间直接称作「上周」而不加说明；\n"
        "  3. 如果用户问的现象（如下滑）在该区间并不成立，要明确指出；\n"
        "  4. 结尾提示用户：若需分析特定时间段，请显式提供日期范围。\n"
    )
    return {"mismatch": True, "banner": banner}


@dataclass
class RoundRecord:
    """一轮循环的记录。"""

    round_no: int
    tool_names: list[str] = field(default_factory=list)
    conclusion: str = ""
    key_numbers: dict = field(default_factory=dict)
    injected_data: dict = field(default_factory=dict)
    tool_errors: list[str] = field(default_factory=list)


class ContextBuilder:
    """
    上下文构建器。

    用法：
        cb = ContextBuilder(role="attribution", template="attribution")
        cb.set_task("上周转化率为什么下滑？")
        cb.inject_metrics({"gmv_gross": 123013, "pay_cvr": 0.0271, ...})
        ctx = cb.build()                      # 组装完整上下文
        cb.add_round(RoundRecord(...))        # 记录一轮
        ctx2 = cb.build()                     # 下一轮：自动带上压缩后的历史
    """

    def __init__(self, role: str = "analysis_report",
                 template: str = "analysis_report",
                 max_rounds_kept: int = 6):
        self.role = role
        self.template = template
        self.max_rounds_kept = max_rounds_kept
        self.task: str = ""
        # 预处理产物：算好的指标（注入 Context 的核心内容）
        self.metrics_block: dict = {}
        self.data_blocks: list[tuple[str, dict]] = []   # (标题, 数据)
        self.rounds: list[RoundRecord] = []
        self.notes: list[str] = []

    # ------------------------------------------------------------------
    # 设置任务与数据
    # ------------------------------------------------------------------

    def set_task(self, task: str) -> None:
        self.task = task or ""

    def inject_metrics(self, metrics: dict, title: str = "核心指标（含同比/环比）") -> None:
        """
        ★ 注入数据预处理产物（算好的指标）。

        注意：这里应该是**已经算好同比/环比**的指标，
        而不是让模型自己去算 —— 模型算数不可靠，且错误无法察觉。
        """
        self.metrics_block = {"title": title, "data": metrics}

    def inject_data(self, title: str, data: dict) -> None:
        """注入一块数据（下钻结果等）。"""
        self.data_blocks.append((title, data))

    def add_note(self, note: str) -> None:
        """注入一条提示（如质控警告）。"""
        self.notes.append(note)

    # ------------------------------------------------------------------
    # 滚动：记录一轮
    # ------------------------------------------------------------------

    def add_round(self, record: RoundRecord) -> None:
        """记录一轮循环结果。超出上限时丢弃最旧的（因为已压缩进历史摘要）。"""
        self.rounds.append(record)
        if len(self.rounds) > self.max_rounds_kept:
            self.rounds = self.rounds[-self.max_rounds_kept:]

    def render_history(self) -> str:
        """
        ★ 渲染滚动历史：近详远略。

        recency_index = 0 表示最近一轮，逐轮递增，压缩强度随之提高。
        """
        if not self.rounds:
            return "（无历史轮次）"

        # 从最近往回编号
        oldest_first = list(reversed(self.rounds))
        total = len(oldest_first)

        blocks = []
        for i, rec in enumerate(oldest_first):
            recency_index = total - 1 - i        # 0 = 最近
            blocks.append(compress_round(
                round_no=rec.round_no,
                tool_names=rec.tool_names,
                conclusion=rec.conclusion,
                key_numbers=rec.key_numbers,
                recency_index=recency_index,
            ))

        header = f"（共 {total} 轮已完成的工作，由远及近）"
        return header + "\n\n" + "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def build(self, include_template: bool = True) -> str:
        """组装完整的上下文文本。"""
        parts: list[str] = []

        # ① 角色
        parts.append(ROLE_PROMPTS.get(self.role, ROLE_PROMPTS["analysis_report"]))

        # ② 业务规则（硬约束，必须靠前）
        parts.append(BUSINESS_RULES)

        # ③ 时间上下文（相对时间已解析，防止模型拿训练数据里的日期来猜）
        parts.append("【时间上下文】\n" + build_time_block())

        # ④ 指标口径字典
        parts.append(EXPLAIN_FOR_AGENT)

        # ⑤ 业务范文模板（框住输出风格）
        if include_template and self.template in TEMPLATES:
            parts.append(TEMPLATES[self.template])

        # ⑥ 任务
        if self.task:
            parts.append(f"【本次任务】\n{self.task}")

        # ⑦ 数据预处理结果
        if self.metrics_block:
            parts.append(
                f"【{self.metrics_block['title']}】\n"
                "（以下指标已由系统预处理并算好同比/环比，直接引用，不要自行计算）\n"
                + compress_injected_data(self.metrics_block["data"])
            )

        for title, data in self.data_blocks:
            parts.append(f"【{title}】\n" + compress_injected_data(data))

        # ⑧ 质控提示
        if self.notes:
            parts.append("【重要提示】\n" + "\n".join(f"- {n}" for n in self.notes))

        # ⑨ 滚动历史
        if self.rounds:
            parts.append("【已完成的工作（压缩后的前轮结论）】\n" + self.render_history())

        return "\n\n" + ("\n" + "=" * 60 + "\n").join(parts)

    def token_stats(self) -> dict:
        """上下文体积统计，用于监控膨胀。"""
        full = self.build()
        return {
            "rounds": len(self.rounds),
            "total_chars": len(full),
            "estimated_tokens": estimate_tokens(full),
            "history_tokens": estimate_tokens(self.render_history()),
            "data_blocks": len(self.data_blocks),
        }
