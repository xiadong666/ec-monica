"""
app/quality/checks.py
=====================
★★ 质控层（Data Quality Control）—— 本项目区别于"玩具 Agent"的核心模块。

它要解决的问题：**归因污染（Confounded Attribution）**

    现象：转化率跌了 20%。
    天真 Agent：详情页不行，建议优化主图和卖点。
    真相可能是：爆款缺货了 / 上周涨价了 / 大促流量退了 / 付费流量占比升高了。
    这四种情况的应对动作完全相反 —— 优化详情页对缺货毫无用处。

    所以质控层的职责是：**在 Agent 下结论之前，先把"不能用来下结论的数据"标出来。**

输出是机器可读的 flags 列表，直接注入 prompt，让模型无法忽略。
"""
from app.config import settings
from app.metrics.compute import (
    ad_efficiency_trend,
    get_data_date_range,
    period_summary,
    price_change_scan,
    split_periods,
    stockout_scan,
)
from app.metrics.definitions import SPECIAL_PERIODS


def check_period_coverage(start: str, end: str) -> list[dict]:
    """检查区间内是否混入了大促等特殊日期。"""
    flags = []
    clean, special = split_periods(start, end, SPECIAL_PERIODS)

    if special:
        # 统计被污染的天数
        polluted = 0
        from datetime import date
        for s, e in special:
            polluted += (date.fromisoformat(e) - date.fromisoformat(s)).days + 1
        flags.append({
            "level": "warning",
            "code": "SPECIAL_PERIOD_INCLUDED",
            "message": (
                f"区间 {start}~{end} 含 {polluted} 天特殊日期（大促），"
                f"这些天的数据不能代表日常经营水平。"
            ),
            "evidence": [{"special_ranges": special}],
            "action": (
                "涉及趋势/效率的结论请使用 exclude_special=True 的口径，"
                "或单独把大促作为事件分析，不要与日常混算。"
            ),
        })
    return flags


def check_sample_size(period: dict) -> list[dict]:
    """样本量检查：天数太少就不许下趋势结论。"""
    flags = []
    days = period.get("sample_days", 0)
    if days == 0:
        flags.append({
            "level": "error",
            "code": "NO_DATA",
            "message": f"区间 {period.get('period')} 没有任何数据。",
            "evidence": [],
            "action": "请检查日期区间是否正确，数据范围为 "
                      f"{get_data_date_range()}。",
        })
    elif days < settings.MIN_CONFIDENCE_SAMPLE_DAYS:
        flags.append({
            "level": "warning",
            "code": "SMALL_SAMPLE",
            "message": (
                f"区间 {period.get('period')} 仅 {days} 天有效数据"
                f"（少于 {settings.MIN_CONFIDENCE_SAMPLE_DAYS} 天）。"
            ),
            "evidence": [{"sample_days": days}],
            "action": "样本过少，结论置信度只能标为 low，禁止表述为'趋势'。",
        })
    return flags


def check_stockout(start: str, end: str) -> list[dict]:
    """
    ★ 缺货检查 —— 必须始终跑。

    缺货是"转化率下跌"最隐蔽也最常见的真凶：
    用户照样点进来（流量不掉），但看到无货就走了，转化率断崖，
    看数据的人却会以为是页面或价格问题。
    """
    flags = []
    rows = stockout_scan(start, end)
    if rows:
        detail = [
            {
                "product_id": r["product_id"],
                "title": r["title"],
                "stockout_days": r["stockout_days"],
                "range": f"{r['first_stockout']} ~ {r['last_stockout']}",
            }
            for r in rows
        ]
        names = "、".join(f"{r['title']}({r['stockout_days']}天)" for r in rows[:3])
        flags.append({
            "level": "error",
            "code": "STOCKOUT_FOUND",
            "message": (
                f"区间内有 {len(rows)} 个商品发生缺货：{names}。"
                "缺货会直接压低转化率，任何转化率下滑结论都必须先排除此项。"
            ),
            "evidence": detail,
            "action": "在解释转化率/销售额下滑前，优先考虑补货；"
                      "禁止把缺货造成的下跌归因为详情页或投放问题。",
        })
    return flags


