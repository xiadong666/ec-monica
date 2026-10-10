"""
app/db/database.py
==================
数据层：连接管理 + 建库 + 只读查询封装。

★ 双后端支持（SQLite / PostgreSQL）
    本模块**不直接依赖任何一种数据库驱动**，所有连接获取都走
    `app/db/adapter.py` 的适配层。业务代码与 SQL 全部保持统一写法
    （位置参数用 `?`），方言差异（占位符、只读强制方式、元数据查询）
    由适配层吸收。

    切换后端只需一个环境变量：
        EC_DB_BACKEND=sqlite    （默认，用 EC_DB_PATH）
        EC_DB_BACKEND=postgres  （用 DATABASE_URL）

关键设计：**读写分离**
    - `get_conn()`       ：普通连接，仅供 seed 脚本 / 初始化使用；
    - `readonly_query()` ：Agent 与指标层唯一允许使用的入口。

    ★ 只读的实现在两种后端上是**等价语义**的，都是"服务端/驱动层强制"：
        SQLite     → 文件 URI 的 `mode=ro`
        PostgreSQL → 会话变量 `default_transaction_read_only = on`
      这比"正则过滤 DROP"强得多：即使 SQL 校验被绕过，
      数据库本身也会拒绝写入。
"""
import re
from pathlib import Path

from app.config import settings
from app.db.adapter import Connection, DbConfig, open_connection
from app.db.schema import ALL_DDL, ALLOWED_TABLES, ddl_for


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def db_config() -> DbConfig:
    """
    当前数据库配置（每次读取，便于测试中动态切换环境变量）。

    ★ 不缓存成模块级常量：因为测试需要切换后端，
      缓存会让"切换后仍连旧库"这种问题很难排查。
      配置读取本身是 os.getenv，开销可忽略。
    """
    return DbConfig(db_path=settings.DB_PATH)


def current_backend() -> str:
    """当前后端名（供日志/健康检查/迁移脚本使用）。"""
    try:
        return db_config().backend
    except ValueError:
        return "unknown"


def get_conn(writable: bool = True) -> Connection:
    """
    获取连接。

    参数 writable=False 时返回只读连接 —— 生产代码应优先使用
    readonly_query()，不要直接用本函数。
    """
    return open_connection(db_config(), readonly=not writable)


