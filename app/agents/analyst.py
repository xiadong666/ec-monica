"""
app/agents/analyst.py
=====================
分析 Agent：负责「发生了什么、为什么」。

职责边界（很重要，职责混在一起是 Agent 项目常见的失败原因）：
    analyst 只做**归因**，不提出行动建议。
    让它同时想"为什么"和"怎么办"，会导致它为了让建议成立而扭曲归因。

关键设计：质控结果作为**硬约束**注入 prompt，而不是等它说错再去纠正。
"""
from langchain.agents import create_agent

from app.llm import build_llm
from app.schemas import Insight
from app.tools import registry
from app.metrics.definitions import EXPLAIN_FOR_AGENT

# 分析 Agent 需要的工具能力：指标 + 下钻 + 质控 + 自由探索
ANALYST_CAPABILITIES = ["schema", "metric", "drilldown", "quality", "sql"]

ANALYST_PROMPT = """你是一名资深电商经营分析师，负责周度经营复盘中的「归因分析」部分。

【你的唯一职责】
找出本周经营数据发生了什么变化、以及**为什么**。
你**不要**提出行动建议 —— 那是策略环节的事。
如果你为了让建议成立而扭曲归因，整个复盘就会失效。

【工作方法（必须遵守顺序）】
1. 先调用 get_data_range 确认数据范围，再调用 run_quality_checks 运行数据质控；
2. 调用 compare_weeks / get_product_breakdown / get_channel_breakdown 等工具取数；
3. 只有在指标工具无法覆盖时，才用 run_sql 做补充探索；
4. 基于质控结果与数据，产出 3~5 条 Insight。

【绝对禁止】
- 禁止自己编造数字。所有数值必须来自工具返回结果，并在 evidence 里注明来源。
- 禁止违反下面的指标口径字典。
- 禁止提出行动建议（不要出现"应该""建议补货""应当调整"这类措辞）。
- 禁止在质控有 blocking 项时，给出"策略有效/无效"这类因果结论。

【归因纪律】
- 转化率(pay_cvr)下滑时，必须先排除：①缺货 ②改价 ③流量结构变化，
  然后才允许讨论详情页/评价等商品自身原因。
- 任何一条 Insight 若可能受大促/缺货/改价/流量结构影响，必须
  is_confounded=true 并在 confounders 里写清具体因素。
- 样本不足（如不足 7 天）时，confidence 必须是 low，且描述里不得使用"趋势"一词。

{metric_dict}
"""


def build_analyst_agent():
    """
    构建分析 Agent（LangChain create_agent 版本）。

    注意：这是**对照实现**。
    生产路径走 app/workflow/loop.py 的显式工具循环 ——
    因为规格要求"数据预处理算好同比/环比后再注入 Context"，
    而 create_agent 的内部循环插不进这一步（工具结果会以原始形态
    直接进入消息历史，模型还得自己算环比）。
    保留本实现便于对比两种方案的差异。
    """
    tools = registry.build_langchain_tools(
        registry.select(capabilities=ANALYST_CAPABILITIES)
    )
    return create_agent(
        model=build_llm(temperature=0.1),
        tools=tools,
        system_prompt=ANALYST_PROMPT.format(metric_dict=EXPLAIN_FOR_AGENT),
        response_format=Insight,
    )


_analyst = None


def get_analyst():
    """懒加载并缓存，避免每次请求重建 Agent。"""
    global _analyst
    if _analyst is None:
        _analyst = build_analyst_agent()
    return _analyst
