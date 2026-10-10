"""
app/metrics/compute.py
======================
★ 指标计算层：纯函数，不依赖 LLM，可单独跑测试。

设计原则（这是整个项目可信度的地基）：
    1. **所有数字只能来自这里** —— LLM 不许自己算数，
       因为大模型做算术题会错，而且错了看不出来。
    2. **纯函数** —— 输入日期区间，输出指标 dict，没有全局状态，
       所以可以被 tests/ 反复验证，也能被 LLM 通过工具安全调用。
    3. **每个结果都带 sample_days** —— 样本量信息必须随数字一起返回，
       否则 Agent 会拿 2 天的数据下"趋势性结论"。

⚠️ 血泪教训（本项目最容易犯的取数错误）：
    fact_daily_product 与 fact_daily_traffic 是**不同粒度**的两张表，
    直接把它们 join 起来算 GMV 会让数字翻倍。
    所以本文件里，店铺级指标只从 fact_daily_product 取，
    ROI / 渠道结构只从 fact_daily_traffic 取，两者永不 join。
"""
from datetime import date, timedelta

from app.db.database import readonly_query
from app.metrics.definitions import SPECIAL_PERIODS


# ---------------------------------------------------------------------------
# 日期工具
# ---------------------------------------------------------------------------

def get_data_date_range() -> tuple[str, str]:
    """返回数据里实际的日期范围，避免对不存在的日期区间做查询。"""
    rows = readonly_query(
        "SELECT MIN(stat_date) AS d_min, MAX(stat_date) AS d_max FROM fact_daily_product"
    )
    if not rows or not rows[0]["d_min"]:
        return "", ""
    return rows[0]["d_min"], rows[0]["d_max"]


def latest_complete_week() -> tuple[str, str]:
    """
    返回数据中最后一个「完整自然周」（周一~周日）。

    为什么要"完整周"？因为拿半周去和整周比，环比数字毫无意义 ——
    这是运营报表里最常见的低级错误。
    """
    _, d_max = get_data_date_range()
    if not d_max:
        raise ValueError("数据库为空，请先运行 scripts/seed_data.py")

    end = date.fromisoformat(d_max)
    # 回退到最近的一个周日（weekday: 周一=0 ... 周日=6）
    days_since_sunday = (end.weekday() + 1) % 7
    week_end = end - timedelta(days=days_since_sunday)
    week_start = week_end - timedelta(days=6)
    return week_start.isoformat(), week_end.isoformat()


def previous_week_of(start: str, end: str) -> tuple[str, str]:
    """给定一个周区间，返回它上一周的同长度区间。"""
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    span = (e - s).days + 1
    return (s - timedelta(days=span)).isoformat(), (s - timedelta(days=1)).isoformat()


def split_periods(start: str, end: str, special: list) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """
    把一个区间切成「干净日」和「特殊日（大促等）」两部分。

    返回 (clean_ranges, special_ranges)，都是若干连续子区间。
    质控层用它来判断"这段数据能不能代表日常水平"。
    """
    cur = date.fromisoformat(start)
    last = date.fromisoformat(end)

    special_days: set[str] = set()
    for sp in special:
        s = max(date.fromisoformat(sp.start), cur)
        e = min(date.fromisoformat(sp.end), last)
        d = s
        while d <= e:
            special_days.add(d.isoformat())
            d += timedelta(days=1)

    def to_ranges(days: list[str]) -> list[tuple[str, str]]:
        """把零散日期合并成连续区间，便于生成可读描述。"""
        if not days:
            return []
        days = sorted(days)
        ranges, rs, prev = [], days[0], days[0]
        for ds in days[1:]:
            if (date.fromisoformat(ds) - date.fromisoformat(prev)).days == 1:
                prev = ds
                continue
            ranges.append((rs, prev))
            rs = prev = ds
        ranges.append((rs, prev))
        return ranges

    all_days = []
    d = cur
    while d <= last:
        all_days.append(d.isoformat())
        d += timedelta(days=1)

    clean_days = [ds for ds in all_days if ds not in special_days]
    return to_ranges(clean_days), to_ranges(sorted(special_days))


def _not_in_special_clause(column: str = "stat_date") -> tuple[str, list]:
    """生成「排除特殊日期」的 SQL 片段与参数。"""
    if not SPECIAL_PERIODS:
        return "", []
    clauses, params = [], []
    for sp in SPECIAL_PERIODS:
        clauses.append(f"NOT ({column} BETWEEN ? AND ?)")
        params.extend([sp.start, sp.end])
    return " AND " + " AND ".join(clauses), params


