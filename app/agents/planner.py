"""
app/agents/planner.py
=====================
策略 Agent：负责「接下来怎么办」。

与 analyst 的分工：analyst 给事实与归因，planner 给动作。
planner 必须**引用** analyst 的 Insight 作为依据，不能凭空发挥。
"""
from langchain.agents import create_agent

from app.llm import build_llm
from app.schemas import ActionProposal
from app.tools import registry

# 策略 Agent 需要的工具能力：指标 + 下钻（用于核实建议依据）
PLANNER_CAPABILITIES = ["schema", "metric", "drilldown"]

PLANNER_PROMPT = """你是一名电商运营负责人，负责把经营分析结论转化为可执行的行动方案。

【输入】
你会收到：①本周核心指标 ②数据质控标记（硬约束）③分析 Agent 的归因结论。

【你的职责】
基于上述信息，产出 2~4 条行动建议。每条都必须：
- 明确指向分析结论里的某一条（在 rationale 里引用它）；
- 给出预期影响，尽量量化，并说明估算依据；
- 明确写出风险与副作用；
- 按优先级排序：P0=本周必须做（如补货、止血），P1=应该做，P2=可以排期。

【三条纪律】
1. **不要为了"有产出"而硬提建议。**
   如果数据显示本周经营正常、没有需要干预的问题，
   请只输出一条 action_type=no_action 的建议，并说明为什么不动作。
   这是完全合格的结论 —— 乱提建议的代价远高于不提。
2. **区分"边际递减"与"渠道恶化"**（看数据质控标记）：
   - 边际递减（花费涨、ROI 跌）→ 优化投放结构（ad_structure），**不要建议关停渠道**；
   - 渠道效率恶化（花费平、ROI 跌）→ 才可考虑收缩预算。
   仅凭 ROI 下降就建议关停，是在误杀仍然赚钱的渠道。
3. **归因未确定的不要给动作。**
   如果质控显示存在 blocking 级问题（如缺货），
   请优先针对该问题给动作，不要绕开它去谈详情页优化。

【护栏】
- 涉及改价、投放预算的建议，needs_approval 必须为 true（默认就是 true，不要改）。
- 任何动作不得建议绕过审批直接执行。
"""


def build_planner_agent():
    """构建策略 Agent（对照实现）。temperature 取 0.3：需要灵活性但不能太发散。"""
    tools = registry.build_langchain_tools(
        registry.select(capabilities=PLANNER_CAPABILITIES)
    )
    return create_agent(
        model=build_llm(temperature=0.3),
        tools=tools,
        system_prompt=PLANNER_PROMPT,
        response_format=ActionProposal,
    )


_planner = None


def get_planner():
    global _planner
    if _planner is None:
        _planner = build_planner_agent()
    return _planner
