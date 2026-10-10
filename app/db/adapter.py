"""
app/db/adapter.py
=================
★ 数据库适配层：SQLite / PostgreSQL 双后端。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么需要适配层，而不是直接用 SQLAlchemy？
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    本项目只用到了两种数据库操作的很小一个子集：
        · 只读查询（SELECT）
        · 建表（init）
        · 批量插入（迁移用）

    引入 SQLAlchemy 会带来：
        · 一个重量级依赖（含 ORM、连接池、会话管理等我们不需要的东西）
        · 查询语义的额外抽象层（排查"这条 SQL 到底发出去的是什么"变难）
        · 与"指标层刻意只用标准库以保证可复现"的设计取向冲突

    所以这里用**薄适配层**，只封装真正存在差异的三处：
        ① 占位符      SQLite 是 ?    PostgreSQL 是 %s
        ② 只读强制    SQLite 是 mode=ro 文件模式
                      PostgreSQL 是 SET default_transaction_read_only
        ③ 错误分类    两者的异常类型与错误码不同

    ★ 判断标准：当抽象能带来的收益小于它引入的复杂度时，不要抽象。
      但**方言差异必须被封装**，否则它会长进业务代码里 ——
      那才是真正的技术债。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os
import re
from abc import ABC, abstractmethod
from typing import Any


# ---------------------------------------------------------------------------
# 方言配置
# ---------------------------------------------------------------------------

class Dialect(ABC):
    """SQL 方言差异的封装。"""

    name: str = "base"

    @abstractmethod
    def placeholder(self, index: int) -> str:
        """第 index 个位置参数（从 0 开始）的占位符。"""

    @abstractmethod
    def adapt_sql(self, sql: str) -> str:
        """把统一的 `?` 占位符 SQL 转成本方言的形式。"""

    def count_placeholders(self, sql: str) -> int:
        """统计 `?` 占位符个数（用于校验参数数量）。"""
        return _strip_literals(sql).count("?")


class SQLiteDialect(Dialect):
    name = "sqlite"

    def placeholder(self, index: int) -> str:
        return "?"

    def adapt_sql(self, sql: str) -> str:
        # SQLite 原生就是 ?，无需转换
        return sql


class PostgresDialect(Dialect):
    name = "postgresql"

    def placeholder(self, index: int) -> str:
        return "%s"

    def adapt_sql(self, sql: str) -> str:
        """
        把 `?` 占位符转成 `%s`。

        ★ 难点：不能无脑 replace("?", "%s") —— 
          字符串字面量里的问号（如 LIKE '%?%'）会被误替换。
          所以先屏蔽掉字符串字面量与注释，只替换代码区的问号。
          这与 validate_sql 里"先去掉注释再检测关键字"是同一类问题：
          **对 SQL 做文本处理时，必须先区分"代码"与"数据"。**
        """
        return _replace_outside_literals(sql, "?", "%s")


def _strip_literals(sql: str) -> str:
    """
    把字符串字面量和注释替换成**等长**空白，只保留 SQL 代码结构。

    ★ 关键约束：输出长度必须与输入**完全一致**。
      调用方（_replace_outside_literals）按下标逐字符比对，
      长度一旦不一致就会 IndexError 或错位替换。

      实测踩过的 bug：行注释循环把结尾的换行符也吞掉了，
      然后又在换行处补了一个空格 —— 掩码比原文多一个字符，
      在含中文的 SQL 上直接 IndexError。
      正确做法：注释体替换成空格，但**换行符本身留给主循环处理**，
      因为它在原文里是一个真实字符。
    """
    out = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # 单引号字符串
        if ch == "'":
            out.append(" ")
            i += 1
            while i < n:
                if sql[i] == "'" and (i + 1 >= n or sql[i + 1] != "'"):
                    i += 1
                    break
                if sql[i] == "'" and i + 1 < n and sql[i + 1] == "'":
                    out.append("  ")            # 转义的单引号占两个字符
                    i += 2
                    continue
                out.append(" ")
                i += 1
            out.append(" ")
            continue
        # 双引号标识符
        if ch == '"':
            out.append(" ")
            i += 1
            while i < n and sql[i] != '"':
                out.append(" ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        # 行注释：注释体替换成空格，但保留结尾换行符本身
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                out.append(" ")
                i += 1
            continue                            # 换行交给主循环追加
        # 块注释
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            out.append("  ")
            i += 2
            while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                out.append(" ")
                i += 1
            if i + 1 < n:
                out.append("  ")
                i += 2
            continue
        out.append(ch)
        i += 1

    masked = "".join(out)
    # 防御性断言：长度必须一致，否则调用方的下标比对会错位。
    # 用一个显式的检查代替"祈祷它是对的"。
    if len(masked) != n:
        raise ValueError(
            f"SQL 掩码长度不一致（{len(masked)} != {n}）—— "
            f"这是适配层的内部错误，请报告。"
        )
    return masked


def _replace_outside_literals(sql: str, old: str, new: str) -> str:
    """只在字符串字面量与注释之外做替换。"""
    masked = _strip_literals(sql)
    result = []
    for i, ch in enumerate(sql):
        if masked[i] == old and ch == old:
            result.append(new)
        else:
            result.append(ch)
    return "".join(result)


# ---------------------------------------------------------------------------
# 连接配置
# ---------------------------------------------------------------------------

class DbConfig:
    """
    数据库配置。

    从环境变量读取，决定使用哪个后端：

        EC_DB_BACKEND = sqlite | postgres    （默认 sqlite）
        EC_DB_PATH    = data/ec.db           （sqlite 用）
        DATABASE_URL  = postgresql://user:pwd@host:5432/dbname   （postgres 用）

    ★ 为什么用 DATABASE_URL 这个约定俗成的名字？
      它是云平台（Heroku/Render/Railway）与容器编排的通用约定，
      用它能让部署配置零改动。而 EC_ 前缀的变量用于项目自有配置。
    """

    def __init__(self, backend: str | None = None, db_path: str | None = None,
                 database_url: str | None = None):
        self.backend = (backend or os.getenv("EC_DB_BACKEND", "sqlite")).lower()
        self.db_path = db_path or os.getenv("EC_DB_PATH", "data/ec.db")
        self.database_url = database_url or os.getenv("DATABASE_URL", "")

        # 兼容：如果只配了 DATABASE_URL 而没配 EC_DB_BACKEND，
        # 自动推断为 postgres —— 这是部署时最容易漏配的地方
        if self.database_url.startswith(("postgres://", "postgresql://")) \
                and not os.getenv("EC_DB_BACKEND"):
            self.backend = "postgres"

        if self.backend not in ("sqlite", "postgres"):
            raise ValueError(
                f"不支持的数据库后端：{self.backend}。"
                f"可选：sqlite / postgres"
            )

    @property
    def dialect(self) -> Dialect:
        return PostgresDialect() if self.backend == "postgres" else SQLiteDialect()

    def describe(self) -> str:
        """安全的配置描述（**不含密码**）。"""
        if self.backend == "postgres":
            return f"postgresql://{_mask_url(self.database_url)}"
        return f"sqlite://{self.db_path}"


def _mask_url(url: str) -> str:
    """
    隐藏连接串里的密码。

    ★ 这很重要：日志、健康检查、审计里都可能打印配置，
      一旦把带密码的连接串写进去就是凭据泄露。
      **任何对外输出的配置描述都必须先脱敏。**
    """
    if not url:
        return "(未配置)"
    return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:***@", url)


# ---------------------------------------------------------------------------
# 连接与游标抽象
# ---------------------------------------------------------------------------

class Connection(ABC):
    """数据库连接的统一接口。"""

    dialect: Dialect
    backend: str

    @abstractmethod
    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行查询，返回 list[dict]。"""

    @abstractmethod
    def execute(self, sql: str, params: tuple = ()) -> None:
        """执行写操作（仅供初始化/迁移使用）。"""

    @abstractmethod
    def executemany(self, sql: str, rows: list[tuple]) -> int:
        """批量写入，返回影响行数。"""

    @abstractmethod
    def commit(self) -> None: ...

    @abstractmethod
    def rollback(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def table_columns(self, table: str) -> list[tuple[str, str]]:
        """返回 [(列名, 类型)]。两种数据库的查询方式不同。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SQLiteConnection(Connection):
    """SQLite 连接（标准库 sqlite3）。"""

    backend = "sqlite"

    def __init__(self, path: str, readonly: bool = False):
        import sqlite3

        self.dialect = SQLiteDialect()
        self.readonly = readonly
        if readonly:
            from pathlib import Path
            # ★ 只读的关键实现：用文件 URI 的 mode=ro。
            #   这是**驱动层**的限制，比任何应用层校验都可靠 ——
            #   即使 SQL 校验被绕过，写入也会被数据库拒绝。
            uri = f"file:{Path(path).as_posix()}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True)
        else:
            self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        cur = self._conn.cursor()
        cur.execute(self.dialect.adapt_sql(sql), params)
        return [dict(r) for r in cur.fetchall()]

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.execute(self.dialect.adapt_sql(sql), params)

    def executemany(self, sql: str, rows: list[tuple]) -> int:
        cur = self._conn.executemany(self.dialect.adapt_sql(sql), rows)
        return cur.rowcount if cur.rowcount is not None else len(rows)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def table_columns(self, table: str) -> list[tuple[str, str]]:
        rows = self.query(f"PRAGMA table_info({table})")
        return [(r["name"], r["type"]) for r in rows]

    @property
    def raw(self):
        return self._conn


class PostgresConnection(Connection):
    """PostgreSQL 连接（psycopg 3）。"""

    backend = "postgres"

    def __init__(self, url: str, readonly: bool = False):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError(
                "使用 PostgreSQL 需要安装驱动：pip install 'psycopg[binary]'。"
                "若只想本地跑，请把 EC_DB_BACKEND 设为 sqlite（默认）。"
            ) from exc

        self.dialect = PostgresDialect()
        self.readonly = readonly
        self._psycopg = psycopg

        # ★ 只读的关键实现：PostgreSQL 没有"以只读模式打开库"的连接参数，
        #   但有 `default_transaction_read_only` 会话变量 ——
        #   设置后当前会话内的任何写操作都会被服务端拒绝。
        #   效果等价于 SQLite 的 mode=ro：**在服务端强制，不靠应用层自觉。**
        #
        #   autocommit=True 是必要的：否则设置会落在事务里，
        #   事务结束即失效（连接池复用时会"只读失效"）。
        self._conn = psycopg.connect(url, autocommit=True)
        if readonly:
            with self._conn.cursor() as cur:
                cur.execute("SET default_transaction_read_only = on")

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        import psycopg.rows

        with self._conn.cursor(row_factory=psycopg.rows.dict_row) as cur:
            cur.execute(self.dialect.adapt_sql(sql), params)
            return list(cur.fetchall())

    def execute(self, sql: str, params: tuple = ()) -> None:
        with self._conn.cursor() as cur:
            cur.execute(self.dialect.adapt_sql(sql), params)

    def executemany(self, sql: str, rows: list[tuple]) -> int:
        """
        批量写入。

        ★ PostgreSQL 的占位符是 %s，executemany 逐行绑定即可。
          为了迁移性能，调用方应分批提交（见 migrate 脚本）。
        """
        if not rows:
            return 0
        with self._conn.cursor() as cur:
            cur.executemany(self.dialect.adapt_sql(sql), rows)
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)

    def commit(self) -> None:
        if not self._conn.autocommit:
            self._conn.commit()

    def rollback(self) -> None:
        if not self._conn.autocommit:
            self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    def table_columns(self, table: str) -> list[tuple[str, str]]:
        """
        从 information_schema 读列信息。

        ★ PostgreSQL 没有 PRAGMA。这是双后端最典型的差异点 ——
          正是这类差异需要被适配层吸收，而不是泄漏到业务代码里。
        """
        rows = self.query(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = ?
            ORDER BY ordinal_position
            """,
            (table,),
        )
        return [(r["column_name"], r["data_type"]) for r in rows]

    @property
    def raw(self):
        return self._conn


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

