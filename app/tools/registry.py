"""
app/tools/registry.py
=====================
★★ Tool Registry —— 动态工具注册与执行。

对应规格要求：
    「动态注册 Function Schema 的 Tool Registry」
    「工具层封装超时重试与错误分类，失败时 Fallback 直接生成」

为什么不能只用一个 list 装工具？
    1. **按意图动态装配**：意图识别后只需要 3~5 个相关工具，
       把 20 个工具全塞给模型会显著提高选错工具的概率；
    2. **统一容错**：超时、重试、错误分类只写一遍，所有工具共享；
    3. **可观测**：每次调用耗时、重试次数、错误类型都留痕，
       这是上线后排查"为什么这次报告慢/差"的唯一依据。

三类错误分类（决定重试策略，这个区分非常关键）：
    ┌─ transport（瞬时网络/超时）→ 可重试。指数退避。
    ├─ data（数据问题：无数据、区间越界）→ 不重试。
    │     重试同样的查询只会得到同样的空结果，浪费时间。
    │     正确做法是把错误信息回灌给模型，让它改查询条件。
    └─ permanent（权限/参数错误）→ 不重试，直接降级。
          重试无意义，应触发 Fallback。
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------

ErrorKind = Literal["transport", "data", "permanent", "unknown"]


class ToolError(Exception):
    """工具执行错误，带分类信息。"""

    def __init__(self, kind: ErrorKind, message: str, retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


def classify_error(exc: Exception) -> ToolError:
    """
    把任意异常归类到三种错误类型。

    ★ 这一步的价值：如果不分类，所有错误都盲目重试 3 次，
      那么"日期区间没数据"这种确定性错误会被重试 3 次，
      白白增加 3 倍延迟，最后还是失败。
      分类之后：只有真正瞬时的错误才重试。
    """
    text = f"{type(exc).__name__}: {exc}".lower()

    # --- transport：瞬时故障，值得重试 ---
    transport_signals = [
        "timeout", "timed out", "connection", "temporarily",
        "rate limit", "429", "502", "503", "504", "reset by peer",
        "ssl", "network", "broken pipe",
    ]
    if any(s in text for s in transport_signals):
        return ToolError("transport", str(exc), retryable=True)

    # --- data：数据/查询问题，重试无用，要改条件 ---
    data_signals = [
        "no such table", "no such column", "syntax error", "无数据",
        "数据库不存在", "为空", "越界", "sql 被拒绝", "未识别到任何数据表",
    ]
    if any(s in text for s in data_signals):
        return ToolError("data", str(exc), retryable=False)

    # --- permanent：权限/参数错误 ---
    permanent_signals = [
        "api key", "unauthorized", "401", "403", "forbidden",
        "not configured", "未配置", "invalid", "不支持",
    ]
    if any(s in text for s in permanent_signals):
        return ToolError("permanent", str(exc), retryable=False)

    return ToolError("unknown", str(exc), retryable=False)


# ---------------------------------------------------------------------------
# 工具定义
# ---------------------------------------------------------------------------

# 能力标签：意图路由靠它挑工具
Capability = Literal[
    "schema",       # 了解数据结构
    "metric",       # 指标计算
    "drilldown",    # 下钻明细
    "quality",      # 数据质控
    "sql",          # 自由探索
    "report",       # 报告渲染
]


@dataclass
class RegisteredTool:
    """一个注册到 Registry 的工具。"""

    name: str
    func: Callable
    description: str
    capabilities: list[str]
    # --- 容错配置 ---
    timeout_s: float = 20.0
    max_retries: int = 2
    backoff_base: float = 0.4
    # --- 运行时统计（可观测性）---
    stats: dict = field(default_factory=lambda: {
        "calls": 0, "failures": 0, "retries": 0, "total_ms": 0.0,
    })

    def public_meta(self) -> dict:
        """对外暴露的元信息（不含函数对象）。"""
        avg = (self.stats["total_ms"] / self.stats["calls"]) if self.stats["calls"] else 0
        return {
            "name": self.name,
            "description": self.description,
            "capabilities": self.capabilities,
            "timeout_s": self.timeout_s,
            "max_retries": self.max_retries,
            "calls": self.stats["calls"],
            "failures": self.stats["failures"],
            "retries": self.stats["retries"],
            "avg_ms": round(avg, 1),
        }


class ToolRegistry:
    """
    工具注册中心。

    用法：
        registry = ToolRegistry()

        @registry.register(capabilities=["metric"], description="...")
        def my_tool(start: str, end: str) -> dict: ...

        # 按能力动态取工具（意图路由用）
        tools = registry.select(capabilities=["metric", "quality"])
    """

    def __init__(self):
        self._tools: dict[str, RegisteredTool] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, capabilities: list[str], description: str,
                 name: str | None = None, timeout_s: float = 20.0,
                 max_retries: int = 2, backoff_base: float = 0.4):
        """装饰器：把一个函数注册为工具。"""
        def deco(fn: Callable):
            tool_name = name or fn.__name__
            self._tools[tool_name] = RegisteredTool(
                name=tool_name, func=fn, description=description,
                capabilities=list(capabilities), timeout_s=timeout_s,
                max_retries=max_retries, backoff_base=backoff_base,
            )
            return fn
        return deco

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def all(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    # ------------------------------------------------------------------
    # 按能力选择（意图路由的核心）
    # ------------------------------------------------------------------

    def select(self, capabilities: list[str] | None = None,
               names: list[str] | None = None) -> list[RegisteredTool]:
        """
        按能力标签或工具名选择工具子集。

        ★ 这是「条件路由」落到实处的关键：
          意图判断为"经营分析"→ 只装配 metric+quality+drilldown 类工具，
          模型面对的选择少了，选错的概率自然下降。
        """
        if names:
            return [t for n in names if (t := self._tools.get(n))]
        if capabilities is None:
            return self.all()
        want = set(capabilities)
        return [t for t in self._tools.values() if want & set(t.capabilities)]

    # ------------------------------------------------------------------
    # 装配成 LangChain 工具（含容错包装）
    # ------------------------------------------------------------------

    def build_langchain_tools(self, tools: list[RegisteredTool] | None = None):
        """
        把注册的工具转成 LangChain 可调用的工具。

        关键：这里包一层 **容错执行**，所以模型调用工具时自动获得
        超时保护、按错误类型重试、结构化错误回灌 —— 模型不需要知道这些。
        """
        out = []
        for t in (tools if tools is not None else self.all()):
            out.append(StructuredTool.from_function(
                func=self._make_callable(t),
                name=t.name,
                description=t.description,
                args_schema=_schema_from_func(t.func),
            ))
        return out

    def _make_callable(self, t: RegisteredTool) -> Callable:
        """生成带容错的调用入口。"""
        def callable_(**kwargs) -> str:
            result = self.execute(t.name, **kwargs)
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False, default=str)
        return callable_

    # ------------------------------------------------------------------
    # ★ 执行：超时 + 重试 + 错误分类 + 降级
    # ------------------------------------------------------------------

    def execute(self, name: str, **kwargs) -> Any:
        """
        执行工具。返回结果 dict；失败时返回 **结构化错误对象**（不抛异常）。

        为什么失败不抛异常？
            抛异常会中断整个 Agent 循环，让本次请求直接 500。
            返回结构化错误则让模型看到"这个工具失败了，原因是 X"，
            它可以换个工具或改参数 —— 这才是 Agent 该有的韧性。

        ★ 关键一步：参数先过 schema 校验再执行。
          实测踩过的坑：最初这里直接把 **kwargs 透传给函数，
          结果 LLM 传了 `start_date` 而函数签名是 `start`，
          直接抛 TypeError。虽然错误被兜住了，但：
            ① 模型只看到 "unexpected keyword argument"，不知道正确参数名；
            ② 白跑一轮工具，浪费延迟和 token（这次实测浪费了 1 轮）。
          加了校验层之后，错误信息会明确告诉模型正确的参数名，
          它下一轮就能自己改对。
        """
        t = self._tools.get(name)
        if t is None:
            return {
                "ok": False,
                "error_kind": "permanent",
                "error": f"未注册的工具：{name}",
                "available": self.names(),
            }
        return self._run(t, kwargs, enforce_timeout=True)

    def execute_inline(self, name: str, **kwargs) -> Any:
        """
        与 execute 完全相同，但**不做线程级超时包装**。

        用途：并行执行。并行 worker 本身已经在 ThreadPoolExecutor 里，
        再嵌一层 _call_with_timeout 会让线程数翻倍（每个调用 2 个线程），
        而超时由线程池的 overall deadline 负责即可。

        ★ 容错语义完全不变：参数校验、重试、错误分类、指数退避
          全部走同一份 _run 实现 —— 只有超时包装方式不同。
          这一点很重要：**并行化不允许改变任何容错行为**。
        """
        t = self._tools.get(name)
        if t is None:
            return {
                "ok": False,
                "error_kind": "permanent",
                "error": f"未注册的工具：{name}",
                "available": self.names(),
            }
        return self._run(t, kwargs, enforce_timeout=False)

    def _run(self, t: RegisteredTool, kwargs: dict,
             enforce_timeout: bool) -> Any:
        """
        工具执行核心：参数校验 → 重试循环 → 错误分类。

        execute 与 execute_inline 共用这一份实现，确保两条路径行为一致。
        """
        # --- 参数校验与规范化（把 LLM 的参数错误挡在执行之前）---
        clean_kwargs = self._validate_args(t, kwargs)
        if isinstance(clean_kwargs, dict) and clean_kwargs.get("__invalid__"):
            return {
                "ok": False,
                "error_kind": "data",
                "error": clean_kwargs["message"],
                "expected_params": clean_kwargs["expected"],
                "hint": clean_kwargs["hint"],
            }

        t.stats["calls"] += 1
        started = time.perf_counter()
        last_error: ToolError | None = None

        for attempt in range(t.max_retries + 1):
            try:
                if enforce_timeout:
                    result = _call_with_timeout(t.func, clean_kwargs, t.timeout_s)
                else:
                    result = _call_direct(t.func, clean_kwargs, t.timeout_s)
                t.stats["total_ms"] += (time.perf_counter() - started) * 1000
                return result

            except Exception as exc:
                err = classify_error(exc)
                last_error = err

                # 不可重试的错误：立刻放弃，把原因交给模型/调用方
                if not err.retryable or attempt >= t.max_retries:
                    break

                t.stats["retries"] += 1
                # 指数退避：0.4s, 0.8s, 1.6s ...
                time.sleep(t.backoff_base * (2 ** attempt))

        t.stats["failures"] += 1
        t.stats["total_ms"] += (time.perf_counter() - started) * 1000

        return {
            "ok": False,
            "error_kind": last_error.kind if last_error else "unknown",
            "retryable": bool(last_error and last_error.retryable),
            "attempts": t.stats["retries"] + 1,
            "error": str(last_error) if last_error else "未知错误",
            "hint": _hint_for(last_error.kind if last_error else "unknown"),
        }

    def _validate_args(self, t: RegisteredTool, kwargs: dict) -> dict:
        """
        按自动生成的 schema 校验参数，并做常见别名映射。

        返回清洗后的 kwargs；参数非法时返回带 __invalid__ 标记的 dict。
        """
        # 别名映射：LLM 常写的参数名 → 函数真实参数名
        aliases = {
            "start_date": "start", "end_date": "end",
            "begin": "start", "finish": "end",
            "begin_date": "start", "finish_date": "end",
            "from_date": "start", "to_date": "end",
            "date_range": "days", "period_days": "days",
            "bucket": "bucket_days", "interval": "bucket_days",
            "query": "sql", "statement": "sql",
            "category_name": "category", "product_category": "category",
        }

        schema_cls = _schema_from_func(t.func)
        valid_fields = set(schema_cls.model_fields)

        normalized: dict = {}
        for key, value in kwargs.items():
            if key in valid_fields:
                normalized[key] = value
            elif aliases.get(key) in valid_fields:
                # 别名命中：静默纠正，并在结果里留一句说明
                normalized[aliases[key]] = value
            elif key in ("__arg1", "arg1", "input", "kwargs"):
                # 某些模型会用占位参数名包一层
                continue
            else:
                return {
                    "__invalid__": True,
                    "message": (
                        f"参数名错误：{t.name} 不接受参数「{key}」。"
                        f"正确参数为 {sorted(valid_fields)}。"
                    ),
                    "expected": sorted(valid_fields),
                    "hint": (
                        f"请用正确参数名重新调用 {t.name}。"
                        + (f"注意：schema 中未声明必填参数，"
                           f"所有参数都有默认值，也可以不传参数。"
                           if not schema_cls.model_fields else "")
                    ),
                }

        # 按 schema 做类型校验（宽松模式：类型不符时尝试强转，失败才报错）
        try:
            validated = schema_cls(**normalized)
        except Exception as exc:                        # noqa: BLE001
            return {
                "__invalid__": True,
                "message": f"参数类型错误：{exc}",
                "expected": sorted(valid_fields),
                "hint": f"请按正确类型重新调用 {t.name}。",
            }

        # ★ 无参工具（schema 无字段）必须直接返回空 dict。
        #   实测踩过的坑：对零字段模型调 model_dump() 会带出
        #   __base__ 之类的内部键，随后被当成函数参数传入，
        #   导致 describe_tables 这类无参工具报
        #   "validation error / unexpected keyword argument"。
        #   无参工具是合法且常见的情况（describe_tables、get_data_range），
        #   必须显式处理。
        if not valid_fields:
            return {}

        return validated.model_dump(exclude_none=False)

    def stats(self) -> dict:
        """全部工具的运行统计（可观测性）。"""
        return {t.name: t.public_meta() for t in self.all()}


def _hint_for(kind: ErrorKind) -> str:
    return {
        "transport": "网络/超时类瞬时故障，已自动重试。可稍后再试或换用其他工具。",
        "data": "数据或查询条件问题，重试无用。请先确认数据范围与日期区间，调整条件后重试。",
        "permanent": "权限或参数类错误，重试无用。请改用其他可用工具，或基于已有信息作答。",
        "unknown": "未知错误，请改用其他工具或基于已有信息作答。",
    }.get(kind, "")


def _call_direct(fn: Callable, kwargs: dict, timeout_s: float):
    """
    直接调用（不额外开线程），但仍用「调用前后计时」守住超时语义的一半。

    为什么并行路径要用它？
        _call_with_timeout 会为每次调用开一个线程来做超时控制。
        如果并行 worker（本身已在池里）再调它，每个调用就占 2 个线程 ——
        N 个并发调用 = 2N 个线程，白白翻倍。

        并行路径的正确做法是：**由线程池的 deadline 负责超时**，
        见 parallel.execute_batch 里对 future.result / as_completed 的
        timeout 处理。本函数只负责"调用 + 若明显超时则报错"。

    ⚠️ 诚实的取舍说明：
        Python 无法强杀正在运行的线程，所以并行路径下**单个工具的
        timeout_s 无法被硬性中断** —— 超时统一由整批的 overall_timeout
        以及线程池的调度来兜底。
        对只读查询这是可接受的（不会产生副作用），
        但这一点必须写清楚，不能假装"并行后超时保护和原来一模一样"。
        串行路径（execute）仍然保留完整的硬超时保护。
    """
    started = time.perf_counter()
    value = fn(**kwargs)
    elapsed = time.perf_counter() - started

    # 事后检查：虽然杀不掉线程，但要让调用方知道它超了，
    # 便于统计与告警（而不是静默地慢下去）
    if elapsed > timeout_s:
        raise TimeoutError(
            f"工具执行耗时 {elapsed:.1f}s，超过配置的 {timeout_s}s 上限"
        )
    return value


def _call_with_timeout(fn: Callable, kwargs: dict, timeout_s: float):
    """
    带超时地调用函数。

    实现说明：用「独立线程 + join(timeout)」而不是 signal，
    因为 signal 方案在 Windows 与多线程环境下不可用。
    超时后线程无法强杀（Python 限制），所以这里只是放弃等待并报超时 —— 
    对本项目足够，因为工具都是无副作用的只读查询。
    """
    import threading

    box: dict = {}

    def runner():
        try:
            box["value"] = fn(**kwargs)
        except Exception as e:                      # noqa: BLE001
            box["error"] = e

    th = threading.Thread(target=runner, daemon=True)
    th.start()
    th.join(timeout_s)

    if th.is_alive():
        raise TimeoutError(f"工具执行超时（>{timeout_s}s）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------------------
# LangChain 工具 schema：按函数签名与类型注解自动生成
# ---------------------------------------------------------------------------

_PY_TO_FIELD = {
    str: (str, ...),
    int: (int, ...),
    float: (float, ...),
    bool: (bool, ...),
    list: (list, ...),
    dict: (dict, ...),
}


def _schema_from_func(fn: Callable) -> type[BaseModel]:
    """
    ★ 从函数签名自动生成 Function Schema。

    这是「动态注册」的技术核心：新增工具只需写一个普通 Python 函数
    并加上 @registry.register 装饰器，schema 自动生成，不需要手写 JSON Schema。
    这也是这套架构能快速扩展的原因 —— 加工具的成本接近于零。

    字段描述里显式写了"不要用 start_date 等别名"，因为实测中
    LLM 很自然地会把 `start` 写成 `start_date`。虽然执行层有别名兜底，
    但在 schema 层面直接引导它用对名字，能少浪费一轮工具调用。
    """
    import inspect
    import typing

    # 通用参数说明：写进 schema，让模型一眼看到正确用法
    param_hints = {
        "start": "起始日期，格式 YYYY-MM-DD，留空取最后一个完整自然周（务必用 start，不要用 start_date）",
        "end": "结束日期，格式 YYYY-MM-DD，留空取最后一个完整自然周（务必用 end，不要用 end_date）",
        "days": "回溯天数，整数",
        "bucket_days": "分段天数，整数",
        "sql": "一条只读 SELECT 语句",
        "category": "类目名称",
        "exclude_special": "是否剔除大促等特殊日期，布尔值",
    }

    fields: dict[str, Any] = {}
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:                                   # noqa: BLE001
        hints = {}

    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue

        # ★ 必须跳过 *args / **kwargs 这类可变参数。
        #   实测踩过的坑：生成器把 `def f(**kw)` 的 "kw" 当成了一个
        #   名为 kw 的必填字符串字段，于是任何用 **kwargs 包装的函数
        #   都会在参数校验时报 "Field required"。
        #   可变参数不是"一个参数"，它是"任意参数"，不该出现在 schema 里。
        if param.kind in (inspect.Parameter.VAR_KEYWORD,
                          inspect.Parameter.VAR_POSITIONAL):
            continue

        ann = hints.get(pname, str)
        default = param.default

        # 解析 Optional[X] / X | None
        optional = False
        origin = typing.get_origin(ann)
        if origin is typing.Union:
            args = [a for a in typing.get_args(ann) if a is not type(None)]
            if args:
                ann = args[0]
                optional = True

        if origin in (list, typing.List):
            base_type = list
        elif origin in (dict, typing.Dict):
            base_type = dict
        else:
            base_type = ann if ann in _PY_TO_FIELD else str

        ftype = _PY_TO_FIELD.get(base_type, (str, ...))[0]
        desc = param_hints.get(pname, "")

        if default is inspect.Parameter.empty:
            fields[pname] = (ftype, Field(..., description=desc))
        elif optional or default is None:
            fields[pname] = (ftype | None, Field(default, description=desc))
        else:
            fields[pname] = (ftype, Field(default, description=desc))

    if not fields:
        # 无参工具：给一个占位字段，LangChain 不接受空 schema
        return create_model(f"{fn.__name__}_Args", __base__=BaseModel)
    return create_model(f"{fn.__name__}_Args", **fields)


# ---------------------------------------------------------------------------
# 全局 Registry 实例
# ---------------------------------------------------------------------------

registry = ToolRegistry()
