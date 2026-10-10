"""
app/workflow/util.py
====================
工作流公共工具。

单独放一个模块的原因（这也是踩坑记录）：
    `safe_progress` 最初写在 engine.py 里，随后 loop.py 也需要它 ——
    但 engine.py 已经 import 了 loop.py，如果 loop.py 反过来 import engine，
    就形成**循环导入**。

    循环导入在 Python 里往往不报错、只是某些属性在导入时尚未绑定，
    于是表现为"运行时偶发 AttributeError/ImportError"，
    非常难排查。正确做法是把双方都要用的东西下沉到独立模块。

    **判断信号：当你想让 A import B 且 B 已经 import A 时，
    需要的那个东西就不属于 A，该抽出来。**
"""


def safe_progress(on_progress, round_no: int, message: str) -> None:
    """
    安全调用进度回调，兼容 `fn(msg)` 与 `fn(round_no, msg)` 两种签名。

    ★ 为什么需要这个函数？
      实测踩过一个必然崩的坑：引擎里写 `on_progress(msg)`（1 个参数），
      循环里写 `on_progress(round_no, msg)`（2 个参数），
      CLI 的回调按 2 个参数定义 —— 请求走到第一个进度点就
      `TypeError: missing 1 required positional argument`。

      根因不是"某一处写错了"，而是**没有把回调签名定义成契约**。
      既然约定含糊，就让调用侧自适应，而不是要求每个调用点
      都记得用的是哪一种。

    两条设计原则：
      1. **回调绝不能影响主流程**。进度上报是辅助功能，
         它自己抛异常不该让整个分析任务失败 —— 所以这里吞掉异常。
      2. 用 inspect 探测参数个数是稳定的：on_progress 一定是我们
         自己传入的普通函数，不必考虑 __call__ 对象或 *args 等复杂情况。
    """
    if on_progress is None:
        return
    try:
        import inspect

        try:
            params = inspect.signature(on_progress).parameters
            positional = [
                p for p in params.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            wants_two = len(positional) >= 2
        except (TypeError, ValueError):
            wants_two = False       # 无法探测签名时，按单参数约定处理

        if wants_two:
            on_progress(round_no, message)
        else:
            on_progress(message)
    except Exception:                                   # noqa: BLE001
        # 进度上报失败不影响任务本身 —— 辅助功能不在关键路径上
        pass
