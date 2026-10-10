"""
app/main.py
===========
FastAPI 应用入口（企业级改造版）。

相对原型版的四个变化：
    ① **所有业务接口都要认证**（原来任何人都能调）；
    ② **授权 + 限流通过依赖统一注入**（不是散落在每个路由里）；
    ③ **会话外置到可插拔存储**（原来是进程内 dict）；
    ④ **结构化日志 + 审计留痕 + 请求追踪**（原来是 print）。

接口分层：
    ┌─ 公共接口   /api/health        健康检查（不需要认证，供探针使用）
    ├─ 认证接口   /api/auth/*        登录、查当前用户与配额
    ├─ 确定性接口 /api/metrics 等     不消耗 LLM，只读角色也能用
    ├─ 工作流接口 /api/chat 等        消耗 LLM，有配额限制
    └─ 可观测接口 /api/tools/stats    工具统计；/api/audit/stats 仅管理员
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app import schemas
from app.auth import Role, create_token, get_jwt_secret, user_store
from app.config import settings
from app.db.database import SQLRejected, init_db, readonly_query, table_schema_text
from app.db.schema import ALLOWED_TABLES
from app.deps import current_user, enforce_access
from app.metrics.compute import (
    channel_breakdown, get_data_date_range, latest_complete_week,
    period_compare, period_summary, product_breakdown,
)
from app.observability import (
    audit, error as log_error, get_request_id, info, set_request_context,
)
from app.quality.checks import run_all_checks
from app.ratelimit import quota_status
from app.session import get_session_store
from app.tools import registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动：建库、初始化工具注册、打印配置与安全状态。"""
    import os

    init_db()
    d_min, d_max = get_data_date_range()
    store = get_session_store()

    info("startup", "EC-Monica 启动",
         db=settings.DB_PATH, data_range=f"{d_min} ~ {d_max}",
         tools=len(registry.names()), model=settings.LLM_MODEL,
         has_api_key=settings.has_api_key,
         session_backend=store.stats().get("backend"))

    # ★ 安全启动检查：把"生产环境不该出现"的配置显式暴露出来。
    #   很多事故的根因不是没做安全，而是**以为做了**。
    warnings = []
    if user_store.using_defaults:
        warnings.append(
            "正在使用开发默认账号（admin/analyst/viewer），口令为弱口令。"
            "生产环境必须通过 EC_USERS 环境变量注入真实用户。"
        )
    if not settings.has_api_key:
        warnings.append("未配置 DEEPSEEK_API_KEY，工作流接口不可用。")
    if store.stats().get("backend") == "memory":
        warnings.append(
            "会话存储为进程内存：重启丢失、多副本不共享。"
            "生产环境请设置 EC_SESSION_BACKEND=redis。"
        )
    if not os.getenv("EC_JWT_SECRET", "").strip():
        warnings.append(
            "未配置 EC_JWT_SECRET，Token 在重启后会全部失效，"
            "且多副本部署时签名不一致。"
        )

    for w in warnings:
        info("startup_warning", w)
    if warnings:
        print(f"\n[启动警告] 共 {len(warnings)} 项需要关注：")
        for i, w in enumerate(warnings, 1):
            print(f"  {i}. {w}")
        print()

    yield
    info("shutdown", "EC-Monica 停止", audit=audit.stats())


app = FastAPI(
    title="EC-Monica 电商产运 Agent",
    description=(
        "面向产品、运营、研发的电商业务 Agent：数据分析与报告生成、"
        "业务归因、需求文档解析。\n\n"
        "**认证**：除 `/api/health` 外所有接口都需要 "
        "`Authorization: Bearer <token>`。先调用 `POST /api/auth/login` 获取令牌。\n\n"
        "**限流**：按角色配置速率与每日 LLM 配额，响应头返回 "
        "`X-Quota-Remaining`。"
    ),
    version="2.0.0",
    lifespan=lifespan,
)


