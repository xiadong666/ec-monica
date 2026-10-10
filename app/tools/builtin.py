"""
app/tools/builtin.py
====================
把项目里所有工具**注册**到 Registry。

★ 这里是「新增工具的成本」的体现：
    每个工具就是一个普通 Python 函数 + 一个 @registry.register 装饰器。
    Function Schema 由函数签名自动推导，不需要手写 JSON Schema。
    所以扩展一个工具的成本 ≈ 写一个函数 —— 这是这套架构能持续演进的前提。

能力标签（capabilities）是意图路由的依据：
    schema    —— 了解数据结构（几乎所有意图都要）
    metric    —— 指标计算（经营分析/报告）
    drilldown —— 明细下钻（归因分析）
    quality   —— 数据质控（归因分析，防污染）
    sql       —— 自由探索（临时性问题）
"""
from app.db.database import SQLRejected, readonly_query, table_schema_text
from app.metrics.compute import (
    ad_efficiency_trend,
    channel_breakdown,
    competitor_price_context,
    cross_breakdown,
    get_data_date_range,
    latest_complete_week,
    period_compare,
    period_summary,
    price_change_scan,
    product_breakdown,
    stockout_scan,
    previous_week_of,
)
from app.quality.checks import run_all_checks
from app.tools.registry import registry


def resolve_period(start: str = "", end: str = "") -> tuple[str, str]:
    """公共辅助：空参数解析为「最后一个完整自然周」。"""
    if start and end:
        return start, end
    return latest_complete_week()


# ===========================================================================
# schema 类：了解数据结构
# ===========================================================================

# --- 防重复调用：元信息是幂等的，整个流程只需一次 ---
_SCHEMA_TEXT_CACHE: dict = {"value": None, "served": 0}


def reset_meta_tool_cache() -> None:
    """
    重置元信息工具的调用计数。

    ★ 必须在**每次 Agent 运行开始**时调用。
      否则计数会在进程内一直累加，第二个请求一进来就被误判成
      "表结构已提供过"，直接拿到提示而不是真正的 schema ——
      那会让所有后续请求都拿不到表结构，比重复调用严重得多。
    """
    _SCHEMA_TEXT_CACHE["served"] = 0


@registry.register(
    capabilities=["schema"],
    description="查看可用数据表的结构（表名、列名、类型、枚举取值、数据日期范围、"
                "表关系与 JOIN 写法）。写 SQL 或取数之前应先调用它。"
                "★ 整个流程只需调用一次，重复调用不会得到新信息。",
    timeout_s=10,
)
def describe_tables() -> dict:
    """
    返回表结构。

    ★ 防重复设计：表结构是静态的，调第二次不会有新信息。
      但实测中模型会反复调用它（出现过 5 轮里调 5 次的情况），
      白白吃掉宝贵轮次。所以第二次起返回一条明确提示，
      告诉模型"结构已经在你的上下文里了，请直接写 SQL"。
      这是"把浪费变成自愈"的做法 —— 比单纯在 prompt 里写
      "不要重复调用"更可靠，因为 prompt 约束是软的，这里是硬的。
    """
    text = table_schema_text()

    if _SCHEMA_TEXT_CACHE["value"] is None:
        _SCHEMA_TEXT_CACHE["value"] = text

    _SCHEMA_TEXT_CACHE["served"] += 1

    if _SCHEMA_TEXT_CACHE["served"] > 1:
        return {
            "ok": True,
            "already_provided": True,
            "note": "表结构在本次会话中已提供过，内容未变化。",
            "hint": (
                "请不要再调用 describe_tables —— 表结构已在你上文的上下文中。"
                "直接基于已知列名写 SQL：商品名是 dim_product.title，"
                "日期列是 stat_date。若上一条 SQL 报错，"
                "请根据错误提示中的建议列名修正后重试。"
            ),
            "schema": text,
        }

    return {"ok": True, "schema": text, "already_provided": False}


@registry.register(
    capabilities=["schema"],
    description="查询数据库实际覆盖的日期范围、最后一个完整自然周及其上一周。"
                "任何取数之前都应先调用，避免查询不存在的区间被误判为业务下滑。",
    timeout_s=10,
)
def get_data_range() -> dict:
    d_min, d_max = get_data_date_range()
    w_start, w_end = latest_complete_week()
    p_start, p_end = previous_week_of(w_start, w_end)
    return {
        "data_range": f"{d_min} ~ {d_max}",
        "latest_complete_week": f"{w_start} ~ {w_end}",
        "previous_week": f"{p_start} ~ {p_end}",
        "note": "做环比请用完整自然周，半周与整周对比没有意义。",
    }


# ===========================================================================
# metric 类：指标计算（数字的唯一可信来源）
# ===========================================================================

@registry.register(
    capabilities=["metric"],
    description="对比某周与上一周的核心指标（GMV、净GMV、订单数、客单价、转化率、"
                "退款率、毛利率），自动剔除大促等特殊日期。做周环比的标准入口，"
                "不要自己写 SQL 算。参数留空则取最后一个完整自然周。",
)
def compare_weeks(start: str = "", end: str = "") -> dict:
    s, e = resolve_period(start, end)
    return period_compare(s, e, exclude_special=True)


@registry.register(
    capabilities=["metric"],
    description="查询指定区间的店铺级核心指标。exclude_special=True 会剔除大促等"
                "特殊日期，做日常水平对比时必须为 True。",
)
def get_period_summary(start: str, end: str, exclude_special: bool = True) -> dict:
    return period_summary(start, end, exclude_special=exclude_special)


