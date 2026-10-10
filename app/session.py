"""
app/session.py
==============
★ 会话存储（可插拔后端）。

为什么要外置？
    原来会话存在 `app/main.py` 里的一个模块级 dict：

        _SESSIONS: dict[str, "object"] = {}

    这带来两个企业级阻塞问题：
      ① **重启即丢失** —— 部署、扩容、崩溃重启后，所有用户的对话上下文断裂；
      ② **多副本不共享** —— 负载均衡把同一用户打到不同副本时，
         会话历史不一致，表现为"Agent 时好时坏地记不住之前聊过什么"。

    这两个问题在小规模时不明显，一旦横向扩容就会立刻暴露。

设计：抽象为 SessionStore 接口
    · InMemorySessionStore —— 单实例开发/测试
    · RedisSessionStore    —— 多副本生产（需要 redis 包，可选依赖）

    ★ 用抽象接口而不是直接写 Redis，是因为"可替换的基础设施"要满足：
      换后端不改业务代码。这也是本项目一贯的做法
      （用户存储、指标计算都遵循同一原则）。
"""
import json
import os
import threading
import time
from typing import Any, Protocol

from app.workflow.engine import SessionHistory


class SessionStore(Protocol):
    """会话存储接口。"""

    def get(self, session_id: str) -> SessionHistory: ...
    def save(self, session_id: str, history: SessionHistory) -> None: ...
    def delete(self, session_id: str) -> None: ...
    def stats(self) -> dict: ...


class InMemorySessionStore:
    """
    进程内会话存储。

    ★ 适用场景要如实说明：**仅单实例部署可用**。
      多副本时必须换 RedisSessionStore，否则会话不共享。
      健康检查会暴露当前用的是哪种后端，避免"以为换了其实没换"。
    """

    def __init__(self, max_sessions: int = 1000, ttl_seconds: int = 8 * 3600):
        self._lock = threading.Lock()
        self._data: dict[str, tuple[SessionHistory, float]] = {}
        self.max_sessions = max_sessions
        self.ttl_seconds = ttl_seconds

    def _evict(self) -> None:
        """
        清理过期 + 超量。必须在持锁状态下调用。

        ★ 实测踩过的 bug：最初 `get()` 里先 `_evict()` 再插入新会话，
          导致"淘汰时是 3 个，插入后变 4 个" —— 上限从来没真正守住。
          正确顺序是**先插入或更新，再淘汰**。
          这类"边界检查时机不对"的 bug 很难靠读代码发现，
          因为它只在恰好达到上限时才暴露。
        """
        now = time.time()
        expired = [k for k, (_, ts) in self._data.items()
                   if now - ts > self.ttl_seconds]
        for k in expired:
            self._data.pop(k, None)

        # 超量时淘汰最旧的（LRU 近似）
        if len(self._data) > self.max_sessions:
            ordered = sorted(self._data.items(), key=lambda kv: kv[1][1])
            for k, _ in ordered[: len(self._data) - self.max_sessions]:
                self._data.pop(k, None)

    def get(self, session_id: str) -> SessionHistory:
        with self._lock:
            entry = self._data.get(session_id)
            if entry is None:
                history = SessionHistory()
            else:
                history = entry[0]
            # 先写入/刷新，再淘汰 —— 保证淘汰后数量不超过上限
            self._data[session_id] = (history, time.time())
            self._evict()
            return history

    def save(self, session_id: str, history: SessionHistory) -> None:
        with self._lock:
            self._data[session_id] = (history, time.time())
            self._evict()

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._data.pop(session_id, None)

    def stats(self) -> dict:
        with self._lock:
            # ★ 不要在 stats 里调用 _evict() —— stats 是只读观测接口，
            #   让它产生副作用（删数据）会让调用方难以预料。
            return {
                "backend": "memory",
                "sessions": len(self._data),
                "max_sessions": self.max_sessions,
                "ttl_seconds": self.ttl_seconds,
                "shared_across_replicas": False,
            }


class RedisSessionStore:
    """
    Redis 会话存储（多副本部署用）。

    ★ 需要 pip install redis；未安装时不会阻塞启动 ——
      而是明确报错并提示，避免"静默降级成内存存储"这种危险行为。
      （静默降级会让多副本部署出现难以排查的会话丢失问题。）
    """

    def __init__(self, url: str, ttl_seconds: int = 8 * 3600,
                 prefix: str = "ecmonica:sess:"):
        try:
            import redis                                    # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "使用 Redis 会话存储需要安装 redis 包：pip install redis。"
                "如果你只是想单实例运行，请把 EC_SESSION_BACKEND 设为 memory。"
            ) from exc

        self._client = redis.from_url(url, decode_responses=True)
        self.ttl_seconds = ttl_seconds
        self.prefix = prefix
        # 连不通时立刻失败，而不是等第一个请求才报错
        self._client.ping()

    def _key(self, session_id: str) -> str:
        return f"{self.prefix}{session_id}"

    def get(self, session_id: str) -> SessionHistory:
        raw = self._client.get(self._key(session_id))
        history = SessionHistory()
        if raw:
            try:
                data = json.loads(raw)
                history.turns = data.get("turns", [])
                history.max_turns = data.get("max_turns", history.max_turns)
            except (json.JSONDecodeError, AttributeError):
                # 旧格式或损坏数据：从空历史开始，不让请求失败
                pass
        return history

    def save(self, session_id: str, history: SessionHistory) -> None:
        payload = json.dumps(
            {"turns": history.turns, "max_turns": history.max_turns},
            ensure_ascii=False,
        )
        self._client.setex(self._key(session_id), self.ttl_seconds, payload)

    def delete(self, session_id: str) -> None:
        self._client.delete(self._key(session_id))

    def stats(self) -> dict:
        try:
            count = sum(1 for _ in self._client.scan_iter(f"{self.prefix}*"))
        except Exception:                                   # noqa: BLE001
            count = -1
        return {
            "backend": "redis",
            "sessions": count,
            "ttl_seconds": self.ttl_seconds,
            "shared_across_replicas": True,
        }


_store: SessionStore | None = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStore:
    """
    获取会话存储单例。

    环境变量：
        EC_SESSION_BACKEND = memory | redis   （默认 memory）
        EC_REDIS_URL       = redis://host:6379/0
        EC_SESSION_TTL     = 秒，默认 28800（8 小时）
    """
    global _store
    if _store is not None:
        return _store

    with _store_lock:
        if _store is not None:
            return _store

        backend = os.getenv("EC_SESSION_BACKEND", "memory").strip().lower()
        ttl = int(os.getenv("EC_SESSION_TTL", str(8 * 3600)))

        if backend == "redis":
            url = os.getenv("EC_REDIS_URL", "redis://127.0.0.1:6379/0")
            _store = RedisSessionStore(url, ttl_seconds=ttl)
            print(f"[启动] 会话存储：Redis（{url}），多副本共享")
        else:
            _store = InMemorySessionStore(ttl_seconds=ttl)
            print("[启动] 会话存储：进程内存。"
                  "⚠️ 仅单实例可用；多副本部署请设 EC_SESSION_BACKEND=redis")
        return _store


def reset_session_store() -> None:
    """重置单例（测试用）。"""
    global _store
    with _store_lock:
        _store = None
