"""
scripts/migrate_to_postgres.py
==============================
把 SQLite 里的模拟数据迁移到 PostgreSQL。

用法：
    # 1) 确认目标库可连接（先建库与用户）
    #    createdb ecmonica
    #    createuser ecmonica --pwprompt

    # 2) 设置连接串
    set DATABASE_URL=postgresql://ecmonica:pwd@localhost:5432/ecmonica

    # 3) 干跑（只检查，不写入）
    python scripts/migrate_to_postgres.py --dry-run

    # 4) 正式迁移
    python scripts/migrate_to_postgres.py

    # 5) 校验数据一致性
    python scripts/migrate_to_postgres.py --verify-only

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
迁移设计要点
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
① **幂等**：脚本可重复运行。默认先 TRUNCATE 再写入，
   避免重复执行导致数据翻倍（这是数据迁移最常见的翻车方式）。

② **分批提交**：500 行一批。一次性插 1080 行问题不大，
   但真实数据量可能上百万行 —— 不分批会撑爆内存与事务日志。

③ **事务语义**：整表一个事务可能过大；本脚本按表提交，
   失败时能明确知道哪张表没成功，且不需要整库重跑。

④ **一致性校验**：迁移后逐表比对行数 + 关键指标（GMV 汇总）。
   只比对行数是不够的 —— 行数对但金额精度丢了（REAL vs DOUBLE PRECISION）
   同样会导致下游指标全错。

⑤ **类型差异**：DDL 转换由 schema.ddl_for() 负责（REAL → DOUBLE PRECISION）。
   金额字段如果用 PG 的 REAL（4 字节）会丢精度，
   而这只会在"求和后差几分钱"时暴露，极难发现。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.adapter import (                                   # noqa: E402
    PostgresConnection, SQLiteConnection, _mask_url,
)
from app.db.schema import ALLOWED_TABLES, all_ddl_for          # noqa: E402

# 迁移表的写入顺序：维表在前（虽然本库没有外键强约束，但保持合理顺序）
MIGRATION_ORDER = [
    "dim_product",
    "fact_daily_product",
    "fact_daily_traffic",
    "fact_daily_inventory",
    "fact_daily_competitor",
]

BATCH_SIZE = 500


def get_columns(conn, table: str) -> list[str]:
    """取表的列名（按定义顺序）。"""
    return [name for name, _ in conn.table_columns(table)]


def create_schema(pg: PostgresConnection) -> None:
    """在 PostgreSQL 中建表（幂等）。"""
    print("\n[1/4] 建表")
    ddl_list = all_ddl_for("postgresql")
    for ddl in ddl_list:
        # 从 DDL 里取表名/索引名用于日志
        head = " ".join(ddl.split()[:5])
        pg.execute(ddl)
        print(f"      OK  {head}...")
    pg.commit()
    print(f"      完成，共执行 {len(ddl_list)} 条 DDL")


def truncate_tables(pg: PostgresConnection) -> None:
    """
    清空目标表。

    ★ 为什么默认清空？
      迁移脚本必须幂等。如果不先清空，第二次运行会主键冲突，
      或者（如果没有主键约束）静默产生重复数据 ——
      后者更危险，因为行数看起来"变多了"而不是报错。
    """
    print("\n[2/4] 清空目标表（保证幂等）")
    for table in reversed(MIGRATION_ORDER):
        pg.execute(f"TRUNCATE TABLE {table}")
        print(f"      TRUNCATE {table}")
    pg.commit()


def migrate_table(lite: SQLiteConnection, pg: PostgresConnection,
                  table: str) -> int:
    """
    迁移单张表。

    返回迁移行数。

    ★ 分批提交的理由：
      一次性插入会让整个表处于一个事务里。数据量大时，
      事务日志（WAL）会暴涨，失败则全部回滚、只能重来。
      分批后失败只丢最后一批，重跑成本低。
    """
    src_cols = get_columns(lite, table)
    dst_cols = get_columns(pg, table)

    # 列必须完全一致 —— 这是迁移前的第一道校验。
    # 如果两边 schema 不一致（例如 DDL 转换出错），宁可直接失败也不要写入错位的数据。
    if src_cols != dst_cols:
        raise SystemExit(
            f"列不一致，拒绝迁移 {table}：\n"
            f"  SQLite     : {src_cols}\n"
            f"  PostgreSQL : {dst_cols}\n"
            f"请检查 schema.py 的 DDL 转换（ddl_for）。"
        )

    col_list = ", ".join(src_cols)
    placeholders = ", ".join("?" for _ in src_cols)     # 由适配层转成 %s
    insert_sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"

    rows = lite.query(f"SELECT {col_list} FROM {table}")
    if not rows:
        print(f"      {table:24} 0 行（源表为空）")
        return 0

    values = [tuple(r[c] for c in src_cols) for r in rows]

    total = 0
    for i in range(0, len(values), BATCH_SIZE):
        batch = values[i: i + BATCH_SIZE]
        pg.executemany(insert_sql, batch)
        pg.commit()
        total += len(batch)
        print(f"      {table:24} {total:>6}/{len(values)} 行", end="\r")

    print(f"      {table:24} {total:>6} 行  完成")
    return total


def migrate_all(lite: SQLiteConnection, pg: PostgresConnection) -> dict:
    """迁移全部表。"""
    print("\n[3/4] 迁移数据")
    counts = {}
    for table in MIGRATION_ORDER:
        t0 = time.perf_counter()
        counts[table] = migrate_table(lite, pg, table)
        elapsed = (time.perf_counter() - t0) * 1000
        if counts[table]:
            print(f"      {'':24} 耗时 {elapsed:.0f} ms")
    return counts


def verify(lite: SQLiteConnection, pg: PostgresConnection) -> bool:
    """
    一致性校验。

    ★ 三层校验，缺一不可：
      ① 行数一致
      ② 列结构一致
      ③ **关键金额汇总一致** —— 这一层专门用来抓类型精度问题。
         如果 PG 端用了 REAL（4 字节）而不是 DOUBLE PRECISION，
         行数会对，但 SUM(gmv) 会差一点点。
    """
    print("\n[4/4] 一致性校验")
    ok = True

    # ① 行数
    print("      行数比对：")
    for table in MIGRATION_ORDER:
        n_lite = lite.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        n_pg = pg.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        match = n_lite == n_pg
        ok = ok and match
        flag = "OK " if match else "差异"
        print(f"        [{flag}] {table:24} SQLite={n_lite:>6}  "
              f"PostgreSQL={n_pg:>6}")

    # ② 列结构
    print("      列结构比对：")
    for table in MIGRATION_ORDER:
        c1 = get_columns(lite, table)
        c2 = get_columns(pg, table)
        match = c1 == c2
        ok = ok and match
        print(f"        [{'OK ' if match else '差异'}] {table:24} "
              f"{len(c1)} 列" + ("" if match else f"  {c1} vs {c2}"))

    # ③ 金额汇总（抓精度问题）
    print("      金额汇总比对（抓浮点精度问题）：")
    checks = [
        ("fact_daily_product", "SUM(gmv)", "GMV 总额"),
        ("fact_daily_product", "SUM(refund_amount)", "退款总额"),
        ("fact_daily_product", "SUM(unit_price)", "单价合计"),
        ("fact_daily_traffic", "SUM(ad_spend)", "投放花费"),
        ("dim_product", "SUM(price)", "标价合计"),
    ]
    for table, expr, label in checks:
        v1 = lite.query(f"SELECT {expr} AS v FROM {table}")[0]["v"] or 0
        v2 = pg.query(f"SELECT {expr} AS v FROM {table}")[0]["v"] or 0
        # 相对误差 < 1e-9 视为一致（浮点求和顺序不同会有极小差异）
        rel = abs(v1 - v2) / abs(v1) if v1 else abs(v1 - v2)
        match = rel < 1e-9
        ok = ok and match
        print(f"        [{'OK ' if match else '差异'}] {label:12} "
              f"SQLite={v1:>16,.4f}  PostgreSQL={v2:>16,.4f}  "
              f"相对误差={rel:.2e}")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移")
    parser.add_argument("--sqlite", default=None,
                        help="源 SQLite 路径（默认取 EC_DB_PATH）")
    parser.add_argument("--database-url", default=None,
                        help="目标 PostgreSQL 连接串（默认取 DATABASE_URL）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只检查连接与结构，不写入任何数据")
    parser.add_argument("--verify-only", action="store_true",
                        help="只做一致性校验，不迁移")
    parser.add_argument("--no-truncate", action="store_true",
                        help="不清空目标表（谨慎使用：可能导致主键冲突或重复数据）")
    args = parser.parse_args()

    import os

    sqlite_path = args.sqlite or os.getenv("EC_DB_PATH", "data/ec.db")
    database_url = args.database_url or os.getenv("DATABASE_URL", "")

    print("=" * 72)
    print("SQLite → PostgreSQL 数据迁移")
    print("=" * 72)
    print(f"  源      : sqlite://{sqlite_path}")
    print(f"  目标    : postgresql://{_mask_url(database_url)}")
    if args.dry_run:
        print("  模式    : 干跑（不写入）")
    elif args.verify_only:
        print("  模式    : 仅校验")

    if not database_url:
        print("\n[错误] 未配置 DATABASE_URL。")
        print("       示例：set DATABASE_URL=postgresql://user:pwd@localhost:5432/ecmonica")
        return 2

    if not Path(sqlite_path).exists():
        print(f"\n[错误] 源数据库不存在：{sqlite_path}")
        print("       请先运行 python scripts/seed_data.py")
        return 2

    # 连接（只读打开源库，防止误写）
    try:
        lite = SQLiteConnection(sqlite_path, readonly=True)
    except Exception as exc:                            # noqa: BLE001
        print(f"\n[错误] 打开源数据库失败：{exc}")
        return 2

    try:
        pg = PostgresConnection(database_url, readonly=False)
    except Exception as exc:                            # noqa: BLE001
        print(f"\n[错误] 连接 PostgreSQL 失败：{type(exc).__name__}: {exc}")
        print("\n排查建议：")
        print("  1. PostgreSQL 是否已启动？")
        print("  2. 数据库与用户是否已创建？（createdb / createuser）")
        print("  3. 连接串中的主机/端口/库名/密码是否正确？")
        print("  4. 是否已安装驱动：pip install 'psycopg[binary]'")
        return 2

    try:
        if args.verify_only:
            good = verify(lite, pg)
            print()
            print("=" * 72)
            print("校验通过 ✅" if good else "校验发现差异 ❌")
            return 0 if good else 1

        create_schema(pg)

        if args.dry_run:
            print("\n[干跑] 检查源表结构与行数")
            for table in MIGRATION_ORDER:
                cols = get_columns(lite, table)
                n = lite.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
                print(f"      {table:24} {len(cols)} 列, {n} 行")
            print("\n[干跑结束] 结构与连接均正常，可以正式迁移。")
            return 0

        if not args.no_truncate:
            truncate_tables(pg)
        else:
            print("\n[警告] 跳过清空目标表 —— 若目标已有数据，可能出现"
                  "主键冲突或重复数据。")

        counts = migrate_all(lite, pg)
        total = sum(counts.values())

        good = verify(lite, pg)

        print()
        print("=" * 72)
        print(f"迁移完成：共 {total} 行")
        for table, n in counts.items():
            print(f"    {table:24} {n:>6} 行")
        print("=" * 72)
        if good:
            print("数据校验通过 ✅  可以切换后端：EC_DB_BACKEND=postgres")
        else:
            print("⚠️  数据校验发现差异 —— 请勿切换后端，先排查上面的差异项。")
        return 0 if good else 1

    finally:
        lite.close()
        pg.close()


if __name__ == "__main__":
    sys.exit(main())
