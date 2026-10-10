"""
app/tools/sql_tool.py
=====================
只读 SQL 工具（已迁移到 Registry 架构）。

真正的定义在 app/tools/builtin.py。
四道护栏实现在 app/db/database.py：表白名单 / 强制 SELECT / 强制 LIMIT / 只读连接。
"""
from app.tools import builtin                        # noqa: F401  触发注册
from app.tools.registry import registry


def get_sql_tools():
    """返回 SQL 类工具（LangChain 形态）。"""
    return registry.build_langchain_tools(
        registry.select(capabilities=["schema", "sql"])
    )