def check_price_change(days: int = 45) -> list[dict]:
    """
    ★ 价格突变检查 —— 识别"悄悄改价"。

    实际成交单价跳变会同时影响转化率和客单价，
    如果不先发现改价，Agent 会把"涨价导致转化下滑"误判成"产品力下降"。
    """
    flags = []
    for r in price_change_scan(days=days, threshold=0.10):
        if r["affected_days"] < 2:
            continue
        direction = "上涨" if r["change_pct"] > 0 else "下跌"
        flags.append({
            "level": "warning",
            "code": "PRICE_CHANGE_DETECTED",
            "message": (
                f"{r['title']}({r['product_id']}) 实际成交单价{direction} "
                f"{abs(r['change_pct']):.1%}："
                f"{r['baseline_unit_price']} → {r['current_unit_price']}，"
                f"自 {r['detected_from']} 起影响 {r['affected_days']} 天。"
            ),
            "evidence": [r],
            "action": (
                "价格变动会同时改变转化率与客单价，"
                "涉及转化率下滑的归因必须把'改价'作为候选原因之一，"
                "并可用竞品价格交叉验证。"
            ),
        })
    return flags


def check_channel_mix(start: str, end: str) -> list[dict]:
    """
    流量结构变化检查。

    整体转化率 = 各渠道转化率的加权平均。
    即使每个渠道的转化率都没变，只要低转化渠道（付费/直播）占比升高，
    整体转化率也会下降 —— 这是假性下跌。
    """
    from app.metrics.compute import channel_breakdown, previous_week_of

    flags = []
    prev_start, prev_end = previous_week_of(start, end)
    cur = {r["channel"]: r for r in channel_breakdown(start, end)}
    prev = {r["channel"]: r for r in channel_breakdown(prev_start, prev_end)}
    if not cur or not prev:
        return flags

    shifts = []
    for ch, r in cur.items():
        if ch in prev:
            delta = r["visitor_share"] - prev[ch]["visitor_share"]
            if abs(delta) >= 0.05:            # 占比变动超过 5 个百分点才算显著
                shifts.append({
                    "channel": ch,
                    "visitor_share_now": round(r["visitor_share"], 4),
                    "visitor_share_prev": round(prev[ch]["visitor_share"], 4),
                    "delta": round(delta, 4),
                    "cvr_now": r["cvr"],
                    "cvr_prev": prev[ch]["cvr"],
                })

    if shifts:
        detail = "；".join(
            f"{s['channel']}占比 {s['visitor_share_prev']:.1%}→{s['visitor_share_now']:.1%}"
            for s in shifts
        )
        flags.append({
            "level": "info",
            "code": "CHANNEL_MIX_SHIFT",
            "message": f"流量结构发生明显变化：{detail}。",
            "evidence": shifts,
            "action": (
                "整体转化率变化可能由流量结构变化导致（辛普森悖论），"
                "下结论前请分渠道看转化率，确认是各渠道内部变化还是结构变化。"
            ),
        })
    return flags