# ---------------------------------------------------------------------------
# 店铺级指标
# ---------------------------------------------------------------------------

def period_summary(start: str, end: str, exclude_special: bool = False) -> dict:
    """
    计算某段区间的店铺级核心指标。

    参数：
        start, end      : 日期区间（含端点），'YYYY-MM-DD'
        exclude_special : True 则剔除大促等特殊日期（做"日常水平"对比时必用）

    返回 dict，含各项指标 + sample_days（样本天数）。
    """
    extra, params = ("", [])
    if exclude_special:
        extra, params = _not_in_special_clause()

    sql = f"""
        SELECT
            COUNT(DISTINCT stat_date)          AS sample_days,
            SUM(gmv)                           AS gmv_gross,
            SUM(refund_amount)                 AS refund_amount,
            SUM(paid_orders)                   AS paid_orders,
            SUM(paid_units)                    AS paid_units,
            SUM(visitors)                      AS visitors,
            SUM(impressions)                   AS impressions,
            SUM(clicks)                        AS clicks
        FROM fact_daily_product
        WHERE stat_date BETWEEN ? AND ?{extra}
    """
    row = readonly_query(sql, tuple([start, end] + params))[0]

    gmv = row["gmv_gross"] or 0.0
    refund = row["refund_amount"] or 0.0
    orders = row["paid_orders"] or 0
    visitors = row["visitors"] or 0

    # 毛利率：用「当日价格快照」(f.unit_price) 而不是维表的「当前价」(p.price)。
    # 若用 p.price 倒推历史，改价之后所有历史毛利都会被算错 —— 这是很容易漏的口径 bug。
    margin_rows = readonly_query(
        f"""
        SELECT SUM(f.paid_units * (f.unit_price - p.cost)) AS gross_profit
        FROM fact_daily_product f
        JOIN dim_product p ON p.product_id = f.product_id
        WHERE f.stat_date BETWEEN ? AND ?{extra.replace('stat_date', 'f.stat_date')}
        """,
        tuple([start, end] + params),
    )
    gross_profit = (margin_rows[0]["gross_profit"] or 0.0) if margin_rows else 0.0

    return {
        "period": f"{start} ~ {end}",
        "sample_days": row["sample_days"] or 0,
        "excluded_special": exclude_special,
        # --- 规模 ---
        "gmv_gross": round(gmv, 2),
        "refund_amount": round(refund, 2),
        "gmv_net": round(gmv - refund, 2),
        "paid_orders": orders,
        "paid_units": row["paid_units"] or 0,
        # --- 效率 ---
        "aov": round(gmv / orders, 2) if orders else 0.0,
        "pay_cvr": round(orders / visitors, 5) if visitors else 0.0,
        "ctr": round(row["clicks"] / row["impressions"], 5) if row["impressions"] else 0.0,
        "refund_rate": round(refund / gmv, 5) if gmv else 0.0,
        "gross_margin_rate": round(gross_profit / gmv, 5) if gmv else 0.0,
        # --- 流量 ---
        "visitors": visitors,
        "impressions": row["impressions"] or 0,
    }


def period_compare(start: str, end: str, exclude_special: bool = True) -> dict:
    """
    环比对比：把给定周与上一周对比，返回每个指标的涨跌幅。

    exclude_special 默认 True —— 因为拿大促周和日常周比环比，
    会得到一个毫无业务意义的 +300%，这是新手最常踩的坑。
    """
    prev_start, prev_end = previous_week_of(start, end)

    cur = period_summary(start, end, exclude_special=exclude_special)
    prev = period_summary(prev_start, prev_end, exclude_special=exclude_special)

    def pct(cur_v, prev_v):
        if not prev_v:
            return None
        return round((cur_v - prev_v) / prev_v, 5)

    changes = {}
    for k in ["gmv_gross", "gmv_net", "paid_orders", "aov", "pay_cvr",
              "visitors", "refund_rate", "gross_margin_rate", "ctr"]:
        changes[k] = pct(cur[k], prev[k])

    return {
        "current": cur,
        "previous": prev,
        "change_pct": changes,
        "note": (
            "已剔除大促等特殊日期" if exclude_special
            else "⚠️ 未剔除特殊日期，若区间含大促则环比不可直接解读"
        ),
    }


# ---------------------------------------------------------------------------
# 商品级下钻
# ---------------------------------------------------------------------------

