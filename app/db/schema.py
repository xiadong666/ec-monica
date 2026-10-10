"""
app/db/schema.py
================
数据层建表语句：模拟一个中小电商的四张核心表。

设计要点（决定了后面所有指标能不能算对）：
1. 事实表必须带「日期 + 商品」粒度，且金额与订单同时记录，
   否则 GMV / AOV / 退款率的口径会互相打架；
2. 退款率用 orders 表里的 refund_amount，而不是另开一张表，
   避免 join 时行数翻倍导致 GMV 被重复计算；
3. 流量表按「渠道」拆分，方便做流量结构归因；
4. 库存表记录每天的库存快照，这是识别"缺货导致的转化下跌"的唯一证据 —— 
   没有这张表，Agent 永远会把缺货当成"详情页不行"。
"""

# --- 商品维表 ---
DDL_DIM_PRODUCT = """
CREATE TABLE IF NOT EXISTS dim_product (
    product_id      TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    category        TEXT NOT NULL,
    price           REAL NOT NULL,          -- 当前售价
    cost            REAL NOT NULL,          -- 成本价（算毛利用）
    launch_date     TEXT NOT NULL,          -- 上架日期
    lifecycle       TEXT NOT NULL           -- 生命周期：新品/成长/成熟/衰退
);
"""

# --- 商品日粒度事实表（核心表）---
DDL_FACT_DAILY = """
CREATE TABLE IF NOT EXISTS fact_daily_product (
    stat_date       TEXT NOT NULL,          -- 日期 YYYY-MM-DD
    product_id      TEXT NOT NULL,
    impressions     INTEGER NOT NULL,       -- 曝光
    clicks          INTEGER NOT NULL,       -- 点击
    visitors        INTEGER NOT NULL,       -- 访客数(UV)
    paid_orders     INTEGER NOT NULL,       -- 支付订单数
    paid_units      INTEGER NOT NULL,       -- 支付件数
    gmv             REAL NOT NULL,          -- 成交额（未扣退款）
    refund_amount   REAL NOT NULL,          -- 当日退款金额
    -- ★ 当日实际售价快照。这一列不能省，它解决两个问题：
    --   ① 改价探测：GMV/件数 会被"一次买几件"的件数结构干扰，不是价格信号；
    --      只有独立记录的价格列才能可靠发现改价。
    --   ② 历史毛利率：dim_product.price 是"当前价"，用它倒推历史毛利会算错。
    unit_price      REAL NOT NULL,
    PRIMARY KEY (stat_date, product_id),
    FOREIGN KEY (product_id) REFERENCES dim_product(product_id)
);
"""

# --- 渠道流量日表 ---
DDL_FACT_TRAFFIC = """
CREATE TABLE IF NOT EXISTS fact_daily_traffic (
    stat_date       TEXT NOT NULL,
    channel         TEXT NOT NULL,          -- 自然搜索/推荐/付费/私域/直播
    visitors        INTEGER NOT NULL,
    orders          INTEGER NOT NULL,
    gmv             REAL NOT NULL,
    ad_spend        REAL NOT NULL,          -- 该渠道当日投放花费
    PRIMARY KEY (stat_date, channel)
);
"""

# --- 库存日快照 ---
DDL_FACT_INVENTORY = """
CREATE TABLE IF NOT EXISTS fact_daily_inventory (
    stat_date       TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    stock_qty       INTEGER NOT NULL,       -- 当日库存
    stockout_flag   INTEGER NOT NULL,       -- 1=缺货(库存为0)
    PRIMARY KEY (stat_date, product_id)
);
"""

# --- 竞品价格日快照（做比价归因用）---
DDL_FACT_COMPETITOR = """
CREATE TABLE IF NOT EXISTS fact_daily_competitor (
    stat_date       TEXT NOT NULL,
    competitor      TEXT NOT NULL,
    category        TEXT NOT NULL,
    avg_price       REAL NOT NULL,
    PRIMARY KEY (stat_date, competitor, category)
);
"""

# --- 检索索引：指标层和数据探查都靠日期+商品过滤 ---
DDL_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_daily_date ON fact_daily_product(stat_date);",
    "CREATE INDEX IF NOT EXISTS idx_daily_prod ON fact_daily_product(product_id);",
    "CREATE INDEX IF NOT EXISTS idx_traffic_date ON fact_daily_traffic(stat_date);",
    "CREATE INDEX IF NOT EXISTS idx_inv_date ON fact_daily_inventory(stat_date);",
    "CREATE INDEX IF NOT EXISTS idx_inv_prod ON fact_daily_inventory(product_id);",
]

ALL_DDL = [
    DDL_DIM_PRODUCT,
    DDL_FACT_DAILY,
    DDL_FACT_TRAFFIC,
    DDL_FACT_INVENTORY,
    DDL_FACT_COMPETITOR,
    *DDL_INDEXES,
]

# 只读 SQL 工具的白名单：只有这些表允许被 Agent 查询
ALLOWED_TABLES = {
    "dim_product",
    "fact_daily_product",
    "fact_daily_traffic",
    "fact_daily_inventory",
    "fact_daily_competitor",
}


# ===========================================================================
# DDL 方言化
# ===========================================================================
#
# ★ 为什么需要这一层？
#   因为 SQLite 与 PostgreSQL 存在"同名不同类型"的差异，而这类差异
#   **不会报错，只会让数据悄悄变形** —— 是最危险的一种不兼容。
#
#   ① REAL 的类型差异（本项目会遇到的最严重的坑）
#        SQLite  : REAL = 8 字节 IEEE 浮点，存金额精度足够
#        Postgres: REAL = **4 字节**浮点，精度约 7 位有效数字
#                  → 123012.88 会变成 123012.875 之类
#        正确做法：PostgreSQL 用 DOUBLE PRECISION（8 字节）
#
#      影响面：金额字段的精度损失会被静默放大到所有下游指标
#              （GMV 求和、客单价、毛利的绝对值都会偏），
#              而且"只差几分钱"极难在测试中发现。
#              必须用范围断言而不是等值断言才能捕获。
#
#   ② 其余类型（TEXT / INTEGER）两边语义一致，无需转换
#   ③ CREATE TABLE/INDEX IF NOT EXISTS 两边都支持

# SQLite 类型 → PostgreSQL 类型
_TYPE_MAP_SQLITE_TO_PG = {
    "REAL": "DOUBLE PRECISION",
    "BLOB": "BYTEA",
    # TEXT / INTEGER 语义一致，保持原样
}


def ddl_for(ddl: str, dialect: str) -> str:
    """
    把 DDL 转成目标方言。

    参数：
        ddl     : 原始（SQLite 风格）DDL
        dialect : "sqlite" | "postgresql"
    """
    if dialect != "postgresql":
        return ddl

    import re

    out = ddl
    # 按长度倒序替换，避免短模式先命中长模式的一部分
    for sqlite_type in sorted(_TYPE_MAP_SQLITE_TO_PG, key=len, reverse=True):
        pg_type = _TYPE_MAP_SQLITE_TO_PG[sqlite_type]
        # 只替换作为**独立类型词**出现的（前后是空白/括号/逗号），
        # 避免误伤列名里恰好包含这些字母的情况
        out = re.sub(
            rf"(?<=[\s(]){sqlite_type}(?=[\s,)\n])",
            pg_type,
            out,
        )
    return out


def all_ddl_for(dialect: str) -> list[str]:
    """返回目标方言的全部 DDL。"""
    return [ddl_for(d, dialect) for d in ALL_DDL]
