"""
app/workflow/requirement.py
===========================
需求文档解析链（对应规格中占比约 45% 的「需求 / 归因解析类」任务）。

为什么需求解析需要单独一条链？
    它和分析报告的差异比看起来大得多：
      · 分析报告要的是"数据说明了什么"—— 依赖指标工具；
      · 需求解析要的是"这个改动会影响什么"—— 依赖 **表结构与血缘**，
        以及对边界情况的穷举。
    用同一套 prompt 和同一组工具去做，两边都做不好。

    ★ 但两者共享同一套骨架：意图路由 → 工具循环 → 上下文工程 → 评审。
      这正是本架构的价值：换的是模板、工具能力和输出 Schema，
      不是整条流水线。新增一类任务不需要重写引擎。
"""
from pydantic import BaseModel, Field


class RequirementItem(BaseModel):
    """一个功能点。"""

    name: str = Field(description="功能点名称")
    description: str = Field(description="功能描述：做什么、给谁用")
    input_output: str = Field(description="输入与输出")
    data_involved: list[str] = Field(
        default_factory=list, description="涉及的数据表或字段"
    )


class ImpactAssessment(BaseModel):
    """影响面评估。"""

    target: str = Field(description="影响对象：数据表 / 报表 / 调用方 / 用户侧")
    impact: str = Field(description="具体影响说明")
    risk_level: str = Field(default="medium", description="风险等级 high/medium/low")


class Clarification(BaseModel):
    """待澄清问题。"""

    question: str = Field(description="要澄清的具体问题")
    owner: str = Field(default="需求方", description="需要谁确认")
    blocking: bool = Field(default=False, description="是否阻塞开发")


class RequirementAnalysis(BaseModel):
    """需求解析的完整产物。"""

    title: str = Field(description="需求标题")
    summary: str = Field(description="需求概述，2~3 句话")
    items: list[RequirementItem] = Field(default_factory=list, description="核心功能点")
    impacts: list[dict] = Field(default_factory=list, description="影响面评估")
    boundaries: list[str] = Field(default_factory=list, description="边界与异常场景")
    clarifications: list[Clarification] = Field(
        default_factory=list, description="待澄清问题"
    )
    acceptance: list[str] = Field(default_factory=list, description="验收标准")
    confidence: str = Field(default="medium", description="整体置信度")


REQUIREMENT_SYSTEM = """你是一名资深电商产品经理，负责把业务需求拆解为清晰可执行的需求文档。

【工作方法】
1. 先调用 describe_tables 了解现有数据结构 —— 影响面评估必须基于真实表结构，
   不能凭空想象有哪些表；
2. 必要时用 run_sql 查看具体字段取值，确认改动是否影响现有数据；
3. 然后产出结构化的需求解析。

【纪律（非常重要）】
1. **不要臆测需求**。需求方没说过、文档里没写的功能，不要自己补全。
   信息不足时，写进 clarifications（待澄清问题），而不是自行假设。
2. **待澄清问题必须具体**。写"需要确认统计口径：是按订单数还是按人数"，
   不要写"需进一步沟通"这种没有信息量的话。
3. **影响面必须包含下游**。任何一个数据改动，都要考虑：
   哪些报表会变、哪些调用方受影响、历史数据是否需要回补。
4. **置信度要诚实**。如果需求描述本身模糊，confidence 标为 low。
"""


def analyze_requirement(task: str, context_dir: str | None = None) -> dict:
    """
    需求解析入口。

    这个函数是"新增一类任务"的示范：
    复用 engine 的骨架（意图路由、上下文、评审），只替换输出 Schema 与模板。
    """
    from app.context.builder import ContextBuilder

    # 复用引擎的上下文与模板基础设施，但用需求解析的模板
    ctx = ContextBuilder(role="requirement", template="requirement")
    ctx.set_task(task)

    # 注入数据结构（需求解析最需要的上下文）
    from app.db.database import table_schema_text
    try:
        ctx.inject_data("现有数据结构（影响面评估必须基于此）", {
            "schema": table_schema_text()
        })
    except Exception as e:                              # noqa: BLE001
        ctx.add_note(f"数据表结构读取失败（{type(e).__name__}），"
                     f"影响面评估可能不完整。")

    # 走工具循环拿到必要信息
    from app.workflow.loop import run_tool_loop
    loop_result = run_tool_loop(
        task=task,
        capabilities=["schema", "sql", "drilldown"],   # 需求解析不需要 metric
        context=ctx,
        max_rounds=4,
    )

    # 结构化产出
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.llm import LLMNotConfigured, structured_llm

    try:
        llm = structured_llm(RequirementAnalysis, temperature=0.2)
        result = llm.invoke([
            SystemMessage(content=REQUIREMENT_SYSTEM + "\n\n" + ctx.build()),
            HumanMessage(content=f"请解析以下需求：\n\n{task}"),
        ])
        return {"ok": True, "analysis": result.model_dump(),
                "loop": loop_result.summary()}
    except LLMNotConfigured:
        # 配置错误向上抛，让接口层返回明确的 503 指引，
        # 而不是伪装成一个"解析失败但 HTTP 200"的响应。
        raise
    except Exception as exc:                            # noqa: BLE001
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "loop": loop_result.summary(),
            "hint": "结构化解析失败。可检查 API Key 与网络，"
                    "或改用 /api/chat 走通用流程。",
        }