# ===========================================================================
# 全局异常处理
# ===========================================================================

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """
    兜底异常处理。

    ★ 关键：响应里带 request_id。
      用户报障时给出这个 ID，就能在日志里精确定位整条链路 —— 
      "500 错误"本身没有任何可排查信息。
    """
    rid = get_request_id()
    log_error("unhandled_exception",
              f"未处理异常：{type(exc).__name__}: {exc}",
              path=request.url.path, exc_type=type(exc).__name__)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "服务内部错误，请稍后重试。",
            "request_id": rid,
        },
    )


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """
    请求级日志、追踪 ID 与计时。

    ★ 用中间件而不是在每个路由里写，是为了保证**不漏** ——
      路由里手写日志，新加接口时很容易忘记。
    """
    import time

    # 支持上游传入的 request_id（便于跨服务串联链路）
    rid = request.headers.get("X-Request-ID") or None
    set_request_context(rid) if rid else set_request_context()
    started = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        log_error("request_failed", "请求处理异常",
                  path=request.url.path, elapsed_ms=round(elapsed, 1))
        raise

    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = get_request_id()

    remaining = getattr(request.state, "quota_remaining", None)
    limit = getattr(request.state, "quota_limit", None)
    if remaining is not None:
        response.headers["X-Quota-Remaining"] = str(remaining)
        if limit:
            response.headers["X-Quota-Limit"] = str(limit)

    # 非工作流接口用 DEBUG 级，避免只读轮询把日志淹没
    from app.observability import log as _log
    level = "INFO" if request.url.path.startswith("/api/chat") else "DEBUG"
    _log(level, "http_request", "",
         method=request.method, path=request.url.path,
         status=response.status_code, elapsed_ms=round(elapsed, 1))

    return response


# ===========================================================================
# 公共接口（不要求认证 —— 健康探针无法携带 Token）
# ===========================================================================

@app.get("/api/health", response_model=schemas.HealthResponse, tags=["系统"])
def health():
    """
    健康检查。

    ★ 刻意不要求认证，但因此**绝不能泄露敏感信息**。
      所以只暴露布尔值与计数，不返回任何配置内容。
    """
    d_min, d_max = get_data_date_range()
    store = get_session_store()
    return schemas.HealthResponse(
        status="ok",
        model=settings.LLM_MODEL,
        has_api_key=settings.has_api_key,
        data_range=f"{d_min} ~ {d_max}",
        tables=len(ALLOWED_TABLES),
        auth_enabled=True,
        session_backend=store.stats().get("backend", "memory"),
        using_default_users=user_store.using_defaults,
    )


# ===========================================================================
# 认证接口
# ===========================================================================

@app.post("/api/auth/login", response_model=schemas.LoginResponse, tags=["认证"])
def login(req: schemas.LoginRequest):
    """
    登录并获取访问令牌。

    ★ 三个安全要点：
      · 不区分"用户不存在"与"密码错误"（防用户枚举）；
      · 失败也记审计（暴力破解可回溯）；
      · 返回体不含任何密码相关字段。

    ★ 这里要显式绑定请求上下文：
      登录接口是**唯一在认证之前**就要写审计的地方，
      此时中间件里还没有 user 信息。若不绑定，
      审计记录里的 user 字段会是 "-"，导致"谁登录失败了"无法追溯 ——
      实测就踩到了这一点。
    """
    from app.observability import set_request_context

    user = user_store.authenticate(req.username, req.password)
    if user is None:
        # 登录失败：审计里记下**尝试的用户名**（放在 target 里），
        # user 字段保持为尝试者，便于识别暴力破解目标
        set_request_context(get_request_id(), req.username, "-")
        audit.record("login_failed", target=req.username, ok=False)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    # 登录成功：绑定真实身份，后续日志与审计自动带上
    set_request_context(get_request_id(), user.username, user.role.value)

    ttl = req.ttl_seconds or 8 * 3600
    token = create_token(
        username=user.username, role=user.role.value,
        secret=get_jwt_secret(), ttl_seconds=ttl,
    )
    audit.record("login_success", target=user.username,
                 role=user.role.value, scope=user.scope.describe())
    return schemas.LoginResponse(
        access_token=token,
        token_type="bearer",
        expires_in=ttl,
        user=user.to_public(),
    )


