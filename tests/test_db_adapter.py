"""
tests/test_db_adapter.py
========================
数据访问层验收：SQLite / PostgreSQL 双后端适配逻辑。

★ 关键设计：**没有 PostgreSQL 也能验证适配正确性**
    CI 环境（以及大多数开发机）不会跑 PostgreSQL，但这不代表
    "适配层没写错"。以下三类可以在无数据库的情况下验证：

      ① 方言转换     —— 占位符 ? → %s，且不误伤字面量
      ② DDL 转换     —— REAL → DOUBLE PRECISION（精度关键）
      ③ 配置解析     —— 后端选择、连接串脱敏、错误提示

    至于"真的连上 PG 能不能跑"，用一个标记为 pg 的测试覆盖 ——
    有 DATABASE_URL 时才跑，没有则 skip。
    这样 CI 全绿有意义（适配逻辑经过验证），
    而真实 PG 环境的验证也不会被漏掉。

运行：
    pytest tests/test_db_adapter.py -v
    pytest tests/test_db_adapter.py -v -m pg      # 需要 DATABASE_URL
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.adapter import (                                  # noqa: E402
    DbConfig, PostgresConnection, PostgresDialect, SQLiteConnection,
    SQLiteDialect, _mask_url, _replace_outside_literals, _strip_literals,
    backend_name, open_connection,
)
from app.db.schema import DDL_FACT_DAILY, all_ddl_for, ddl_for  # noqa: E402


# ===========================================================================
# 1. 占位符方言转换
# ===========================================================================

@pytest.mark.offline
def test_sqlite_placeholder_unchanged():
    """SQLite 方言不应改写 SQL。"""
    sql = "SELECT * FROM dim_product WHERE product_id = ? AND category = ?"
    assert SQLiteDialect().adapt_sql(sql) == sql


@pytest.mark.offline
def test_postgres_placeholder_converted():
    """PostgreSQL 需要把 ? 转成 %s。"""
    d = PostgresDialect()
    sql = "SELECT * FROM dim_product WHERE product_id = ?"
    assert d.adapt_sql(sql) == \
        "SELECT * FROM dim_product WHERE product_id = %s"


@pytest.mark.offline
def test_placeholder_not_replaced_inside_string_literal():
    """
    ★ 字符串字面量里的问号不能被替换。

    这是最容易出错的地方：无脑 replace("?", "%s") 会把
    LIKE '%?%' 改成 LIKE '%%s%'，静默产生错误的查询。
    而且这种错误不会报错 —— 只是结果不对。
    """
    d = PostgresDialect()
    sql = "SELECT * FROM dim_product WHERE title LIKE '%?%' AND cost > ?"
    out = d.adapt_sql(sql)
    assert "'%?%'" in out, "字面量里的问号被误替换了"
    assert out.endswith("cost > %s"), out


@pytest.mark.offline
def test_placeholder_not_replaced_inside_comment():
    """注释里的问号同样不能被替换。"""
    d = PostgresDialect()
    sql = ("SELECT * FROM dim_product -- 这里有个 ? 号\n"
           "WHERE product_id = ?")
    out = d.adapt_sql(sql)
    assert "-- 这里有个 ? 号" in out
    assert out.rstrip().endswith("= %s")


@pytest.mark.offline
def test_escape_single_quote_in_literal():
    """SQL 里转义的单引号（''）不应干扰掩码。"""
    sql = "SELECT * FROM dim_product WHERE title = 'it''s a ? test' AND cost > ?"
    masked = _strip_literals(sql)
    assert len(masked) == len(sql), "掩码长度必须与原文一致"
    assert masked.count("?") == 1, f"应只识别到 1 个占位符：{masked}"


@pytest.mark.offline
def test_mask_length_invariant_with_chinese():
    """
    ★ 掩码长度不变量（含中文）。

    实测踩过的 bug：行注释循环吞掉了换行符又补了空格，
    导致掩码比原文多一个字符，在含中文的 SQL 上直接 IndexError。
    这类"长度不一致"的问题只在特定输入下暴露。
    """
    samples = [
        "SELECT 1 -- 中文注释\nWHERE x = ?",
        "SELECT '中文' FROM t -- 注释\nWHERE a = ?",
        "SELECT /* 块注释 ? */ 1 FROM t WHERE b = ?",
        "SELECT '带''转义' FROM t WHERE c = ?",
        "SELECT 1 WHERE d = ? -- 末尾注释无换行",
    ]
    for sql in samples:
        masked = _strip_literals(sql)
        assert len(masked) == len(sql), f"长度不一致：{sql!r}"


@pytest.mark.offline
def test_count_placeholders_ignores_literals():
    """占位符计数必须忽略字面量与注释。"""
    d = SQLiteDialect()
    sql = ("SELECT * FROM t WHERE a = ? AND b LIKE '%?%' -- ? 注释\n"
           "AND c = 'x?y' AND d = ?")
    assert d.count_placeholders(sql) == 2


@pytest.mark.offline
def test_replace_outside_literals_directly():
    """直接验证替换函数。"""
    sql = "a = ? AND b = '?' AND c = ?"
    out = _replace_outside_literals(sql, "?", "%s")
    assert out == "a = %s AND b = '?' AND c = %s"


# ===========================================================================
# 2. DDL 方言转换（精度关键）
# ===========================================================================

@pytest.mark.offline
def test_ddl_real_becomes_double_precision():
    """
    ★ REAL → DOUBLE PRECISION 是本项目最关键的一处方言转换。

    PostgreSQL 的 REAL 是 **4 字节**浮点（约 7 位有效数字），
    存金额会丢精度：123012.88 可能变成 123012.875。
    而这类错误不会报错，只会让所有下游金额指标悄悄偏一点 ——
    必须用范围断言才能发现。
    """
    pg_ddl = ddl_for(DDL_FACT_DAILY, "postgresql")
    assert "DOUBLE PRECISION" in pg_ddl
    # 不应还有裸的 REAL（排除 DOUBLE PRECISION 里的部分）
    stripped = pg_ddl.replace("DOUBLE PRECISION", "")
    assert "REAL" not in stripped, f"仍有未转换的 REAL：{pg_ddl}"


@pytest.mark.offline
def test_ddl_sqlite_unchanged():
    """SQLite 方言返回原 DDL。"""
    assert ddl_for(DDL_FACT_DAILY, "sqlite") == DDL_FACT_DAILY


@pytest.mark.offline
def test_ddl_does_not_touch_column_names():
    """转换不能误伤列名（例如列名里含 real 字样）。"""
    ddl = "CREATE TABLE t (real_value TEXT, amount REAL NOT NULL);"
    out = ddl_for(ddl, "postgresql")
    assert "real_value TEXT" in out, "列名被误改了"
    assert "amount DOUBLE PRECISION" in out


@pytest.mark.offline
def test_all_ddl_convertible():
    """
    全部 DDL 都能转换，且都幂等。

    ★ 注意区分两类 DDL：
      · CREATE TABLE —— 含列类型，需检查 REAL → DOUBLE PRECISION
      · CREATE INDEX —— **不含类型**，只需检查幂等性
      实测踩过：最初对两类统一断言"必须含类型词"，
      结果 CREATE INDEX 必然失败 —— 是**断言比实现更严**，测试写错了。
    """
    pg = all_ddl_for("postgresql")
    assert len(pg) >= 10

    table_ddl = [d for d in pg if "CREATE TABLE" in d]
    index_ddl = [d for d in pg if "CREATE INDEX" in d]

    assert table_ddl, "应有建表语句"
    assert index_ddl, "应有建索引语句"
    assert len(table_ddl) + len(index_ddl) == len(pg), "存在未覆盖的 DDL 类型"

    for ddl in pg:
        assert "IF NOT EXISTS" in ddl, f"DDL 不幂等：{ddl[:60]}"

    for ddl in table_ddl:
        assert any(t in ddl for t in ("DOUBLE PRECISION", "TEXT", "INTEGER")), \
            f"建表语句应含类型定义：{ddl[:80]}"

    # 金额列必须转成 DOUBLE PRECISION（精度关键）
    fact = [d for d in table_ddl if "fact_daily_product" in d]
    assert fact, "应有 fact_daily_product 建表语句"
    assert "DOUBLE PRECISION" in fact[0]
    assert "gmv" in fact[0]


# ===========================================================================
# 3. 配置解析与安全
# ===========================================================================

@pytest.mark.offline
def test_config_defaults_to_sqlite(monkeypatch):
    """未配置时默认 SQLite。"""
    monkeypatch.delenv("EC_DB_BACKEND", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = DbConfig()
    assert cfg.backend == "sqlite"
    assert isinstance(cfg.dialect, SQLiteDialect)


@pytest.mark.offline
def test_config_explicit_postgres(monkeypatch):
    monkeypatch.setenv("EC_DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/db")
    cfg = DbConfig()
    assert cfg.backend == "postgres"
    assert isinstance(cfg.dialect, PostgresDialect)


@pytest.mark.offline
def test_config_infers_postgres_from_url(monkeypatch):
    """
    ★ 只配 DATABASE_URL 时应自动推断为 postgres。

    这是部署时最容易漏配的地方 —— 平台通常只注入 DATABASE_URL，
    不会再额外设一个 EC_DB_BACKEND。自动推断能避免
    "配了连接串却还在连 SQLite"这种静默错配。
    """
    monkeypatch.delenv("EC_DB_BACKEND", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db")
    assert DbConfig().backend == "postgres"

    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h:5432/db")
    assert DbConfig().backend == "postgres"


@pytest.mark.offline
def test_config_rejects_unknown_backend(monkeypatch):
    monkeypatch.setenv("EC_DB_BACKEND", "mysql")
    with pytest.raises(ValueError, match="不支持的数据库后端"):
        DbConfig()


@pytest.mark.offline
def test_password_masked_in_description(monkeypatch):
    """
    ★ 连接串必须脱敏。

    日志、健康检查、审计都可能打印配置 ——
    一旦把带密码的连接串写进去就是凭据泄露。
    """
    monkeypatch.setenv("EC_DB_BACKEND", "postgres")
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql://ecmonica:SuperSecret123@db:5432/ec")
    desc = DbConfig().describe()
    assert "SuperSecret123" not in desc, f"密码泄露：{desc}"
    assert "***" in desc
    assert "ecmonica" in desc


@pytest.mark.offline
def test_mask_url_handles_edge_cases():
    assert _mask_url("") == "(未配置)"
    assert _mask_url("postgresql://u@h/db") == "postgresql://u@h/db"
    masked = _mask_url("postgresql://user:pw@host:5432/db")
    assert "pw" not in masked.replace("user", "")


@pytest.mark.offline
def test_pg_missing_database_url_raises(monkeypatch):
    """选了 PG 后端但没配连接串 → 明确报错，不静默回退到 SQLite。"""
    monkeypatch.setenv("EC_DB_BACKEND", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        open_connection(DbConfig())


@pytest.mark.offline
def test_missing_sqlite_file_gives_clear_error(tmp_path):
    """SQLite 文件不存在 → 提示跑 seed 脚本，而不是抛裸异常。"""
    with pytest.raises(RuntimeError, match="seed_data"):
        open_connection(DbConfig(backend="sqlite",
                                 db_path=str(tmp_path / "nope.db")),
                        readonly=True)


# ===========================================================================
# 4. SQLite 后端实际行为（真实数据库）
# ===========================================================================

@pytest.mark.offline
def test_sqlite_readonly_blocks_write():
    """
    ★ 只读连接必须阻止写入（驱动层强制）。

    这是 SQL 护栏体系里最关键的一层 ——
    即使前四道应用层校验被绕过，这一层也不会被突破。
    """
    cfg = DbConfig()
    if not Path(cfg.db_path).exists():
        pytest.skip("数据库不存在")

    conn = SQLiteConnection(cfg.db_path, readonly=True)
    try:
        with pytest.raises(Exception):
            conn.execute("DELETE FROM dim_product")
    finally:
        conn.close()


@pytest.mark.offline
def test_sqlite_query_returns_dicts():
    cfg = DbConfig()
    if not Path(cfg.db_path).exists():
        pytest.skip("数据库不存在")
    conn = SQLiteConnection(cfg.db_path, readonly=True)
    try:
        rows = conn.query("SELECT product_id, title FROM dim_product LIMIT 2")
        assert isinstance(rows, list) and rows
        assert isinstance(rows[0], dict)
        assert "product_id" in rows[0]
    finally:
        conn.close()


@pytest.mark.offline
def test_sqlite_table_columns():
    cfg = DbConfig()
    if not Path(cfg.db_path).exists():
        pytest.skip("数据库不存在")
    conn = SQLiteConnection(cfg.db_path, readonly=True)
    try:
        cols = conn.table_columns("dim_product")
        names = [c[0] for c in cols]
        assert "product_id" in names and "title" in names
        # 维表没有日期列 —— 这是 JOIN 报错的根源
        assert "stat_date" not in names
    finally:
        conn.close()


@pytest.mark.offline
def test_readonly_query_end_to_end():
    """readonly_query 走完整链路（护栏 + 适配 + 只读连接）。"""
    from app.db.database import readonly_query

    rows = readonly_query("SELECT COUNT(*) AS n FROM dim_product")
    assert rows and rows[0]["n"] == 12


@pytest.mark.offline
def test_readonly_query_with_params():
    """参数化查询必须能正确绑定（占位符转换后仍要对应）。"""
    from app.db.database import readonly_query

    rows = readonly_query(
        "SELECT title FROM dim_product WHERE product_id = ?", ("P001",))
    assert len(rows) == 1
    assert rows[0]["title"]


@pytest.mark.offline
def test_db_health_reports_backend():
    """健康检查要暴露后端类型，且不泄露连接串。"""
    from app.db.database import db_health

    h = db_health()
    assert h["backend"] in ("sqlite", "postgres")
    assert "password" not in str(h).lower()
    if h["backend"] == "sqlite":
        assert h.get("products") == 12


# ===========================================================================
# 5. 真实 PostgreSQL（需要 DATABASE_URL）
# ===========================================================================

@pytest.mark.pg
@pytest.mark.skipif(not os.getenv("DATABASE_URL", "").startswith(
    ("postgres://", "postgresql://")),
    reason="未配置 DATABASE_URL，跳过真实 PostgreSQL 测试")
@pytest.mark.offline
def test_postgres_readonly_enforced():
    """
    ★ PostgreSQL 只读必须由**服务端**强制。

    实现方式是会话变量 default_transaction_read_only = on。
    如果只靠应用层校验，一旦某条路径没走校验就会产生写操作。
    """
    url = os.environ["DATABASE_URL"]
    conn = PostgresConnection(url, readonly=True)
    try:
        with pytest.raises(Exception) as ei:
            conn.execute("CREATE TABLE _ec_should_fail (id INT)")
        msg = str(ei.value).lower()
        assert "read-only" in msg or "read only" in msg, \
            f"应报只读错误，实际：{ei.value}"
    finally:
        conn.close()


@pytest.mark.pg
@pytest.mark.skipif(not os.getenv("DATABASE_URL", "").startswith(
    ("postgres://", "postgresql://")),
    reason="未配置 DATABASE_URL，跳过真实 PostgreSQL 测试")
@pytest.mark.offline
def test_postgres_matches_sqlite_data():
    """
    ★ 迁移一致性：PG 与 SQLite 的关键指标必须一致。

    重点校验金额汇总 —— 如果 PG 端用了 REAL（4 字节）而非
    DOUBLE PRECISION，行数会对但金额会差一点。
    """
    from app.db.database import readonly_query

    lite = SQLiteConnection(DbConfig(backend="sqlite").db_path, readonly=True)
    pg = PostgresConnection(os.environ["DATABASE_URL"], readonly=True)
    try:
        for expr, label in [("SUM(gmv)", "GMV"), ("SUM(unit_price)", "单价合计"),
                            ("COUNT(*)", "行数")]:
            v1 = lite.query(f"SELECT {expr} AS v FROM fact_daily_product")[0]["v"]
            v2 = pg.query(f"SELECT {expr} AS v FROM fact_daily_product")[0]["v"]
            if isinstance(v1, float):
                rel = abs(v1 - v2) / abs(v1) if v1 else abs(v1 - v2)
                assert rel < 1e-9, f"{label} 不一致：{v1} vs {v2}（相对误差 {rel}）"
            else:
                assert v1 == v2, f"{label} 不一致：{v1} vs {v2}"
    finally:
        lite.close()
        pg.close()


@pytest.mark.pg
@pytest.mark.skipif(not os.getenv("DATABASE_URL", "").startswith(
    ("postgres://", "postgresql://")),
    reason="未配置 DATABASE_URL，跳过真实 PostgreSQL 测试")
@pytest.mark.offline
def test_postgres_metric_layer_works():
    """
    切换到 PG 后端后，指标层应产出完全相同的数字。

    这是"换库不改业务代码"的最终验证 ——
    如果这条通过，说明适配层成功吸收了所有方言差异。
    """
    import importlib

    # 临时切到 PG
    old = os.environ.get("EC_DB_BACKEND")
    os.environ["EC_DB_BACKEND"] = "postgres"
    try:
        import app.db.database as db_mod
        importlib.reload(db_mod)
        from app.metrics.compute import latest_complete_week, period_compare

        start, end = latest_complete_week()
        pg_result = period_compare(start, end, exclude_special=True)["current"]
    finally:
        if old is None:
            os.environ.pop("EC_DB_BACKEND", None)
        else:
            os.environ["EC_DB_BACKEND"] = old
        import app.db.database as db_mod2
        importlib.reload(db_mod2)

    # 与 SQLite 结果对比
    from app.metrics.compute import latest_complete_week as lcw
    from app.metrics.compute import period_compare as pc

    start, end = lcw()
    lite_result = pc(start, end, exclude_special=True)["current"]

    for key in ("gmv_gross", "paid_orders", "visitors", "pay_cvr", "aov"):
        v1, v2 = lite_result[key], pg_result[key]
        if isinstance(v1, float):
            rel = abs(v1 - v2) / abs(v1) if v1 else abs(v1 - v2)
            assert rel < 1e-9, f"{key} 不一致：SQLite={v1} PG={v2}"
        else:
            assert v1 == v2, f"{key} 不一致：SQLite={v1} PG={v2}"