def open_connection(config: DbConfig | None = None,
                    readonly: bool = False,
                    for_setup: bool = False) -> Connection:
    """
    按配置打开连接。

    参数：
        config    : 数据库配置，默认从环境变量读取
        readonly  : 是否只读（Agent 与指标层必须为 True）
        for_setup : 初始化/迁移场景。PostgreSQL 首次建库时需要更高权限，
                    且此时**不会**开启只读。
    """
    config = config or DbConfig()

    if config.backend == "postgres":
        if not config.database_url:
            raise RuntimeError(
                "已选择 PostgreSQL 后端但未配置 DATABASE_URL。\n"
                "示例：DATABASE_URL=postgresql://ecmonica:pwd@localhost:5432/ecmonica"
            )
        return PostgresConnection(config.database_url, readonly=readonly)

    # SQLite
    from pathlib import Path

    if readonly and not Path(config.db_path).exists():
        raise RuntimeError(
            f"数据库不存在：{config.db_path}。"
            f"请先运行 python scripts/seed_data.py 生成模拟数据，"
            f"或用 scripts/migrate_to_postgres.py 迁移。"
        )
    return SQLiteConnection(config.db_path, readonly=readonly)


def backend_name() -> str:
    """当前后端名（用于日志与健康检查）。"""
    try:
        return DbConfig().backend
    except ValueError:
        return "unknown"