def product_breakdown(start: str, end: str) -> list[dict]:
    """
    按商品下钻，每个商品一行。

    ★ 关键：同时带出 stockout_days 与实际售价，
      让 Agent 能自动区分"缺货型下跌"和"改价型下跌"。
    """
    rows = readonly_query(
        """
        SELECT
            f.product_id,
            p.title,
            p.category,
            p.lifecycle,
            p.price                                     AS list_price,
            SUM(f.gmv)                                  AS gmv,
            SUM(f.refund_amount)                        AS refund_amount,
            SUM(f.paid_orders)                          AS paid_orders,
            SUM(f.paid_units)                           AS paid_units,
            SUM(f.visitors)                             AS visitors,
            SUM(f.impressions)                          AS impressions,
            -- 实际成交单价（受折扣/件数结构影响，仅作参考）
            ROUND(SUM(f.gmv) / NULLIF(SUM(f.paid_units), 0), 2) AS revenue_per_unit,
            -- ★ 区间内实际售价（价格快照均值），改价判断要用这个
            ROUND(AVG(f.unit_price), 2)                 AS stat_price
        FROM fact_daily_product f
        JOIN dim_product p ON p.product_id = f.product_id
        WHERE f.stat_date BETWEEN ? AND ?
        GROUP BY f.product_id, p.title, p.category, p.lifecycle, p.price
        ORDER BY gmv DESC
        """,
        (start, end),
    )

    # 单独查缺货天数，避免 join 造成 GMV 重复累加
    so_rows = readonly_query(
        """
        SELECT product_id, SUM(stockout_flag) AS stockout_days
        FROM fact_daily_inventory
        WHERE stat_date BETWEEN ? AND ?
        GROUP BY product_id
        """,
        (start, end),
    )
    so_map = {r["product_id"]: r["stockout_days"] for r in so_rows}

    out = []
    for r in rows:
        orders = r["paid_orders"] or 0
        visitors = r["visitors"] or 0
        gmv = r["gmv"] or 0.0
        out.append({
            "product_id": r["product_id"],
            "title": r["title"],
            "category": r["category"],
            "lifecycle": r["lifecycle"],
            "gmv": round(gmv, 2),
            "refund_amount": round(r["refund_amount"] or 0.0, 2),
            "gmv_net": round(gmv - (r["refund_amount"] or 0.0), 2),
            "paid_orders": orders,
            "visitors": visitors,
            "pay_cvr": round(orders / visitors, 5) if visitors else 0.0,
            "list_price": r["list_price"] or 0.0,          # 维表当前标价
            "stat_price": r["stat_price"] or 0.0,          # ★ 区间内实际售价
            "revenue_per_unit": r["revenue_per_unit"] or 0.0,
            "stockout_days": so_map.get(r["product_id"], 0),
        })
    return out


