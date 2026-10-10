"""
app/context/compressor.py
=========================
上下文压缩。

对应规格要求：
    「携带**压缩后的**前轮结论滚动输入」

为什么必须压缩？——这是长循环能跑下去的关键
    工具循环跑到第 5 轮时，如果把每轮的完整工具返回都留在上下文里，
    光是商品下钻就有 12 行 JSON、渠道 5 行、质控几十行……
    token 会迅速膨胀，而且**噪音会盖过信号**：
    模型被大量原始数据淹没后，反而抓不住前几轮已经确认的结论，
    表现为"重复查询已经查过的数据""遗忘之前的结论"。

压缩策略（分层，越远越简）：
    最近 1 轮 → 完整保留结论 + 关键数字
    第 2~3 轮 → 只保留结论与使用的工具
    更早的轮次 → 压成一行要点

这个"近详远略"的分层是刻意设计的：
    模型对最近一轮的细节依赖最强，而早期轮次的价值主要在于
    "已经确认了什么、已经排除了什么"，不需要细节。
"""
from typing import Any


def _short(value: Any, limit: int = 160) -> str:
    """把任意值压成短字符串。"""
    if value is None:
        return "—"
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())          # 折叠空白
    return text if len(text) <= limit else text[:limit] + "…"


def compress_round(round_no: int, tool_names: list[str],
                   conclusion: str, key_numbers: dict | None = None,
                   recency_index: int = 0) -> str:
    """
    把一轮循环压成一条记录。

    参数：
        round_no     : 轮次编号
        tool_names   : 本轮调用了哪些工具
        conclusion   : 本轮得出的结论
        key_numbers  : 关键数字（会被完整保留 —— 数字是结论的锚点，不能压）
        recency_index: 距今第几轮（0=最近一轮），决定压缩强度

    ★ 设计约束：**压缩必须是净收益，但可追溯性不能丢**。
      实测踩过的两个坑：
        坑1：最初每轮无条件加 "第N轮｜工具：xxx" 的头，
             对本来就简短的结论，"头"比省掉的内容还长 ——
             压缩率算出来 108%，即"压缩"后反而更大。
        坑2：为了压掉那个头，把轮次编号也一起去掉了 ——
             结果审计时无法追溯"这个结论是哪一轮得出的"，
             而这恰恰是排查"模型在第几轮开始跑偏"的唯一线索。

      最终策略（兼顾两者）：
        · 轮次编号**永远保留** —— 它只有 4 个字符，但价值极高；
        · 工具名只在正文够长时才带（正文短时，头的开销不划算）；
        · 正文长度上限随"距今轮次"递减。
      这个细节决定了长循环跑到第 6 轮时上下文是 400 还是 4000 字符。
    """
    tools = "、".join(tool_names) if tool_names else "无"

    if recency_index == 0:
        # 最近一轮：保留关键数字 + 较长的结论正文；工具名记录进去以便追溯
        lines = [f"第{round_no}轮（工具：{tools}）"]
        if key_numbers:
            lines.append("关键数字：" + "；".join(
                f"{k}={v}" for k, v in key_numbers.items()))
        lines.append(f"结论：{_short(conclusion, 500)}")
        return "\n".join(lines)

    if recency_index <= 2:
        # 第 2~3 轮：轮次编号 + 结论摘要
        return f"第{round_no}轮｜{_short(conclusion, 260)}"

    # 更早：只留轮次编号 + 要点
    return f"第{round_no}轮｜{_short(conclusion, 110)}"


def compress_injected_data(data: dict, max_items: int | None = None,
                           max_str: int = 120) -> str:
    """
    压缩「预处理注入的数据」（工具返回结果），供 Context 使用。

    这一步是「数据预处理」与「上下文工程」的衔接点：
    算好的同比/环比指标要注入 Context，但原始结构往往冗长，
    需要先压成紧凑文本。

    ★ max_items 的取值是个权衡，实测踩过坑：
        太小（如固定 8）：交叉维度表有 35 个格子，被截成 8 个，
          模型只能看到局部，于是它老实地报告
          "工具只返回了 35 格中的前 8 项" —— 数据其实拿到了，是被我们自己砍掉的。
        太大（如全量）：长列表会挤占上下文，压缩的意义就没了。

      所以改为**按数据规模自适应**：
        小列表（≤12 项）全量保留 —— 它们通常就是核心结论所在；
        大列表保留前 20 项 —— 足够看清结构，同时注明省略数量，
        让模型知道"还有更多"而不是误以为"只有这些"。

    压缩规则：
      - 列表只保留前 max_items 项（并注明总数与省略数量）；
      - 长字符串截断；
      - 嵌套 dict 展平为一层。
    """
    if max_items is None:
        # 自适应：找出最大的列表规模来决定保留多少
        largest = 0
        for value in data.values():
            if isinstance(value, list):
                largest = max(largest, len(value))
            elif isinstance(value, dict):
                for inner in value.values():
                    if isinstance(inner, list):
                        largest = max(largest, len(inner))
        if largest <= 12:
            max_items = 50        # 小列表全量保留
        else:
            max_items = 20        # 大列表保留前 20 项

    lines: list[str] = []

    def emit(prefix: str, value: Any):
        if isinstance(value, dict):
            for k, v in value.items():
                emit(f"{prefix}{k}", v)
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}: （空）")
                return
            lines.append(f"{prefix}: 共 {len(value)} 项")
            for i, item in enumerate(value[:max_items], 1):
                if isinstance(item, dict):
                    bits = "，".join(f"{k}={_short(v, 40)}" for k, v in item.items())
                    lines.append(f"  {i}. {_short(bits, max_str)}")
                else:
                    lines.append(f"  {i}. {_short(item, max_str)}")
            if len(value) > max_items:
                lines.append(f"  …（其余 {len(value) - max_items} 项省略）")
        else:
            lines.append(f"{prefix}: {_short(value, max_str)}")

    for key, val in data.items():
        emit(key, val)

    return "\n".join(lines)


def estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数（中文约 1 字 1 token，英文约 4 字符 1 token）。

    只用于监控上下文膨胀趋势 —— 不需要精确，
    它的价值是让你在循环失控之前就发现"token 涨得太快了"。
    """
    if not text:
        return 0
    chinese = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other = len(text) - chinese
    return chinese + other // 4