def check_ad_diminishing_return(bucket_days: int = 7) -> list[dict]:
    """
    投放边际递减检查。

    区分两种截然不同的结论：
      A. 花费涨 + ROI 跌 → 边际递减，应优化投放结构（不是关停渠道！）
      B. 花费平 + ROI 跌 → 渠道效率真实恶化，才考虑收缩

    ⚠️ 实现要点（实测踩过两个坑）：
       坑1：不能只比较「最后一段 vs 平均」—— 末段花费常因自然波动回落，
            单点比较会直接漏报。趋势判断不能用单点。
       坑2：也不能只用线性斜率 —— 加预算的真实形态往往是「上台阶」：
            花费跳升一级后走平，ROI 掉一个档次后也走平。
            此时线性斜率≈0，会被误判为"没有恶化"，
            而业务上这恰恰是最该报警的形态（换挡后停在低 ROI 平台）。

    因此这里用「水平位移（level shift）+ 趋势」双重检测：
       - 前 1/3 与后 1/3 的均值对比 → 抓台阶式跳变
       - 全期最小二乘斜率          → 抓渐进式恶化
       两者任一成立即告警，并说明是哪种形态（动作不同）。
    """
    flags = []
    buckets = [b for b in ad_efficiency_trend(bucket_days) if b["roi"]]
    if len(buckets) < 3:
        return flags

    def slope(values: list[float]) -> float:
        """最小二乘斜率（x 取 0,1,2,...）。"""
        n = len(values)
        xs = list(range(n))
        mx = sum(xs) / n
        my = sum(values) / n
        denom = sum((x - mx) ** 2 for x in xs)
        if not denom:
            return 0.0
        return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / denom

    spends = [b["ad_spend"] for b in buckets]
    rois = [b["roi"] for b in buckets]

    spend_slope = slope(spends)
    roi_slope = slope(rois)

    # 归一化：斜率相对均值，避免量纲影响
    spend_trend = spend_slope / (sum(spends) / len(spends)) if spends else 0
    roi_trend = roi_slope / (sum(rois) / len(rois)) if rois else 0

    # 前后 1/3 的水平位移
    k = max(len(buckets) // 3, 1)
    spend_early = sum(spends[:k]) / k
    spend_late = sum(spends[-k:]) / k
    roi_early = sum(rois[:k]) / k
    roi_late = sum(rois[-k:]) / k
    spend_shift = (spend_late - spend_early) / spend_early if spend_early else 0
    roi_shift = (roi_late - roi_early) / roi_early if roi_early else 0

    def roi_breakeven_note() -> str:
        return (f"前段平均 ROI {roi_early:.2f} → 后段 {roi_late:.2f}")

    # 形态 A：边际递减 —— 花费上去了，ROI 下来了
    #   判据用「或」连接两种度量，因为不同分段方式下信号出现在不同度量上：
    #     水平位移（spend_shift）→ 抓台阶式加预算
    #     线性斜率（spend_trend）→ 抓渐进式加预算
    #   实测：bucket=7 时花费斜率仅 +0.0037（0.5% 阈值下方），
    #   但水平位移 +8.5%、ROI 位移 -22.7% 已经把结论说得很清楚了。
    #   死守斜率阈值就会漏报 —— 度量必须匹配形态。
    spend_up = spend_shift > 0.08 or spend_trend > 0.005
    roi_down_hard = roi_shift < -0.10          # ROI 掉了至少一个档次
    roi_down_soft = roi_trend < -0.005         # ROI 持续缓慢下滑

    if spend_up and (roi_down_hard or roi_down_soft):
        shape = "上台阶" if spend_shift > 0.08 else "渐进"
        flags.append({
            "level": "warning",
            "code": "AD_DIMINISHING_RETURN",
            "message": (
                f"投放出现「{shape}式」边际递减：花费水平 {spend_early:,.0f} → "
                f"{spend_late:,.0f}/期（{spend_shift:+.1%}），"
                f"ROI {roi_early:.2f} → {roi_late:.2f}（{roi_shift:+.1%}）。"
            ),
            "evidence": buckets,
            "action": (
                "这是「加预算加到更差人群」的典型特征，正确动作是优化投放结构"
                "（分层出价 / 定向收缩 / 素材分层），**不是关停渠道**。"
                "仅凭 ROI 下降就建议关停，会误杀仍然赚钱的渠道 —— "
                f"当前 ROI 仍有 {roi_late:.2f}，关停前必须先确认它是否高于盈亏平衡线。"
            ),
        })
    # 形态 B：花费没涨，但效率真实恶化
    elif roi_down_hard and not spend_up:
        flags.append({
            "level": "warning",
            "code": "AD_EFFICIENCY_DECLINE",
            "message": (
                f"花费基本持平但 ROI 下滑（{roi_breakeven_note()}），"
                f"位移 {roi_shift:+.1%}、趋势 {roi_trend:+.2%}/期。"
            ),
            "evidence": buckets,
            "action": (
                "花费没涨而效率下降，说明渠道效率真实恶化"
                "（竞争加剧 / 人群衰减 / 素材疲劳），"
                "与「边际递减」不同：这种情况才需要考虑收缩预算或换素材。"
            ),
        })
    return flags


def run_all_checks(start: str, end: str, period: dict | None = None) -> dict:
    """
    跑完所有质控检查，返回：
        {
          "flags": [...],          # 所有问题标记
          "blocking": [...],       # error 级：必须先处理才能下结论
          "has_blocking": bool,
          "text": "..."            # 直接注入 prompt 的可读文本
        }
    """
    if period is None:
        period = period_summary(start, end, exclude_special=True)

    flags: list[dict] = []
    flags += check_period_coverage(start, end)
    flags += check_sample_size(period)
    flags += check_stockout(start, end)
    flags += check_price_change()
    flags += check_channel_mix(start, end)
    flags += check_ad_diminishing_return()

    blocking = [f for f in flags if f["level"] == "error"]

    return {
        "flags": flags,
        "blocking": blocking,
        "has_blocking": bool(blocking),
        "text": render_flags(flags),
    }


def render_flags(flags: list[dict]) -> str:
    """把 flags 渲染成注入 prompt 的文本。"""
    if not flags:
        return "【数据质控】未发现异常，数据可用于归因分析。"

    icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}
    lines = [
        "【数据质控结果 —— 以下是硬约束，归因时必须遵守】",
        "",
    ]
    for i, f in enumerate(flags, 1):
        lines.append(f"{icon.get(f['level'], '•')} [{i}] {f['code']}（{f['level']}）")
        lines.append(f"    现象：{f['message']}")
        lines.append(f"    要求：{f.get('action', '')}")
        lines.append("")
    return "\n".join(lines)
