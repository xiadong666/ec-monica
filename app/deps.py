"""
app/deps.py
===========
FastAPI 依赖项：认证、授权、限流、请求追踪。

把这些收在一个文件里，是为了让 main.py 的路由保持干净 —— 
路由函数只关心业务，安全与观测由依赖统一注入。

★ 关键设计：**默认拒绝**
    DEPENDENCIES 里没有显式配置的路径，一律拒绝（仅 admin 可访问）。
    这是安全设计的默认姿态：新加接口忘了配置权限时，
    结果是"访问不了"（可发现、可修复），而不是"谁都能访问"（静默漏洞）。
"""
from fastapi import Depends, Header, HTTPException, Request, status

from app import ratelimit
from app.auth import (
    Role, TokenError, User, decode_token, get_jwt_secret, user_store,
)
from app.observability import audit, get_request_id, log, set_request_context


def _fail(code: int, message: str, headers: dict | None = None) -> HTTPException:
    return HTTPException(status_code=code, detail=message, headers=headers)


def _extract_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip()


async def current_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> User:
    """
    解析并校验当前用户。

    认证失败统一返回 401 + WWW-Authenticate 头（HTTP 语义规范），
    不泄露"用户不存在"还是"密码错误"这类细节。
    """
    token = _extract_token(authorization)
    if not token:
        raise _fail(
            status.HTTP_401_UNAUTHORIZED,
            "缺少访问令牌。请在 Authorization 头中提供 Bearer <token>。"
            "先调用 POST /api/auth/login 获取。",
            {"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = decode_token(token, get_jwt_secret())
    except TokenError as exc:
        raise _fail(
            status.HTTP_401_UNAUTHORIZED,
            f"访问令牌无效：{exc}",
            {"WWW-Authenticate": f'Bearer error="invalid_token"'},
        ) from exc

    user = user_store.get(payload.get("sub", ""))
    if user is None:
        raise _fail(status.HTTP_401_UNAUTHORIZED, "令牌对应的用户不存在")
    if user.disabled:
        raise _fail(status.HTTP_403_FORBIDDEN, "该账号已被停用")

    # 把追踪上下文绑定到本次请求（后续日志自动带上）
    set_request_context(get_request_id(), user.username, user.role.value)
    request.state.user = user
    return user


async def enforce_access(
    request: Request,
    user: User = Depends(current_user),
) -> User:
    """
    授权 + 限流（认证之后执行）。

    顺序很重要：**先认证、再鉴权、最后限流**。
      如果先限流，未认证的请求也会消耗计数，
      攻击者可以用垃圾请求把正常用户的额度耗光。
    """
    path = request.url.path

    # ---------- ① 授权 ----------
    if not user.can_access(path):
        audit.record("access_denied", target=path, ok=False,
                     reason=f"角色 {user.role.value} 无权访问")
        log("WARNING", "access_denied",
            f"角色 {user.role.value} 尝试访问 {path}，已拒绝",
            path=path)
        raise _fail(
            status.HTTP_403_FORBIDDEN,
            f"当前角色（{user.role.value}）无权访问 {path}。"
            f"如需权限请联系管理员。",
        )

    # ---------- ② 限流 + 配额 ----------
    result = ratelimit.check_limits(
        username=user.username, role=user.role.value, path=path,
        daily_quota_override=user.daily_llm_quota,
    )
    if not result.allowed:
        audit.record("rate_limited", target=path, ok=False,
                     reason=result.reason)
        log("WARNING", "rate_limited", result.reason,
            path=path, limit=result.limit)
        raise _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            result.reason,
            {"Retry-After": str(result.retry_after_s or 60)},
        )

    # 把剩余配额放进响应头，前端可展示
    request.state.quota_remaining = result.remaining
    request.state.quota_limit = result.limit
    return user