# ---------------------------------------------------------------------------
# 初始化
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    建表 + 建索引（幂等，可重复调用）。

    ★ DDL 也必须方言化：
      SQLite 的 `TEXT`/`REAL` 与 PostgreSQL 的 `TEXT`/`DOUBLE PRECISION`
      不完全等价（尤其 REAL 在 PG 里是 4 字节浮点，
      存金额会有精度问题）。schema.py 的 ddl_for() 负责转换。
    """
    cfg = db_config()
    if cfg.backend == "sqlite":
        Path(cfg.db_path).parent.mkdir(parents=True, exist_ok=True)

    conn = open_connection(cfg, readonly=False, for_setup=True)
    try:
        dialect = cfg.dialect.name
        for ddl in ALL_DDL:
            conn.execute(ddl_for(ddl, dialect))
        conn.commit()
    finally:
        conn.close()


def _connect_readonly() -> Connection:
    """以只读模式打开数据库。写操作在此连接上必然失败（服务端强制）。"""
    return open_connection(db_config(), readonly=True)


# ---------------------------------------------------------------------------
# 护栏：SQL 安全检查
# ---------------------------------------------------------------------------

# 只允许单条 SELECT / WITH 开头的查询
_SELECT_ONLY = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)
# 明确拒绝的写操作与危险关键字
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|"
    r"pragma|vacuum|reindex|grant|revoke)\b",
    re.IGNORECASE,
)
# 从 SQL 中提取被引用的表名
_TABLE_REF = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.IGNORECASE)


class SQLRejected(Exception):
    """SQL 被护栏拒绝。"""


def validate_sql(sql: str, max_limit: int = 500) -> str:
    """
    校验并规范化 SQL，返回可执行的 SQL。不合规则抛 SQLRejected。

    四道关卡（对应四条护栏）：
      1. 只允许 SELECT / WITH —— 挡住写操作；
      2. 关键字黑名单 —— 挡住注释绕过、ATTACH、PRAGMA 等；
      3. 表名白名单 —— 挡住访问未授权表；
      4. 强制补 LIMIT —— 防止一条查询把内存打爆。
    """
    if not sql or not sql.strip():
        raise SQLRejected("SQL 为空")

    # 去掉行注释，避免用注释拼接绕过关键字检测
    cleaned = re.sub(r"--[^\n]*", " ", sql)
    # 拒绝多语句（分号后还有内容）
    stripped = cleaned.strip().rstrip(";")
    if ";" in stripped:
        raise SQLRejected("不允许多条语句，请只提交一条 SELECT 查询")

    if not _SELECT_ONLY.match(stripped):
        raise SQLRejected("只允许 SELECT / WITH 查询")

    hit = _FORBIDDEN.search(stripped)
    if hit:
        raise SQLRejected(f"包含被禁止的关键字：{hit.group(0).upper()}")

    tables = {t.lower() for t in _TABLE_REF.findall(stripped)}
    illegal = tables - ALLOWED_TABLES
    if illegal:
        raise SQLRejected(
            f"访问了未授权的表：{sorted(illegal)}；可用表：{sorted(ALLOWED_TABLES)}"
        )
    if not tables:
        raise SQLRejected("未识别到任何数据表，请使用 FROM 指定表名")

    # 统一大写匹配 LIMIT（WITH 子句里的 LIMIT 不干扰，这里只看是否已存在）
    if not re.search(r"\blimit\b", stripped, re.IGNORECASE):
        stripped = f"{stripped} LIMIT {max_limit}"

    return stripped


def readonly_query(sql: str, params: tuple = (), max_limit: int = 500) -> list[dict]:
    """
    经护栏校验后执行只读查询，返回 list[dict]。

    这是 Agent 取数的唯一入口。任何异常都会转成可读错误信息，
    方便把错误回灌给模型让它自我修正（而不是让服务直接 500）。

    ★ 双后端说明：
      · SQL 继续用 `?` 写占位符 —— 适配层会转成 PostgreSQL 的 %s；
      · 只读由**服务端/驱动层**强制（SQLite mode=ro / PG read_only 会话变量），
        不依赖本函数的校验。校验只是"第一道"，不是"唯一一道"。
    """
    safe_sql = validate_sql(sql, max_limit=max_limit)

    conn = _connect_readonly()
    try:
        return conn.query(safe_sql, params)
    finally:
        conn.close()


def join_guide() -> str:
    """
    ★ 表关系与命名约定说明。

    为什么需要单独写这一段？—— 因为实测中模型写 JOIN 时会凭常识编列名：
        · 商品名写成 p.product_name / p.name  → 真实列名是 p.title
        · 日期写成 date                      → 真实列名是 stat_date
        · 用 USING (date) 关联事实表与维表     → dim_product 根本没有日期列
    这些错误不是 schema 缺信息，而是**模型在用通用电商系统的命名直觉套本项目**。
    所以要在它写 SQL 之前就把「关联键是什么、哪些名字不存在」讲清楚，
    而不是等它写错再报错重试 —— 一次工具调用就是几秒和一次 token。

    这段文本会随 describe_tables 一起注入，是降低 SQL 报错率最有效的一处改动。
    """
    return """【表关系与命名约定 —— 写 JOIN 前必读】

■ 关联键（JOIN 全部通过 product_id，且必须用 ON 而不是 USING）
    fact_daily_product.product_id   →  dim_product.product_id
    fact_daily_inventory.product_id →  dim_product.product_id
    fact_daily_product.stat_date    →  fact_daily_inventory.stat_date
    fact_daily_product.stat_date    →  fact_daily_traffic.stat_date
    （事实表之间按 stat_date 关联；维表没有日期列，不要拿它关联日期）