@registry.register(
    capabilities=["metric"],
    description="查看投放花费与 ROI 的分段趋势（已自动剔除大促，避免趋势被污染）。"
                "用于判断「加预算导致边际递减」还是「渠道效率真实恶化」——"
                "两者都表现为 ROI 下降，但应对动作完全相反。",
)
def get_ad_trend(bucket_days: int = 7) -> list[dict]:
    return ad_efficiency_trend(bucket_days)


# ===========================================================================
# drilldown 类：明细下钻
# ===========================================================================

@registry.register(
    capabilities=["drilldown"],
    description="按商品下钻核心指标，含缺货天数(stockout_days)与实际售价(stat_price)。"
                "排查转化率/销售额问题时必用：能直接区分「缺货导致」与「改价导致」。",
)
def get_product_breakdown(start: str = "", end: str = "") -> list[dict]:
    s, e = resolve_period(start, end)
    return product_breakdown(s, e)


@registry.register(
    capabilities=["drilldown"],
    description="按渠道下钻流量与GMV结构，并计算投放 ROI（仅付费/直播渠道有 ROI）。"
                "注意 ROI 只能从这里获取，不要 join 商品表自己算，会算错（GMV 会翻倍）。",
)
def get_channel_breakdown(start: str = "", end: str = "") -> list[dict]:
    s, e = resolve_period(start, end)
    return channel_breakdown(s, e)


@registry.register(
    capabilities=["drilldown", "quality"],
    description="扫描区间内发生过缺货的商品。转化率下跌时**必须**先调用它排除缺货因素。",
)
def scan_stockout(start: str = "", end: str = "") -> list[dict]:
    s, e = resolve_period(start, end)
    return stockout_scan(s, e)


@registry.register(
    capabilities=["drilldown", "quality"],
    description="扫描近期实际售价发生跳变（改价）的商品。涨价会同时压低转化率、"
                "抬高客单价，是转化率下跌的常见真因。days 为回溯天数。",
)
def scan_price_change(days: int = 60) -> list[dict]:
    return price_change_scan(days=days)


@registry.register(
    capabilities=["drilldown"],
    description="★ 交叉维度聚合：按「类目 × 渠道」拆解指标。"
                "当用户要的是**交叉组合**（如'各类目在各渠道的 GMV'）时用这个，"
                "不要自己写多表 JOIN —— 类目在维表、渠道在另一张事实表，"
                "关联关系容易写错。"
                "dimension 固定为 category（行维度）；"
                "metric 取值：gmv / paid_orders / visitors / impressions / refund_amount",
)
def get_cross_breakdown(dimension: str = "category", metric: str = "gmv",
                        start: str = "", end: str = "") -> dict:
    return cross_breakdown(dimension=dimension, metric=metric, start=start, end=end)


@registry.register(
    capabilities=["drilldown"],
    description="查询某类目的竞品价格水平，用于比价归因。"
                "category 取值：家居 / 数码 / 美妆 / 户外 / 家纺 / 运动 / 箱包",
)
def get_competitor_price(category: str, days: int = 30) -> list[dict]:
    return competitor_price_context(category, days)


# ===========================================================================
# quality 类：数据质控（归因分析的核心护栏）
# ===========================================================================

@registry.register(
    capabilities=["quality"],
    description="运行全部数据质控检查，返回数据质量问题清单（缺货、改价、大促污染、"
                "样本不足、流量结构变化、投放边际递减）。下结论之前必须先运行它。"
                "返回的 has_blocking 为 true 时，不得给出「策略有效/无效」类因果结论。",
)
def run_quality_checks(start: str = "", end: str = "") -> dict:
    s, e = resolve_period(start, end)
    result = run_all_checks(s, e)
    return {
        "period": f"{s} ~ {e}",
        "has_blocking": result["has_blocking"],
        "blocking_codes": [f["code"] for f in result["blocking"]],
        "flags_text": result["text"],
        "flags": [
            {"code": f["code"], "level": f["level"], "message": f["message"]}
            for f in result["flags"]
        ],
    }


# ===========================================================================
# sql 类：自由探索（受四道护栏约束）
# ===========================================================================

@registry.register(
    capabilities=["sql"],
    description="执行一条只读 SELECT 查询（自动补 LIMIT 500）。用于覆盖指标工具"
                "没有预设的临时探索性问题。只能查询授权表、只能 SELECT、不允许多条语句。"
                "★ 写 JOIN 前请先调用 describe_tables —— 商品名是 dim_product.title"
                "（不是 product_name/name），日期列是 stat_date（不是 date），"
                "dim_product 没有日期列。涉及核心指标口径时优先用专门的指标工具。",
    timeout_s=15,
)
def run_sql(sql: str) -> dict:
    from app.db.database import friendly_sql_error

    try:
        rows = readonly_query(sql)
    except SQLRejected as e:
        return {"ok": False, "error_kind": "data", "error": f"SQL 被拒绝：{e}",
                "hint": "请先调用 describe_tables 查看可用表与列名。"}
    except Exception as e:                              # noqa: BLE001
        # ★ 把 SQLite 原始报错翻译成"模型能照着改"的提示。
        #   裸报错如 "no such column: p.product_name" 只说错了，
        #   不说该改成什么 —— 模型容易反复试错、白耗轮次。
        #   补上"最接近的真实列名"后，下一轮就能直接改对。
        friendly = friendly_sql_error(e, sql)
        return {
            "ok": False,
            "error_kind": "data",
            "error": friendly["error"],
            "expected_columns": friendly.get("suggestions"),
            "hint": friendly.get("hint") or "请先调用 describe_tables 核对列名。",
        }

    if not rows:
        return {"ok": True, "rows": [], "count": 0,
                "hint": "无数据。请确认日期区间是否超出数据范围，先调用 get_data_range。"}
    return {"ok": True, "rows": rows, "count": len(rows)}


# 触发注册（导入即注册）
BUILTIN_TOOL_NAMES = registry.names()