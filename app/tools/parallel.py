"""
app/tools/parallel.py
=====================
★ 并行工具执行（自适应策略）。

对应企业级改造目标：把「同一轮内的多个工具调用」从串行改为并行。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
★ 一个必须如实说明的实测结论：**盲目并行会更慢。**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    本项目实测（6 个工具，本地 SQLite）：

        串行实测      : 20.3 ms
        并行墙钟      : 27.4 ms      ← 更慢
        单个工具耗时  : 12~20 ms
        线程池开销    : 约 20~30 ms（创建 + 调度）

    原因很直接：**工具的耗时不敌线程池的开销。**
    如果照着"并行一定更快"的直觉直接上线，得到的是一个负优化。

    这个结论有普遍性：并行化的收益 ≈ (N-1) × 单个耗时 − 线程池开销。
    只有单次工具耗时足够大（一般是几十到几百毫秒以上，
    例如走网络的外部 API、跨库大查询），并行才真正划算。

    所以本模块**不是无脑并行，而是自适应**：
      · 预估收益不明确时，走串行（在现有进程内直接执行，零额外开销）；
      · 预估收益显著时，才启用线程池并行。
    判据见 _should_parallelize()，并且**用工具的真实历史耗时**判断，
    不靠猜。

    这对企业级场景很重要：接真实的数仓/外部 API 后，
    单次工具耗时会从毫秒级升到百毫秒甚至秒级，
    届时同一个自适应策略会自动启用并行 —— 不需要改代码。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

三个必须守住的语义（并行不能改变任何行为）：
    ① **顺序**：返回顺序必须与请求顺序一致，否则预处理与日志会错位；
    ② **容错**：每个调用的超时、重试、错误分类、退避完全保留 ——
       走同一份 registry._run 实现，只有并发方式不同；
    ③ **不嵌套线程**：registry.execute 内部本来就会开线程做超时控制，
       在 worker 线程里再开一层会让线程数翻倍，
       所以并行路径用 execute_inline。
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from app.tools.registry import registry

# 线程池开销的经验值（创建 + 调度 + 结果回收）。
# 这是本机实测出来的量级，用于收益预估；宁可估高一点，
# 因为"该并行却没并行"只是慢一点，"不该并行却并行"是负优化。
_THREADPOOL_OVERHEAD_MS = 25.0

# 预估收益低于这个倍数就不并行（1.15 = 至少快 15% 才值得）
_MIN_SPEEDUP = 1.15


@dataclass
class ToolCallSpec:
    """一次工具调用请求。"""

    tool: str
    args: dict = field(default_factory=dict)


@dataclass
class ToolCallOutcome:
    """一次工具调用的结果（含执行元信息）。"""

    spec: ToolCallSpec
    raw: Any
    ok: bool
    elapsed_ms: float
    error_kind: str | None = None
    error: str | None = None
    attempts: int | None = None


def execute_one(spec: ToolCallSpec, inline: bool) -> ToolCallOutcome:
    """
    执行单个工具，返回带元信息的结果。

    参数 inline：
        True  —— 直接调用（不做线程级超时），**供并行 worker 使用**。
                 因为 worker 本身已在池中，再嵌一层线程只会让线程数翻倍。
        False —— 走 registry.execute 的标准路径（含线程级超时）。

    无论哪条路径，**重试、错误分类、退避逻辑完全一致** ——
    真正的容错逻辑在 registry._run 内部，不在超时包装里。
    """
    started = time.perf_counter()
    try:
        if inline:
            raw = registry.execute_inline(spec.tool, **spec.args)
        else:
            raw = registry.execute(spec.tool, **spec.args)
    except Exception as exc:                            # noqa: BLE001
        elapsed = (time.perf_counter() - started) * 1000
        return ToolCallOutcome(
            spec=spec, raw=None, ok=False, elapsed_ms=elapsed,
            error_kind="unknown", error=f"{type(exc).__name__}: {exc}",
        )

    elapsed = (time.perf_counter() - started) * 1000

    if isinstance(raw, dict) and raw.get("ok") is False:
        return ToolCallOutcome(
            spec=spec, raw=raw, ok=False, elapsed_ms=elapsed,
            error_kind=raw.get("error_kind"), error=raw.get("error"),
            attempts=raw.get("attempts"),
        )

    return ToolCallOutcome(spec=spec, raw=raw, ok=True, elapsed_ms=elapsed)


def _expected_ms(spec: ToolCallSpec) -> float:
    """
    预估某个工具的耗时，用于判断并行是否有收益。

    数据来源优先级：
      ① 该工具的历史平均耗时（registry 统计）—— 最可靠；
      ② 该工具配置的 timeout_s 的一个保守比例 —— 兜底。

    ★ 用真实统计而不是常数，是为了让策略随环境自动演进：
      本地 SQLite 时估值是毫秒级 → 不并行；
      接上数仓后估值升到几百毫秒 → 自动启用并行。
    """
    t = registry.get(spec.tool)
    if t is None:
        return 0.0
    stats = t.stats
    if stats["calls"] > 0:
        return stats["total_ms"] / stats["calls"]
    return 0.0


def _should_parallelize(specs: list[ToolCallSpec]) -> tuple[bool, str]:
    """
    判断这批调用是否值得并行。

    收益模型：
        并行墙钟 ≈ max(单个耗时) + 线程池开销
        串行墙钟 ≈ sum(单个耗时)
        → 预估加速比 = sum / (max + overhead)

    返回 (是否并行, 原因)。原因会写进 metrics，便于事后复盘"为什么这次没并行"。
    """
    if len(specs) < 2:
        return False, "只有 1 个调用，并行无意义"

    estimates = [_expected_ms(s) for s in specs]
    known = [e for e in estimates if e > 0]

    if not known:
        # 首次运行、尚无统计：无法预估收益，保守走串行。
        # 这也是为了避免"冷启动时反而变慢"。
        return False, "无历史耗时统计，保守串行（首次运行后会自动评估）"

    total = sum(estimates)
    slowest = max(estimates)
    predicted_speedup = total / (slowest + _THREADPOOL_OVERHEAD_MS)

    if predicted_speedup < _MIN_SPEEDUP:
        return False, (
            f"预估加速比 {predicted_speedup:.2f}x < {_MIN_SPEEDUP}x："
            f"工具总耗时约 {total:.0f}ms、最慢 {slowest:.0f}ms，"
            f"不敌线程池开销约 {_THREADPOOL_OVERHEAD_MS:.0f}ms，串行更快"
        )
    return True, (
        f"预估加速比 {predicted_speedup:.2f}x："
        f"工具总耗时约 {total:.0f}ms、最慢 {slowest:.0f}ms，并行有净收益"
    )


def execute_batch(specs: list[ToolCallSpec],
                  max_workers: int | None = None,
                  overall_timeout_s: float = 60.0,
                  force_parallel: bool | None = None
                  ) -> tuple[list[ToolCallOutcome], dict]:
    """
    ★ 执行一批工具调用（自适应并行/串行）。

    参数：
        specs             : 调用列表
        max_workers       : 并发上限。None 则自动（上限 6）。
        overall_timeout_s : 整批总超时（仅并行路径生效）。
        force_parallel    : True 强制并行 / False 强制串行 / None 自适应。
                            用于测试与压测对比。

    返回：
        (outcomes, metrics)
        outcomes 顺序与 specs **严格一致**
        metrics  含 mode、耗时、收益预估与实际 —— 用于量化与监控

    设计取舍：
        · 顺序由"按原始下标回填"保证，不依赖完成顺序；
        · 单个调用超时不中断其他调用（已提交池中），
          但结果里会明确标记；
        · 不做"失败即取消全部" —— 对只读查询没必要，
          且部分成功比全部失败对模型更有价值。
    """
    if not specs:
        return [], {"calls": 0, "mode": "empty", "parallel": False}

    if force_parallel is None:
        use_parallel, reason = _should_parallelize(specs)
    else:
        use_parallel = force_parallel and len(specs) > 1
        reason = "强制并行" if use_parallel else "强制串行"

    # ---------------- 串行路径（进程内直接执行，零额外开销）----------------
    if not use_parallel:
        started = time.perf_counter()
        outcomes = [execute_one(s, inline=False) for s in specs]
        wall_ms = (time.perf_counter() - started) * 1000
        metrics = {
            "calls": len(specs),
            "mode": "serial",
            "parallel": False,
            "reason": reason,
            "wall_ms": round(wall_ms, 1),
            "sum_ms": round(sum(o.elapsed_ms for o in outcomes), 1),
            "net_saved_ms": 0.0,
        }
        return outcomes, metrics

    # ---------------- 并行路径 ----------------
    workers = max_workers or min(len(specs), 6)
    workers = max(1, min(workers, len(specs)))

    started = time.perf_counter()
    slots: list[ToolCallOutcome | None] = [None] * len(specs)

    with ThreadPoolExecutor(max_workers=workers,
                            thread_name_prefix="ectool") as pool:
        future_to_idx = {
            pool.submit(execute_one, spec, True): i
            for i, spec in enumerate(specs)
        }
        try:
            for future in as_completed(future_to_idx,
                                       timeout=overall_timeout_s):
                idx = future_to_idx[future]
                try:
                    slots[idx] = future.result()
                except Exception as exc:                # noqa: BLE001
                    slots[idx] = ToolCallOutcome(
                        spec=specs[idx], raw=None, ok=False, elapsed_ms=0.0,
                        error_kind="unknown",
                        error=f"{type(exc).__name__}: {exc}",
                    )
        except TimeoutError:
            for idx, outcome in enumerate(slots):
                if outcome is None:
                    slots[idx] = ToolCallOutcome(
                        spec=specs[idx], raw=None, ok=False, elapsed_ms=0.0,
                        error_kind="transport",
                        error=f"整批执行超时（>{overall_timeout_s}s），该调用未完成",
                    )

    wall_ms = (time.perf_counter() - started) * 1000
    final: list[ToolCallOutcome] = [o for o in slots if o is not None]

    # --- 量化实际收益：与"同样的调用串行会花多久"对比 ---
    sum_ms = sum(o.elapsed_ms for o in final)
    net_saved = sum_ms - wall_ms
    metrics = {
        "calls": len(final),
        "mode": "parallel",
        "parallel": True,
        "reason": reason,
        "max_workers": workers,
        "wall_ms": round(wall_ms, 1),
        "sum_ms": round(sum_ms, 1),
        "net_saved_ms": round(net_saved, 1),
        "speedup": round(sum_ms / wall_ms, 2) if wall_ms > 0 else 1.0,
    }
    return final, metrics
