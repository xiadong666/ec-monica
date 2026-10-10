"""
app/tools/__init__.py
=====================
工具层统一出口。

架构：registry（注册中心 + 容错执行）+ builtin（内置工具注册）

注意：导入 builtin 会触发所有工具注册 —— 这一步是必需的，
     否则 registry 里是空的。
"""
from app.tools import builtin                      # noqa: F401  触发注册
from app.tools.registry import (
    RegisteredTool, ToolError, ToolRegistry, classify_error, registry,
)

__all__ = ["registry", "ToolRegistry", "RegisteredTool", "ToolError",
           "classify_error"]