@app.get("/api/auth/me", tags=["认证"])
def me(user=Depends(current_user)):
    """查看当前用户信息与配额使用情况。"""
    return {
        "user": user.to_public(),
        "quota": quota_status(user.username, user.role.value,
                              user.daily_llm_quota),
    }


# ===========================================================================
# 确定性接口（只读角色也能用，不消耗 LLM 配额）
# ===========================================================================

@app.get("/api/schema", tags=["系统"])
def api_schema(user=Depends(enforce_access)):
    """可用数据表结构。"""
    audit.record("view_schema", target="data_schema", ok=True)
    return {"schema": table_schema_text()}


@app.get("/api/metrics", tags=["经营分析"])
def api_metrics(start: str | None = None, end: str | None = None,
                exclude_special: bool = True,
                user=Depends(enforce_access)):
    """核心指标（默认最后一个完整自然周，并与上一周对比）。"""
    if not (start and end):
        start, end = latest_complete_week()
    if exclude_special:
        return period_compare(start, end, exclude_special=True)
    return {
        "current": period_summary(start, end, exclude_special=False),
        "note": "未剔除特殊日期，若区间含大促则不可与日常对比",
    }


@app.get("/api/quality", tags=["经营分析"])
def api_quality(start: str | None = None, end: str | None = None,
                user=Depends(enforce_access)):
    """
    数据质控结果。即使不接 LLM，也能直接告诉你
    「这周的数据能不能用来下结论」。
    """
    if not (start and end):
        start, end = latest_complete_week()
    qc = run_all_checks(start, end)
    return {
        "period": f"{start} ~ {end}",
        "has_blocking": qc["has_blocking"],
        "flags": qc["flags"],
        "report": qc["text"],
    }


@app.get("/api/products", tags=["经营分析"])
def api_products(start: str | None = None, end: str | None = None,
                 user=Depends(enforce_access)):
    """
    商品下钻。

    ★ 数据权限（第 ③ 层）在这里生效：受限用户只能看到 scope 内的类目。
      注意是**在数据层过滤**，不是返回后再删 —— 
      后者容易在某个分支漏掉，导致越权泄露。
    """
    if not (start and end):
        start, end = latest_complete_week()
    rows = product_breakdown(start, end)

    if not user.scope.unrestricted:
        before = len(rows)
        rows = [r for r in rows
                if user.scope.allows_category(r.get("category", ""))]
        filtered = before - len(rows)
        if filtered:
            audit.record("data_scoped", target="products", ok=True,
                         filtered_out=filtered, scope=user.scope.describe())
    return {"period": f"{start} ~ {end}", "products": rows}


@app.get("/api/channels", tags=["经营分析"])
def api_channels(start: str | None = None, end: str | None = None,
                 user=Depends(enforce_access)):
    """渠道下钻（含投放 ROI，仅付费/直播渠道有值）。"""
    if not (start and end):
        start, end = latest_complete_week()
    rows = channel_breakdown(start, end)
    if user.scope.channels is not None:
        rows = [r for r in rows if r.get("channel") in user.scope.channels]
    return {"period": f"{start} ~ {end}", "channels": rows}


@app.post("/api/sql", tags=["系统"])
def api_sql(req: schemas.SQLRequest, user=Depends(enforce_access)):
    """执行只读 SQL（经四道护栏校验）。"""
    # ★ SQL 接口重点审计：它能绕过指标层口径直接看原始数据，
    #   是数据泄露风险最高的接口。
    audit.record("run_sql", target=req.sql[:200], ok=True,
                 scope=user.scope.describe())
    try:
        rows = readonly_query(req.sql)
    except SQLRejected as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(status_code=500,
                            detail=f"{type(e).__name__}: {e}") from e
    return {"count": len(rows), "rows": rows}


# ===========================================================================
# 可观测接口
# ===========================================================================

@app.get("/api/tools", tags=["可观测"])
def api_tools(user=Depends(enforce_access)):
    """已注册工具及能力标签。"""
    return {
        "count": len(registry.names()),
        "tools": [
            {"name": t.name, "capabilities": t.capabilities,
             "description": t.description[:120],
             "timeout_s": t.timeout_s, "max_retries": t.max_retries}
            for t in registry.all()
        ],
    }


