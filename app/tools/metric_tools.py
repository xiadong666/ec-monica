"""
app/tools/metric_tools.py
=========================
指标工具（已迁移到 Registry 架构）。

保留本模块是为了兼容与可读性：
工具的真正定义在 app/tools/builtin.py（用 @registry.register 注册），
这里只提供按名称取回 LangChain 工具的便捷函数。

★ 架构变化说明：
    旧版：工具是模块级常量列表（METRIC_TOOLS = [...]），
          固定不变，意图路由时无法按需装配。
    新版：工具注册在 Registry 中并带能力标签，
          可由 `registry.select(capabilities=[...])` 动态装配。
          新增工具只需写一个函数 + 一个装饰器。
"""
from app.tools import builtin                        # noqa: F401  触发注册
from app.tools.registry import registry


def get_metric_tools():
    """返回指标类工具（LangChain 形态，含容错包装）。"""
    return registry.build_langchain_tools(
        registry.select(capabilities=["schema", "metric", "drilldown", "quality"])
    )


def get_all_tools():
    """返回全部已注册工具（LangChain 形态）。"""
    return registry.build_langchain_tools()


def call_tool(name: str, **kwargs):
    """
    直接调用已注册工具（含超时重试与错误分类）。

    这是给测试与内部逻辑用的入口 —— 
    与 Agent 调用走的是**同一条链路**，所以测试结果能真实反映线上行为。
    """
    return registry.execute(name, **kwargs)