■ 日期列名统一是 stat_date，**不存在名为 date 的列**
    除 dim_product 外，每张表都有 stat_date。
    dim_product 是维表，**没有任何日期列** —— 需要按时间筛选时必须 JOIN 事实表。

■ 商品名称列是 dim_product.title，**不存在 product_name 或 name**
    商品维表字段：product_id / title / category / price / cost / launch_date / lifecycle

■ 正确写法示例
    SELECT p.title, SUM(f.gmv) AS gmv
    FROM fact_daily_product f
    JOIN dim_product p ON p.product_id = f.product_id
    WHERE f.stat_date BETWEEN '2025-08-18' AND '2025-08-24'
    GROUP BY p.title
    ORDER BY gmv DESC;

■ 常见错误（都会被拒绝，不要写）
    ✗ p.product_name / p.name        → 应为 p.title
    ✗ date / f.date                  → 应为 f.stat_date
    ✗ JOIN dim_product USING (date)  → dim_product 无日期列，应用 ON p.product_id = f.product_id
    ✗ SELECT * FROM ... 不带日期条件  → 结果太多且容易被强制 LIMIT 截断，失去意义
"""


def suggest_column(bad_name: str) -> list[str]:
    """
    对写错的列名给出最接近的真实列名（模糊匹配）。

    这是"让错误信息可自我修正"的关键一步：
    模型看到 "no such column: product_name" 时并不知道正确名字是什么，
    但它看到 "是否想用 title？" 就能立刻改对，不必白跑一轮。
    """
    import difflib

    # ★ 候选列名必须覆盖 schema 的全部真实列。
    #   实测踩过的坑：最初漏了 product_id，导致模型写
    #   `t.product_id`（t 这个别名不存在）时报错却给不出任何建议，
    #   模型只能瞎猜，又白跑一轮。
    all_cols = [
        # dim_product
        "product_id", "title", "category", "price", "cost",
        "launch_date", "lifecycle",
        # fact_daily_product
        "stat_date", "impressions", "clicks", "visitors",
        "paid_orders", "paid_units", "gmv", "refund_amount", "unit_price",
        # fact_daily_traffic
        "channel", "orders", "ad_spend",
        # fact_daily_inventory
        "stock_qty", "stockout_flag",
        # fact_daily_competitor
        "competitor", "avg_price",
    ]

    # 高频错误的精确映射（比模糊匹配更可靠）
    exact = {
        "date": ["stat_date"],
        "product_name": ["title"],
        "name": ["title"],
        "product_title": ["title"],
        "title_name": ["title"],
        "amount": ["gmv"],
        "revenue": ["gmv"],
        "sales": ["gmv"],
        "sales_amount": ["gmv"],
        "qty": ["stock_qty", "paid_units"],
        "quantity": ["stock_qty"],
        "inventory": ["stock_qty"],
        "stock": ["stock_qty"],
        "spend": ["ad_spend"],
        "cost_spend": ["ad_spend"],
        "refund": ["refund_amount"],
        "category_name": ["category"],
        "cat": ["category"],
        "sku": ["product_id"],
        "item_id": ["product_id"],
        "goods_id": ["product_id"],
    }
    # 去掉表别名前缀：t.product_id -> product_id
    key = bad_name.lower().split(".")[-1].strip()
    if key in exact:
        return exact[key]

    return difflib.get_close_matches(key, all_cols, n=3, cutoff=0.5)


def all_column_names() -> list[str]:
    """
    全部真实列名（供"完全编造的列名"场景做兜底提示）。

    ★ 为什么需要兜底？
      高频错误（product_name / date / amount）有精确映射表，
      difflib 模糊匹配也能覆盖拼写接近的情况。
      但如果模型编了一个**完全不相干**的名字（如 bad_col、foo），
      两者都给不出建议 —— 模型只看到"列名不存在"，
      却不知道该用什么，只能继续瞎猜，白耗轮次。

      此时唯一有用的信息就是"到底有哪些列"。列数不多（20 多个），
      全列出来比让模型再调一次 describe_tables 更省一轮。
    """
    return [
        "product_id", "title", "category", "price", "cost",
        "launch_date", "lifecycle",
        "stat_date", "impressions", "clicks", "visitors",
        "paid_orders", "paid_units", "gmv", "refund_amount", "unit_price",
        "channel", "orders", "ad_spend",
        "stock_qty", "stockout_flag",
        "competitor", "avg_price",
    ]


def friendly_sql_error(err: Exception, sql: str = "") -> dict:
    """
    把 SQLite / PostgreSQL 的原始报错翻译成「模型能照着改」的提示。

    原始报错如 "no such column: p.product_name" 对模型来说信息量很低 ——
    它知道错了，但不知道该改成什么，于是容易反复试错，白白消耗轮次。
    这里补上"正确的列名是什么"，让下一轮直接改对。

    ★ 双后端兼容：
      SQLite     : "no such column: x" / "no such table: x"
      PostgreSQL : 'column "x" does not exist' / 'relation "x" does not exist'
      两种报错文案不同，但语义一致，这里统一处理。
    """
    text = str(err)

    # --- 列名不存在（兼容两种数据库的报错文案）---
    m = (re.search(r"no such column:\s*([A-Za-z_][A-Za-z0-9_.]*)", text)
         or re.search(r'column\s+"?([A-Za-z_][A-Za-z0-9_.]*)"?\s+does not exist',
                      text))
    if m:
        bad = m.group(1)
        suggestions = suggest_column(bad)
        # ★ 兜底：完全编造的列名给不出建议时，列出全部可用列。
        #   宁可多占几十个 token，也不要让模型在"不知道该用什么"的状态下瞎猜。
        if not suggestions:
            suggestions = all_column_names()
            hint = (
                f"数据库中不存在列「{bad}」，且没有相近的列名。"
                f"全部可用列名为：{', '.join(suggestions)}。"
                f"注意：商品名是 dim_product.title（不是 product_name/name），"
                f"日期列是 stat_date（不是 date）。"
                f"请从上述列表中选用正确的列名。"
            )
        else:
            hint = (
                f"数据库中不存在列「{bad}」。"
                f"最接近的真实列名是：{', '.join(suggestions)}。"
                f"注意：商品名是 dim_product.title（不是 product_name/name），"
                f"日期列是 stat_date（不是 date）。"
                f"请先调用 describe_tables 核对完整列名。"
            )
        return {
            "error": f"列名不存在：{bad}",
            "suggestions": suggestions,
            "hint": hint,
        }

    # --- USING 关联失败 ---
    if "cannot join using column" in text:
        col = re.search(r"cannot join using column\s+(\S+)", text)
        name = col.group(1) if col else "该列"
        return {
            "error": f"USING 关联失败：{name} 不是两张表共有的列",
            "hint": (
                f"不要用 USING({name}) 关联。事实表与商品维表应写成："
                "JOIN dim_product p ON p.product_id = f.product_id。"
                "注意 dim_product 没有日期列，按时间筛选必须用事实表的 stat_date。"
            ),
        }

    # --- 表不存在（兼容两种数据库）---
    t = (re.search(r"no such table:\s*(\S+)", text)
         or re.search(r'relation\s+"?([A-Za-z_][A-Za-z0-9_]*)"?\s+does not exist',
                      text))
    if t:
        return {
            "error": f"表不存在：{t.group(1)}",
            "hint": f"可用表：{sorted(ALLOWED_TABLES)}。请先调用 describe_tables。",
        }

    # --- 语法错误 ---
    if "syntax error" in text.lower():
        return {
            "error": f"SQL 语法错误：{text}",
            "hint": "请检查 SQL 语法。日期用字符串字面量，如 "
                    "stat_date BETWEEN '2025-08-18' AND '2025-08-24'。",
        }

    # --- 只读拒绝（PostgreSQL 的 default_transaction_read_only 会返回这个）---
    if "read-only" in text.lower() or "read only" in text.lower():
        return {
            "error": f"写操作被拒绝（连接为只读）：{text}",
            "hint": "本连接是只读的，只能执行 SELECT 查询。"
                    "如需写入请使用数据迁移脚本。",
        }

    # --- 其他 ---
    return {"error": f"查询失败：{type(err).__name__}: {text}", "hint": ""}


def table_schema_text() -> str:
    """
    生成「表结构说明」文本，用于注入 prompt。
    Agent 只有知道列名和含义，才可能写出正确的 SQL。

    ★ 列信息读取走适配层（Connection.table_columns）：
      SQLite 用 PRAGMA table_info，PostgreSQL 用 information_schema。
      这是双后端最典型的差异点，必须封装 —— 
      否则业务代码里会出现 `if backend == 'postgres': ...` 这种分支，
      那是技术债的开始。
    """
    lines = ["【可用数据表】（只能 SELECT 以下表，均已强制 LIMIT）", ""]

    conn: Connection | None = None
    try:
        conn = _connect_readonly()
    except Exception:                                   # noqa: BLE001
        # 数据库还没准备好时不阻断（例如首次启动尚未 seed）
        conn = None

    for table in sorted(ALLOWED_TABLES):
        lines.append(f"■ {table}")
        if conn:
            try:
                for name, col_type in conn.table_columns(table):
                    lines.append(f"    - {name} ({col_type})")
            except Exception as exc:                     # noqa: BLE001
                lines.append(f"    （列信息读取失败：{type(exc).__name__}）")
        lines.append("")

    if conn:
        # 附上几个关键列的取值样例，减少模型瞎猜枚举值
        try:
            cats = conn.query(
                "SELECT DISTINCT category FROM dim_product ORDER BY category")
            lines.append("dim_product.category 取值：" +
                         ", ".join(r["category"] for r in cats))

            chans = conn.query(
                "SELECT DISTINCT channel FROM fact_daily_traffic ORDER BY channel")
            lines.append("fact_daily_traffic.channel 取值：" +
                         ", ".join(r["channel"] for r in chans))

            rng = conn.query(
                "SELECT MIN(stat_date) AS d_min, MAX(stat_date) AS d_max "
                "FROM fact_daily_product")
            if rng:
                lines.append(f"数据日期范围：{rng[0]['d_min']} ~ {rng[0]['d_max']}")
        except Exception as exc:                         # noqa: BLE001
            lines.append(f"（取值样例读取失败：{type(exc).__name__}）")
        finally:
            conn.close()

    lines.append("")
    lines.append("日期格式：'YYYY-MM-DD'。所有表都有 stat_date 列（dim_product 除外）。")
    lines.append("")
    lines.append(join_guide())
    return "\n".join(lines)


def db_health() -> dict:
    """
    数据库健康信息（供健康检查与运维排查）。

    ★ 只暴露后端类型与计数，**不暴露连接串**（可能含密码）。
    """
    info: dict = {"backend": current_backend()}
    try:
        cfg = db_config()
        rows = readonly_query(
            "SELECT COUNT(*) AS n FROM dim_product")
        info["products"] = rows[0]["n"] if rows else 0
        rng = readonly_query(
            "SELECT MIN(stat_date) AS d_min, MAX(stat_date) AS d_max "
            "FROM fact_daily_product")
        if rng:
            info["data_range"] = f"{rng[0]['d_min']} ~ {rng[0]['d_max']}"
        # 脱敏后的连接描述
        from app.db.adapter import _mask_url
        info["target"] = (f"sqlite://{cfg.db_path}" if cfg.backend == "sqlite"
                          else f"postgresql://{_mask_url(cfg.database_url)}")
    except Exception as exc:                            # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info