@app.get("/api/tools/stats", tags=["可观测"])
def api_tool_stats(user=Depends(enforce_access)):
    """工具运行统计（调用/失败/重试/平均耗时）。"""
    return {"stats": registry.stats()}


@app.get("/api/audit/stats", tags=["可观测"])
def api_audit_stats(user=Depends(enforce_access)):
    """审计与安全状态。"""
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="仅管理员可查看审计状态")
    store = get_session_store()
    return {
        "audit": audit.stats(),
        "sessions": store.stats(),
        "users": [u for u in user_store.list_users()],
        "using_default_users": user_store.using_defaults,
        "request_id": get_request_id(),
    }


# ===========================================================================
# 工作流接口（消耗 LLM 配额）
# ===========================================================================

@app.post("/api/chat", response_model=schemas.ChatResponse, tags=["工作流"])
def api_chat(req: schemas.ChatRequest, user=Depends(enforce_access)):
    """统一入口：自动做意图识别与条件路由。"""
    from app.llm import LLMNotConfigured
    from app.observability import Timer
    from app.workflow.engine import EcommerceAgent

    timer = Timer()
    store = get_session_store()
    session_id = req.session_id or user.username
    history = store.get(session_id)

    try:
        agent = EcommerceAgent()
        resp = agent.run(
            task=req.message, start=req.start, end=req.end, focus=req.focus,
            history=history,
        )
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:                              # noqa: BLE001
        log_error("chat_failed", f"{type(e).__name__}: {e}",
                  task=req.message[:120])
        raise HTTPException(
            status_code=500,
            detail=f"执行失败：{type(e).__name__}: {e}。"
                   f"request_id={get_request_id()}",
        ) from e

    timer.mark("agent")
    store.save(session_id, history)

    # ★ 审计：工作流调用是重点留痕对象 ——
    #   Agent 给出的建议可能影响真实经营决策，必须可追溯。
    audit.record(
        "chat", target=req.message[:200], ok=True,
        intent=resp.intent.intent.value if resp.intent else None,
        route=resp.intent.route if resp.intent else None,
        tool_calls=resp.loop.tool_calls if resp.loop else [],
        degraded=resp.degraded,
        time_mismatch=resp.time_mismatch,
        critique_passed=resp.critique.passed if resp.critique else None,
        elapsed_ms=round(resp.elapsed_ms, 1),
        scope=user.scope.describe(),
    )

    meta = resp.meta()
    meta["timing"] = timer.breakdown()
    meta["session_id"] = session_id

    return schemas.ChatResponse(
        reply=resp.content,
        intent=resp.intent.intent.value if resp.intent else None,
        route=resp.intent.route if resp.intent else None,
        degraded=resp.degraded,
        meta=meta,
    )


@app.post("/api/requirement", tags=["工作流"])
def api_requirement(req: schemas.RequirementRequest,
                    user=Depends(enforce_access)):
    """需求文档解析。"""
    from app.llm import LLMNotConfigured

    audit.record("requirement_parse", target=req.text[:200], ok=True)
    try:
        from app.workflow.requirement import analyze_requirement
        return analyze_requirement(req.text)
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"需求解析失败：{type(e).__name__}: {e}",
        ) from e


@app.post("/api/weekly-review", response_model=schemas.WeeklyReview, tags=["工作流"])
def api_weekly_review(req: schemas.WeeklyReviewRequest,
                      user=Depends(enforce_access)):
    """生成周复盘报告（结构化输出）。"""
    from app.llm import LLMNotConfigured
    from app.services.weekly_review import run_weekly_review

    audit.record("weekly_review", target=f"{req.start}~{req.end}", ok=True)
    try:
        return run_weekly_review(
            start=req.start, end=req.end, focus=req.focus,
            max_revisions=req.max_revisions,
        )
    except LLMNotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"周复盘生成失败：{type(e).__name__}: {e}",
        ) from e
