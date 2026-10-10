"""
app/llm.py
==========
LLM 工厂：集中创建模型实例。

⚠️ 关键约束（沿用已验证的结论）：
    必须用支持 Function Calling 的模型（deepseek-chat / V3）。
    deepseek-reasoner（R1）不支持工具调用，用它会导致 Agent 无法调用工具。

为什么要有 build_llm(temperature, cheap) 两个参数？
    - temperature：分析类任务要低（0.1，要稳定可复现），
      文案类任务要高（0.7，要多样性）；
    - cheap：分类/打标这类高频小任务用便宜模型，
      别为了给一句话打标签而调用最贵的模型。
"""
from langchain_openai import ChatOpenAI

from app.config import settings


class LLMNotConfigured(RuntimeError):
    """未配置 API Key。"""


def build_llm(temperature: float = 0.1, cheap: bool = False) -> ChatOpenAI:
    """
    创建 LLM 实例。

    参数：
        temperature: 采样温度。分析/取数用 0.1，文案生成用 0.7。
        cheap:       True 则使用廉价模型（分类、打标等任务）。
    """
    if not settings.has_api_key:
        raise LLMNotConfigured(
            "未配置 DEEPSEEK_API_KEY。请复制 .env.example 为 .env 并填入 Key。\n"
            "提示：不配 Key 也可以运行 scripts/verify_foundation.py 验证数据与指标层。"
        )

    return ChatOpenAI(
        model=settings.LLM_MODEL_CHEAP if cheap else settings.LLM_MODEL,
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com/v1",
        temperature=temperature,
        timeout=120,
        max_retries=2,
    )


# ---------------------------------------------------------------------------
# ★ 结构化输出：必须指定 method，不能用 LangChain 的默认值
# ---------------------------------------------------------------------------

# 记录哪种模式可用，避免每次请求都重新试错
_STRUCTURED_METHOD: list[str] = []      # 缓存探测结果（空表示尚未确定）


def structured_llm(schema, temperature: float = 0.0, cheap: bool = False):
    """
    创建「结构化输出」LLM，并选择当前可用且实际可用的模式。

    ★ 这里踩过一个会让整个项目「只剩确定性部分能用」的坑：

      LangChain 的 `with_structured_output(Schema)` 默认使用
      `method="json_schema"`，它会发送
          response_format={"type": "json_schema", ...}
      而 **DeepSeek 的 API 不支持这个类型**，直接返回 400：
          "This response_format type is unavailable now"

      症状极具迷惑性：基础对话完全正常（`llm.invoke("你好")` 能返回），
      但所有结构化调用（规划器、意图兜底、需求解析、周复盘）全部失败 ——
      而且失败被各处 except 兜底吞掉，表现为「Agent 什么都不做就说结束了」。

      正确做法是显式指定 `method="function_calling"`，
      因为 DeepSeek 支持 Function Calling（这也是本项目 README 里
      强调"必须用 deepseek-chat 而非 R1"的同一个原因）。

    这里做成三级回退，兼顾不同供应商：
        ① function_calling —— DeepSeek 可用，且最可靠
        ② json_mode        —— 部分供应商只支持这个
        ③ 默认 json_schema —— OpenAI 等原生支持的供应商
    探测结果会缓存，避免每次请求都付一次试错成本。
    """
    llm = build_llm(temperature=temperature, cheap=cheap)

    candidates = (
        _STRUCTURED_METHOD                       # 已知可用则直接用
        or ["function_calling", "json_mode", None]
    )

    last_error: Exception | None = None
    for method in candidates:
        try:
            bound = (llm.with_structured_output(schema) if method is None
                     else llm.with_structured_output(schema, method=method))
            # 首次使用时做一次真实探测，确认这个 method 真的可用。
            # 只检查对象能否构造是发现不了服务端拒绝的 ——
            # 「response_format 类型不支持」这类错误只有真正发出请求才会暴露。
            if not _STRUCTURED_METHOD:
                _probe(bound)
                _STRUCTURED_METHOD.append("default" if method is None else method)
            return bound
        except Exception as exc:                 # noqa: BLE001
            last_error = exc
            continue

    raise LLMNotConfigured(
        f"结构化输出不可用（已尝试 function_calling / json_mode / 默认模式）。"
        f"最后一次错误：{type(last_error).__name__}: {last_error}"
    )


def _probe(bound_llm) -> None:
    """
    轻量探测：确认绑定的结构化模型真的能调通。

    故意发一个最小的真实请求 —— 只有真正发出请求，才能发现
    「response_format 类型不支持」这类服务端拒绝。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    bound_llm.invoke([
        SystemMessage(content="只输出一个 JSON 对象，不要其他内容。"),
        HumanMessage(content='返回 {"ok": true}'),
    ])


def structured_method() -> str:
    """返回当前探测到的结构化输出模式（用于诊断）。"""
    return _STRUCTURED_METHOD[0] if _STRUCTURED_METHOD else "(尚未探测)"
