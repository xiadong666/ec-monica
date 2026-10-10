"""
app/observability.py
====================
★ 结构化日志 + 审计留痕 + 请求追踪。

为什么 print 不够用？
    原来代码里全是 print，问题有三个：
      ① **无法检索**：日志平台需要结构化字段（JSON），
         纯文本只能靠 grep，无法按 user / path / elapsed 过滤聚合；
      ② **无法关联**：一次请求会产生多条日志（意图、工具、评审），
         没有 request_id 就无法把它们串成一条链路；
      ③ **无法告警**：告警规则要基于字段（如 elapsed_ms > 30000、
         degraded=true），文本日志做不到。

三层能力：
    ① 结构化日志（JSON 行）—— 机器可读，可直接进 ELK/Loki
    ② 请求追踪（request_id）—— 把一次请求的所有日志串起来
    ③ 审计留痕（audit）—— **单独一条不可篡改的记录**，
       回答"谁在什么时候让 Agent 做了什么"

★ 为什么审计要单独一层，不能混在日志里？
    日志会滚动删除、会被采样、会因磁盘压力被丢弃；
    而审计记录涉及合规（谁看了哪些数据、Agent 给了什么建议），
    必须独立保存、不可采样、有固定字段。
    企业环境里这两者的保留策略通常是完全不同的。
"""
import json
import os
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

# 请求级上下文：用 ContextVar 而不是全局变量，
# 因为 FastAPI 是异步/多线程的，全局变量会串号。
_request_id: ContextVar[str] = ContextVar("request_id", default="-")
_user: ContextVar[str] = ContextVar("user", default="-")
_role: ContextVar[str] = ContextVar("role", default="-")


def set_request_context(request_id: str | None = None,
                        username: str = "-", role: str = "-") -> str:
    """设置当前请求的追踪上下文。"""
    rid = request_id or uuid.uuid4().hex[:12]
    _request_id.set(rid)
    _user.set(username)
    _role.set(role)
    return rid


def get_request_id() -> str:
    return _request_id.get()


def get_user() -> str:
    return _user.get()


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------------
# 结构化日志
# ---------------------------------------------------------------------------

# 日志级别（数值越大越严重）
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}
_min_level = LEVELS.get(os.getenv("EC_LOG_LEVEL", "INFO").upper(), 20)


def log(level: str, event: str, message: str = "",
        **fields: Any) -> str:
    """
    输出一条结构化日志（JSON 行）。

    参数：
        level   : DEBUG / INFO / WARNING / ERROR
        event   : 事件名（机器可读的稳定标识，如 "tool_call"、"llm_invoke"）
        message : 人读的描述
        **fields: 任意结构化字段

    ★ event 与 message 要分开：
      event 是**稳定标识**（代码里写死，不会变），用于聚合统计；
      message 是**人读描述**（可能带变量），用于排查。
      把两者混在一起，统计就无法做了。
    """
    if LEVELS.get(level.upper(), 20) < _min_level:
        return ""

    record = {
        "ts": _now_iso(),
        "level": level.upper(),
        "event": event,
        "request_id": _request_id.get(),
        "user": _user.get(),
        "role": _role.get(),
    }
    if message:
        record["message"] = message
    record.update(fields)

    line = json.dumps(record, ensure_ascii=False, default=str)
    print(line, file=sys.stderr, flush=True)
    return line


def info(event: str, message: str = "", **fields):
    return log("INFO", event, message, **fields)


def warning(event: str, message: str = "", **fields):
    return log("WARNING", event, message, **fields)


def error(event: str, message: str = "", **fields):
    return log("ERROR", event, message, **fields)


def debug(event: str, message: str = "", **fields):
    return log("DEBUG", event, message, **fields)


# ---------------------------------------------------------------------------
# 审计留痕
# ---------------------------------------------------------------------------

class AuditLog:
    """
    审计记录。

    ★ 记什么？
      · 谁（user / role）
      · 什么时候（ts）
      · 做了什么（action：chat / sql / export…）
      · 对什么做的（target：会话 ID、SQL 语句摘要）
      · 结果如何（ok / degraded / critique_passed）
      · 数据范围（scope：该用户能看到哪些数据）—— 合规必备

    ★ 为什么不采样、不滚动删除？
      审计用于合规与追责，必须完整。
      生产环境应写入独立的追加型存储（如 append-only 表 / 日志服务），
      并设置远超普通日志的保留期。
    """

    def __init__(self, path: str | None = None, buffer_size: int = 1):
        self.path = path or os.getenv("EC_AUDIT_LOG", "")
        self._buffer: list[dict] = []
        self.buffer_size = buffer_size
        self._count = 0

    def record(self, action: str, target: str = "", ok: bool = True,
               **fields: Any) -> dict:
        """写入一条审计记录。"""
        entry = {
            "ts": _now_iso(),
            "audit": True,
            "request_id": _request_id.get(),
            "user": _user.get(),
            "role": _role.get(),
            "action": action,
            "target": target,
            "ok": ok,
        }
        entry.update(fields)
        self._count += 1

        line = json.dumps(entry, ensure_ascii=False, default=str)

        # 有配置路径就落文件（追加写，不覆盖历史）
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                # 审计写失败必须显性暴露 —— 静默丢审计记录是合规事故
                error("audit_write_failed", f"审计日志写入失败：{exc}",
                      audit_path=self.path)
        else:
            print(line, file=sys.stderr, flush=True)

        return entry

    def stats(self) -> dict:
        return {
            "records": self._count,
            "persisted": bool(self.path),
            "path": self.path or "(stderr)",
        }


audit = AuditLog()


# ---------------------------------------------------------------------------
# 请求计时器
# ---------------------------------------------------------------------------

class Timer:
    """
    简易计时器，用于记录各阶段耗时。

    企业级排查"为什么这次慢"时，只知道总耗时是不够的 ——
    必须能拆到阶段（意图 / 数据准备 / 工具循环 / 汇总 / 评审）。
    """

    def __init__(self):
        self._t0 = time.perf_counter()
        self._marks: list[tuple[str, float]] = []

    def mark(self, name: str) -> float:
        """记录一个阶段耗时（毫秒）。"""
        elapsed = (time.perf_counter() - self._t0) * 1000
        self._marks.append((name, elapsed))
        return elapsed

    @property
    def total_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def breakdown(self) -> dict:
        """各阶段耗时明细（阶段间为增量）。"""
        out: dict[str, float] = {}
        prev = 0.0
        for name, elapsed in self._marks:
            out[name] = round(elapsed - prev, 1)
            prev = elapsed
        out["total"] = round(self.total_ms, 1)
        return out
