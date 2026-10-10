"""
app/schemas.py
==============
请求 / 响应模型 + 结构化产物 Schema。

为什么要给「运营结论」也定 Schema？
    让 Agent 自由输出 Markdown，你会得到一段读起来很专业、
    但无法入库、无法对比、无法审计、也无法判断对错的文字。
    定义 Schema 之后：每个结论必须挂证据、必须标置信度、
    必须说明是否受污染 —— 这样前端能渲染、数据库能存、评审能逐条挑刺。
"""
from typing import Literal

from pydantic import BaseModel, Field


# ===========================================================================
# 系统
# ===========================================================================

class HealthResponse(BaseModel):
    status: str
    model: str
    has_api_key: bool
    data_range: str
    tables: int
    # --- 企业级新增：安全与基础设施状态（只暴露布尔/计数，不泄露配置内容）---
    auth_enabled: bool = True
    session_backend: str = "memory"
    using_default_users: bool = False


# ===========================================================================
# 认证
# ===========================================================================

class LoginRequest(BaseModel):
    """登录请求。"""

    username: str = Field(description="用户名")
    password: str = Field(description="密码")
    ttl_seconds: int | None = Field(
        default=None, ge=60, le=7 * 24 * 3600,
        description="令牌有效期（秒），默认 8 小时",
    )


class LoginResponse(BaseModel):
    """登录响应。"""

    access_token: str = Field(description="JWT 访问令牌")
    token_type: str = Field(default="bearer")
    expires_in: int = Field(description="有效期（秒）")
    user: dict = Field(description="用户信息（不含任何凭据字段）")


# ===========================================================================
# 工作流请求 / 响应
# ===========================================================================

class ChatRequest(BaseModel):
    """统一入口请求。"""

    message: str = Field(description="用户请求，自动做意图识别与路由")
    session_id: str | None = Field(
        default=None, description="会话 ID，用于携带前轮结论滚动输入"
    )
    start: str | None = Field(default=None, description="分析起始日期 YYYY-MM-DD")
    end: str | None = Field(default=None, description="分析结束日期 YYYY-MM-DD")
    focus: str | None = Field(default=None, description="额外关注点")


class ChatResponse(BaseModel):
    """统一入口响应。"""

    reply: str
    intent: str | None = Field(default=None, description="识别出的意图")
    route: str | None = Field(default=None, description="路由目标")
    degraded: bool = Field(
        default=False, description="是否走了降级路径（工具失败等）"
    )
    meta: dict = Field(
        default_factory=dict,
        description="执行元信息：工具调用、轮次、耗时、上下文规模、评审结果等",
    )


class RequirementRequest(BaseModel):
    """需求解析请求。"""

    text: str = Field(description="需求描述或需求文档内容")


class SQLRequest(BaseModel):
    sql: str


# ===========================================================================
# 结构化分析产物
# ===========================================================================

class Insight(BaseModel):
    """一条经营发现。每个字段都是为了让结论可复核而存在。"""

    metric: str = Field(description="指标键名，如 pay_cvr / gmv_gross / roi")
    description: str = Field(description="一句话说明这个指标发生了什么变化")
    current_value: float = Field(description="当前值")
    previous_value: float | None = Field(default=None, description="对比期数值")
    change_pct: float | None = Field(default=None, description="变化幅度，小数形式")
    evidence: list[str] = Field(
        default_factory=list,
        description="证据：必须写明数据来源，如 'fact_daily_product 2025-08-18~08-24'",
    )
    confidence: Literal["high", "medium", "low"] = Field(
        default="medium", description="置信度。样本不足或被污染时必须降级"
    )
    is_confounded: bool = Field(
        default=False, description="是否可能被大促/缺货/改价/流量结构变化污染"
    )
    confounders: list[str] = Field(
        default_factory=list, description="若受污染，列出具体因素"
    )


class ActionProposal(BaseModel):
    """一条行动建议。默认必须人工审批 —— 电商动作直接关联钱。"""

    action_type: Literal[
        "restock", "price_change", "ad_budget", "ad_structure",
        "copy_rewrite", "recover_traffic", "no_action",
    ] = Field(description="动作类型")
    target: str = Field(description="作用对象")
    summary: str = Field(description="动作说明")
    rationale: str = Field(description="为什么这么做，必须引用分析结论")
    expected_impact: str = Field(description="预期影响，尽量量化")
    risk: str = Field(description="风险与副作用")
    priority: Literal["P0", "P1", "P2"] = Field(default="P1")
    needs_approval: bool = Field(
        default=True, description="是否需要人工审批。涉及改价/预算的一律为 true"
    )


class Critique(BaseModel):
    """评审输出：决定打回还是通过。"""

    passed: bool = Field(description="结论是否可信、可放行")
    problems: list[str] = Field(default_factory=list, description="发现的问题")
    must_fix: list[str] = Field(default_factory=list, description="必须修正的点")
    verdict: str = Field(description="一句话总评")


class WeeklyReview(BaseModel):
    """周复盘结构化产物。"""

    period: str
    headline: str
    metrics: dict = Field(
        default_factory=dict,
        description="核心指标，直接来自指标层计算，LLM 不得修改",
    )
    insights: list[Insight] = Field(default_factory=list)
    actions: list[ActionProposal] = Field(default_factory=list)
    narrative: str = Field(default="", description="复盘正文（Markdown）")
    quality_flags: list[dict] = Field(default_factory=list)
    critique: Critique | None = None
    revision_rounds: int = 0
    generated_at: str = ""


class WeeklyReviewRequest(BaseModel):
    """周复盘请求。start/end 留空则取数据中最后一个完整自然周。"""

    start: str | None = None
    end: str | None = None
    focus: str | None = Field(default=None, description="关注点")
    max_revisions: int = Field(default=1, ge=0, le=3)
