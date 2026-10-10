"""
app/ratelimit.py
================
★ 限流与配额。

为什么企业级必须有限流？
    每一次 /api/chat 都会真实消耗 LLM 费用。没有限流意味着：
      · 一个脚本刷接口 → 当天账单失控；
      · 一个死循环客户端 → 拖垮整个服务的响应时间；
      · 无法做容量规划（不知道谁在用、用多少）。

两层限流：
    ① 速率限制（Rate Limit）：单位时间内最多 N 次请求 —— 防突发
    ② 配额（Quota）：每天累计最多 M 次 LLM 调用 —— 防长期超支

    区分这两层很重要：速率限制防"瞬间打爆"，配额防"细水长流用超"。
    只有速率限制的话，一个客户端每秒钟 1 次、跑一整天依然会把成本打穿。

★ 存储后端的诚实说明：
    当前实现是**进程内滑动窗口**，适合单实例部署。
    多副本部署时必须换成 Redis（否则每个副本各算一份，总额度会被放大 N 倍）。
    接口已抽象为 RateLimitStore，替换时不需要改业务代码。
"""
import threading
import time
from dataclasses import dataclass, field


@dataclass
class LimitResult:
    """限流判定结果。"""

    allowed: bool
    reason: str = ""
    # 便于返回 Retry-After 头
    retry_after_s: int = 0
    # 配额相关信息（便于前端展示剩余量）
    remaining: int | None = None
    limit: int | None = None
    reset_at: float | None = None


class RateLimitStore:
    """
    限流计数器存储（进程内实现）。

    ★ 用滑动窗口而不是固定窗口：
      固定窗口有"临界问题" —— 在窗口边界前后各打满一次，
      实际瞬时速率是限额的 2 倍。
      滑动窗口按"最近 N 秒内的事件数"计数，没有这个漏洞。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # key -> list[timestamp]
        self._events: dict[str, list[float]] = {}
        # key -> (count, date_str)
        self._daily: dict[str, tuple[int, str]] = {}

    # ---------------- 速率限制 ----------------

    def hit(self, key: str, limit: int, window_s: int) -> LimitResult:
        """记录一次请求并判定是否超速。"""
        now = time.time()
        cutoff = now - window_s

        with self._lock:
            events = [t for t in self._events.get(key, []) if t > cutoff]
            if len(events) >= limit:
                oldest = min(events)
                retry_after = max(1, int(oldest + window_s - now) + 1)
                self._events[key] = events
                return LimitResult(
                    allowed=False,
                    reason=(f"请求过于频繁：{window_s} 秒内最多 {limit} 次，"
                            f"请 {retry_after} 秒后重试"),
                    retry_after_s=retry_after,
                    remaining=0, limit=limit,
                )
            events.append(now)
            self._events[key] = events
            return LimitResult(
                allowed=True, remaining=max(0, limit - len(events)), limit=limit,
            )

    # ---------------- 每日配额 ----------------

    def consume_quota(self, key: str, quota: int, day: str) -> LimitResult:
        """消耗一次配额并判定是否超限。"""
        if quota <= 0:
            return LimitResult(allowed=True, remaining=None, limit=None)

        with self._lock:
            count, stored_day = self._daily.get(key, (0, day))
            if stored_day != day:
                count, stored_day = 0, day          # 跨天自动重置

            if count >= quota:
                return LimitResult(
                    allowed=False,
                    reason=f"今日 LLM 调用配额已用完（{count}/{quota}），"
                           f"请明日再试或联系管理员提升配额",
                    remaining=0, limit=quota,
                )

            count += 1
            self._daily[key] = (count, stored_day)
            return LimitResult(
                allowed=True, remaining=quota - count, limit=quota,
            )

    def peek_quota(self, key: str, quota: int, day: str) -> LimitResult:
        """查看配额使用情况（不消耗）。"""
        if quota <= 0:
            return LimitResult(allowed=True, remaining=None, limit=None)
        with self._lock:
            count, stored_day = self._daily.get(key, (0, day))
            if stored_day != day:
                count = 0
            return LimitResult(
                allowed=count < quota,
                remaining=max(0, quota - count),
                limit=quota,
            )

    def reset(self) -> None:
        """清空所有计数（测试用）。"""
        with self._lock:
            self._events.clear()
            self._daily.clear()


# 全局单例
rate_store = RateLimitStore()


@dataclass
class RateLimitPolicy:
    """限流策略（可按角色配置）。"""

    requests_per_window: int = 30      # 窗口内最大请求数
    window_seconds: int = 60           # 窗口长度
    daily_llm_quota: int = 200         # 每日 LLM 调用配额

    @classmethod
    def for_role(cls, role: str) -> "RateLimitPolicy":
        """
        按角色给不同策略。

        ★ 这是"按成本的权限设计"：
          只读用户不消耗 LLM，所以速率可以放宽；
          分析师会跑 Agent，配额要收紧。
        """
        if role == "admin":
            return cls(requests_per_window=120, window_seconds=60,
                       daily_llm_quota=1000)
        if role == "analyst":
            return cls(requests_per_window=30, window_seconds=60,
                       daily_llm_quota=200)
        return cls(requests_per_window=60, window_seconds=60,
                   daily_llm_quota=0)        # viewer 无 LLM 配额


# 会消耗 LLM 成本的路径 —— 配额只对它们生效
LLM_PATHS = {"/api/chat", "/api/requirement", "/api/weekly-review"}


def check_limits(username: str, role: str, path: str,
                 policy: RateLimitPolicy | None = None,
                 daily_quota_override: int | None = None,
                 day: str | None = None) -> LimitResult:
    """
    统一的限流检查入口。

    返回 LimitResult；调用方在 allowed=False 时返回 429。
    """
    policy = policy or RateLimitPolicy.for_role(role)
    day = day or time.strftime("%Y-%m-%d")

    # ① 速率限制（所有接口）
    rl = rate_store.hit(f"rate:{username}", policy.requests_per_window,
                        policy.window_seconds)
    if not rl.allowed:
        return rl

    # ② 配额（只有会花 LLM 钱的路径才消耗）
    if path in LLM_PATHS:
        quota = (daily_quota_override if daily_quota_override is not None
                 else policy.daily_llm_quota)
        q = rate_store.consume_quota(f"quota:{username}:{day}", quota, day)
        if not q.allowed:
            return q
        rl.remaining = q.remaining
        rl.limit = q.limit

    return rl


def quota_status(username: str, role: str,
                 daily_quota_override: int | None = None) -> dict:
    """查询某用户当前的配额使用情况（供前端展示）。"""
    policy = RateLimitPolicy.for_role(role)
    quota = (daily_quota_override if daily_quota_override is not None
             else policy.daily_llm_quota)
    day = time.strftime("%Y-%m-%d")
    q = rate_store.peek_quota(f"quota:{username}:{day}", quota, day)
    return {
        "day": day,
        "daily_llm_quota": quota,
        "used": (quota - q.remaining) if q.remaining is not None else 0,
        "remaining": q.remaining,
        "requests_per_window": policy.requests_per_window,
        "window_seconds": policy.window_seconds,
    }