def price_change_scan(days: int = 60, threshold: float = 0.10,
                      min_segment_days: int = 2) -> list[dict]:
    """
    ★ 改价探测（变点检测 / Change-Point Detection）。

    为什么不能图省事用「和全期中位数比」？
        如果一次涨价发生在窗口中间，那么"涨价后"的天数可能已经多于之前，
        中位数会漂移到涨价后的价格上，导致 baseline == current，
        涨幅算出来是 0 —— 这正是本项目实测踩到的坑。

    正确做法：枚举所有切分点，分别算「切分前中位数」与「切分后中位数」，
    取相对差异最大的那个切分点作为变点。中位数抗离群值，
    所以不会被单日的促销折扣带偏。

    数据来源是 fact_daily_product.unit_price（当日售价快照）。
    注意不要用 SUM(gmv)/SUM(paid_units) 代替 —— 那个比值会被
    "一次买几件"的件数结构影响，不是价格信号。
    """
    _, d_max = get_data_date_range()
    if not d_max:
        return []
    end = date.fromisoformat(d_max)
    start = end - timedelta(days=days - 1)

    rows = readonly_query(
        """
        SELECT f.product_id, p.title, f.stat_date, f.unit_price
        FROM fact_daily_product f
        JOIN dim_product p ON p.product_id = f.product_id
        WHERE f.stat_date BETWEEN ? AND ? AND f.unit_price > 0
        ORDER BY f.product_id, f.stat_date
        """,
        (start.isoformat(), end.isoformat()),
    )

    series: dict[str, list[tuple[str, float]]] = {}
    titles: dict[str, str] = {}
    for r in rows:
        series.setdefault(r["product_id"], []).append((r["stat_date"], r["unit_price"]))
        titles[r["product_id"]] = r["title"]

    results = []
    for pid, pts in series.items():
        if len(pts) < min_segment_days * 2:
            continue

        prices = [p for _, p in pts]
        overall = sorted(prices)[len(prices) // 2]
        if not overall:
            continue

        # --- 枚举切分点，找差异最大的那个 ---
        best = None
        for i in range(min_segment_days, len(pts) - min_segment_days + 1):
            left = sorted(p for _, p in pts[:i])
            right = sorted(p for _, p in pts[i:])
            m_left = left[len(left) // 2]
            m_right = right[len(right) // 2]
            if not m_left:
                continue
            diff = (m_right - m_left) / m_left
            if best is None or abs(diff) > abs(best["change_pct"]):
                best = {
                    "change_pct": diff,
                    "before_price": round(m_left, 2),
                    "after_price": round(m_right, 2),
                    "detected_from": pts[i][0],
                    "days_before": i,
                    "days_after": len(pts) - i,
                }

        if not best or abs(best["change_pct"]) < threshold:
            continue

        results.append({
            "product_id": pid,
            "title": titles[pid],
            "baseline_unit_price": best["before_price"],
            "current_unit_price": best["after_price"],
            "change_pct": round(best["change_pct"], 5),
            "detected_from": best["detected_from"],
            "affected_days": best["days_after"],
        })

    results.sort(key=lambda x: abs(x["change_pct"]), reverse=True)
    return results


def stockout_scan(start: str, end: str) -> list[dict]:
    """列出区间内发生过缺货的商品（含缺货天数）。"""
    rows = readonly_query(
        """
        SELECT
            i.product_id,
            p.title,
            SUM(i.stockout_flag)                       AS stockout_days,
            MIN(CASE WHEN i.stockout_flag = 1 THEN i.stat_date END) AS first_stockout,
            MAX(CASE WHEN i.stockout_flag = 1 THEN i.stat_date END) AS last_stockout
        FROM fact_daily_inventory i
        JOIN dim_product p ON p.product_id = i.product_id
        WHERE i.stat_date BETWEEN ? AND ?
        GROUP BY i.product_id, p.title
        HAVING SUM(i.stockout_flag) > 0
        ORDER BY stockout_days DESC
        """,
        (start, end),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 渠道与投放
# ---------------------------------------------------------------------------

def channel_breakdown(start: str, end: str) -> list[dict]:
    """
    渠道结构分析（含投放 ROI）。

    ★ ROI 只能在这里算 —— 因为只有 fact_daily_traffic 有 ad_spend。
      千万不要 join fact_daily_product，那会让 GMV 翻倍。
    """
    rows = readonly_query(
        """
        SELECT
            channel,
            SUM(visitors)   AS visitors,
            SUM(orders)     AS orders,
            SUM(gmv)        AS gmv,
            ROUND(SUM(ad_spend), 2) AS ad_spend
        FROM fact_daily_traffic
        WHERE stat_date BETWEEN ? AND ?
        GROUP BY channel
        ORDER BY gmv DESC
        """,
        (start, end),
    )

    total_visitors = sum(r["visitors"] or 0 for r in rows) or 1
    total_gmv = sum(r["gmv"] or 0.0 for r in rows) or 1.0

    out = []
    for r in rows:
        gmv = r["gmv"] or 0.0
        spend = r["ad_spend"] or 0.0
        visitors = r["visitors"] or 0
        orders = r["orders"] or 0
        out.append({
            "channel": r["channel"],
            "visitors": visitors,
            "visitor_share": round(visitors / total_visitors, 5),
            "gmv": round(gmv, 2),
            "gmv_share": round(gmv / total_gmv, 5),
            "orders": orders,
            "cvr": round(orders / visitors, 5) if visitors else 0.0,
            "aov": round(gmv / orders, 2) if orders else 0.0,
            "ad_spend": round(spend, 2),
            "roi": round(gmv / spend, 3) if spend else None,
        })
    return out


def ad_efficiency_trend(bucket_days: int = 7, exclude_special: bool = True) -> list[dict]:
    """
    投放效率趋势：按 bucket_days 分段看花费与 ROI 的关系。

    用途：判断"加预算导致 ROI 下滑"（边际递减）还是"渠道本身变差"。
    区别很重要 —— 前者应优化投放结构，后者才考虑关停。

    ★ exclude_special 默认 True，这一点是整个项目最值得记住的教训之一：
      最初此函数不排除大促，结果 618 那一周的花费（12.8 万，是平时的 19 倍）
      落在第一个分桶里，把"前段平均花费"抬到天上，
      于是算出来花费是「下降 -75%」—— 完全反了。
      靠这份数据做趋势判断，永远得不出"花费在涨"的结论。
      **质控工具自己必须先做质控**，否则它会变成假信息的源头。
      大促应作为独立事件单独分析，不能混进趋势基线。
    """
    _, d_max = get_data_date_range()
    if not d_max:
        return []
    end = date.fromisoformat(d_max)
    # 多取 60 天缓冲，保证剔除大促后仍有足够天数
    start = end - timedelta(days=150)

    extra, params = ("", [])
    if exclude_special:
        extra, params = _not_in_special_clause()

    rows = readonly_query(
        f"""
        SELECT stat_date, SUM(ad_spend) AS spend, SUM(gmv) AS gmv
        FROM fact_daily_traffic
        WHERE stat_date BETWEEN ? AND ?{extra}
        GROUP BY stat_date
        ORDER BY stat_date
        """,
        tuple([start.isoformat(), end.isoformat()] + params),
    )

    # 只保留最后 90 个「有效交易日」，避免缓冲期影响分段
    rows = rows[-90:]

    buckets: list[dict] = []
    cur_spend = cur_gmv = 0.0
    cur_days: list[str] = []
    for i, r in enumerate(rows, start=1):
        cur_spend += r["spend"] or 0.0
        cur_gmv += r["gmv"] or 0.0
        cur_days.append(r["stat_date"])
        if i % bucket_days == 0:
            buckets.append({
                "period": f"{cur_days[0]} ~ {cur_days[-1]}",
                "days": len(cur_days),
                "ad_spend": round(cur_spend, 2),
                "gmv": round(cur_gmv, 2),
                "roi": round(cur_gmv / cur_spend, 3) if cur_spend else None,
            })
            cur_spend = cur_gmv = 0.0
            cur_days = []

    # 尾巴不足一个 bucket 也保留，但标注天数
    if cur_days:
        buckets.append({
            "period": f"{cur_days[0]} ~ {cur_days[-1]}",
            "days": len(cur_days),
            "ad_spend": round(cur_spend, 2),
            "gmv": round(cur_gmv, 2),
            "roi": round(cur_gmv / cur_spend, 3) if cur_spend else None,
        })

    # 顺带算出环比变化，方便 Agent 直接引用
    for i in range(1, len(buckets)):
        prev, cur = buckets[i - 1], buckets[i]
        if prev["ad_spend"]:
            buckets[i]["spend_change_pct"] = round(
                (cur["ad_spend"] - prev["ad_spend"]) / prev["ad_spend"], 5)
        if prev["roi"]:
            buckets[i]["roi_change_pct"] = round(
                (cur["roi"] - prev["roi"]) / prev["roi"], 5)
    return buckets


def cross_breakdown(dimension: str = "category", metric: str = "gmv",
                    start: str = "", end: str = "") -> dict | list[dict]:
    """
    ★ 交叉维度聚合：按「类目 × 渠道」这类组合拆解指标。

    为什么需要它？—— 因为这是**固定工具覆盖不了的空白**，
    而让模型自己写 SQL 又会踩坑：

      实测中用户问"按商品类目拆分各渠道的 GMV"，需要三表 JOIN
      （商品事实表取数、维表取类目、流量表取渠道且无直接外键）。
      模型连续写错三次：t.product_id（别名不存在）、
      f.channel（channel 不在商品事实表）、甚至去查 information_schema，
      最终触发降级、没能作答。

      根因不是模型笨，而是**这个关联关系确实绕**：
      类目在维表、渠道在另一张事实表，两张事实表之间没有 product 级外键，
      只能按 stat_date 关联。靠 prompt 教它写这种三方关联，
      成功率远低于直接给它一个封装好的工具。

    ★ 设计原则：**能封装成工具的能力，不要让模型每次现场推导。**
      模型的强项是"决定要看什么"，弱项是"记住这张表的关联细节"。
      把后者固化成代码，前者留给模型。

    参数：
        dimension: "category"（按类目）或 "channel"（按渠道）
        metric:    gmv | paid_orders | visitors | impressions | refund_amount
        start/end: 日期区间，留空取最后一个完整自然周
    """
    if not (start and end):
        start, end = latest_complete_week()

    if dimension not in {"category", "channel"}:
        return {"error": f"不支持的维度：{dimension}",
                "supported": ["category", "channel"]}

    metric_col = {
        "gmv": "f.gmv", "paid_orders": "f.paid_orders",
        "visitors": "f.visitors", "impressions": "f.impressions",
        "refund_amount": "f.refund_amount",
    }.get(metric)
    if metric_col is None:
        return {"error": f"不支持的指标：{metric}",
                "supported": ["gmv", "paid_orders", "visitors",
                              "impressions", "refund_amount"]}

    # ★ 关键：本数据集的两张事实表**只能按 stat_date 关联**，
    #   它们之间没有 product×channel 粒度的事实表。
    #   直接 JOIN 会产生笛卡尔积：同一天每个商品 × 每个渠道组合都成立，
    #   于是 GMV 被放大 N 倍（N = 渠道数），且同一类目在各渠道的数字完全相同。
    #
    #   所以这里不直接 SUM，而是用「类目结构 × 渠道结构」做近似重构：
    #     某类目在某渠道的估算值 = 该类目的类目占比 × 该渠道的总 GMV
    #   这样每个渠道的合计等于该渠道真实 GMV，类目占比也保持真实。
    #
    #   ★ 这是必须显式说明的假设，不能假装是精确联表结果 ——
    #     否则用户会拿一个数字上和却含义失真的表格去做决策。
    #     如果你的真实数仓存在商品×渠道粒度的事实表，
    #     把下面这段替换成直接 JOIN 即可（那样才是精确值）。
    cat_rows = readonly_query(
        f"""
        SELECT p.category AS category, SUM({metric_col}) AS v
        FROM fact_daily_product f
        JOIN dim_product p ON p.product_id = f.product_id
        WHERE f.stat_date BETWEEN ? AND ?
        GROUP BY p.category
        """,
        (start, end),
    )
    ch_rows = readonly_query(
        """
        SELECT channel, SUM(gmv) AS gmv, SUM(visitors) AS visitors,
               SUM(orders) AS orders, SUM(ad_spend) AS ad_spend
        FROM fact_daily_traffic
        WHERE stat_date BETWEEN ? AND ?
        GROUP BY channel
        """,
        (start, end),
    )

    cat_total = sum(r["v"] or 0 for r in cat_rows) or 1
    cells = []
    for c in cat_rows:
        cat_share = (c["v"] or 0) / cat_total
        for ch in ch_rows:
            # 渠道侧的代理基数：GMV 用 gmv，其余指标用各自真实列
            base = {
                "gmv": ch["gmv"], "paid_orders": ch["orders"],
                "visitors": ch["visitors"], "ad_spend": ch["ad_spend"],
            }.get(metric)
            if metric in ("impressions", "refund_amount"):
                # 这两个指标在流量表里没有对应列，退化为按 GMV 结构分摊
                base = ch["gmv"]
            cells.append({
                "category": c["category"],
                "channel": ch["channel"],
                metric: round((base or 0) * cat_share, 2),
                "category_share": round(cat_share, 5),
            })

    cells.sort(key=lambda x: x.get(metric, 0), reverse=True)

    return {
        "period": f"{start} ~ {end}",
        "rows": "category",
        "columns": "channel",
        "metric": metric,
        "cell_count": len(cells),
        "data": cells,
        "method": "结构重构（非直接联表）",
        "_note": (
            "★ 本数据集没有「商品 × 渠道」粒度的事实表，两张事实表只能按 "
            "stat_date 关联，直接 JOIN 会产生笛卡尔积并放大数值。"
            "因此本工具使用「类目结构 × 渠道结构」重构："
            "各渠道合计等于真实渠道值，类目占比也真实，"
            "但**单个交叉格是估算值**，只可用于看结构分布，"
            "不可用于汇报精确数字。精确值请用 get_product_breakdown"
            "（按商品）或 get_channel_breakdown（按渠道）。"
        ),
    }


def competitor_price_context(category: str, days: int = 30) -> list[dict]:
    """竞品价格走势（最近 N 天按周聚合），用于比价归因。"""
    _, d_max = get_data_date_range()
    if not d_max:
        return []
    end = date.fromisoformat(d_max)
    start = end - timedelta(days=days - 1)

    return readonly_query(
        """
        SELECT competitor,
               MIN(stat_date) AS from_date,
               MAX(stat_date) AS to_date,
               ROUND(AVG(avg_price), 2) AS avg_price
        FROM fact_daily_competitor
        WHERE category = ? AND stat_date BETWEEN ? AND ?
        GROUP BY competitor
        ORDER BY avg_price
        """,
        (category, start.isoformat(), end.isoformat()),
    )
