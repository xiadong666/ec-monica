"""
app/workflow/preprocess.py
==========================
★★ 数据预处理层：把工具返回的原始数据，加工成"可以直接注入 Context 的指标"。

对应规格要求：
    「数据预处理计算同比/环比指标后注入 Context」

★ 这是整个 Agent 数字可信度的技术来源，值得单独一层：

    错误做法：工具返回原始数据 → 直接丢给 LLM → "请算出环比"
              结果：模型算错，而且错得看不出来（它给出的仍是格式正确的报告）

    正确做法：工具返回原始数据 → 代码算好同比/环比 → 注入 Context
              模型只负责"解读这个变化意味着什么"

    一句话：**计算归代码，解读归模型。**

本模块从工具返回结果里自动识别可计算环比的结构并补齐变化率，
这样无论模型调用了哪个工具、以什么顺序，注入 Context 的数据
都已经带好了同比/环比，不需要模型自己算。
"""
from typing import Any

# 可作为"环比基数"的数值型字段
_NUMERIC_KEYS = {
    "gmv", "gmv_gross", "gmv_net", "paid_orders", "paid_units", "visitors",
    "impressions", "clicks", "aov", "pay_cvr", "ctr", "refund_rate",
    "refund_amount", "gross_margin_rate", "ad_spend", "roi", "orders",
    "visitor_share", "gmv_share",
}


def _pct_change(cur: float | None, prev: float | None) -> float | None:
    """计算变化率，任何一步不合法都返回 None（不猜）。"""
    if cur is None or prev is None:
        return None
    try:
        cur_f, prev_f = float(cur), float(prev)
    except (TypeError, ValueError):
        return None
    if prev_f == 0:
        return None
    return round((cur_f - prev_f) / prev_f, 5)


def enrich_period_compare(raw: dict) -> dict:
    """
    为 period_compare 的结果补齐变化率。

    工具本来就会返回 change_pct，这里做的是**兜底校验**：
    如果模型调用时漏了某个指标，或工具版本变化导致字段缺失，
    这里会把它算出来，保证注入 Context 的数据始终完整。
    """
    cur = raw.get("current") or {}
    prev = raw.get("previous") or {}
    changes = dict(raw.get("change_pct") or {})

    for key, cur_val in cur.items():
        if key not in _NUMERIC_KEYS:
            continue
        if changes.get(key) is None:
            changes[key] = _pct_change(cur_val, prev.get(key))

    return {
        "period": f"{cur.get('period', '?')}（对比 {prev.get('period', '?')}）",
        "current": {k: v for k, v in cur.items() if k in _NUMERIC_KEYS or k == "period"},
        "previous": {k: v for k, v in prev.items() if k in _NUMERIC_KEYS or k == "period"},
        "change_pct": changes,
        "摘要": _summarize_compare(changes),
    }


def _summarize_compare(changes: dict) -> str:
    """把变化率压成一句人话，方便模型快速抓住重点。"""
    label = {
        "gmv_gross": "成交额", "gmv_net": "净成交额", "paid_orders": "订单数",
        "aov": "客单价", "pay_cvr": "转化率", "visitors": "访客数",
        "refund_rate": "退款率", "gross_margin_rate": "毛利率", "ctr": "点击率",
    }
    items = []
    for k, v in changes.items():
        if v is None or k not in label:
            continue
        items.append((label[k], v))

    if not items:
        return "无可用环比数据"

    # 变动最大的三项
    items.sort(key=lambda x: abs(x[1]), reverse=True)
    top = items[:3]
    return "；".join(f"{n} {v:+.2%}" for n, v in top)


def enrich_breakdown(rows: list[dict], dimension: str) -> dict:
    """
    为下钻数据（商品/渠道）补充聚合信息。

    下钻数据动辄十几行，直接注入 Context 既占 token 又难抓重点。
    这里主动算出"贡献度"，让模型一眼看到谁是大头 —— 
    相当于**把分析的体力活提前做掉**，模型只需做判断。
    """
    if not rows:
        return {"dimension": dimension, "count": 0, "items": []}

    # 找出数值列作为排序依据
    sort_key = None
    for candidate in ("gmv", "gmv_net", "paid_orders", "visitors"):
        if all(candidate in r for r in rows):
            sort_key = candidate
            break

    data = sorted(rows, key=lambda r: r.get(sort_key, 0) or 0,
                  reverse=True) if sort_key else rows
    total = sum((r.get(sort_key, 0) or 0) for r in data) if sort_key else 0

    items = []
    for r in data:
        item = dict(r)
        if sort_key and total:
            item["贡献占比"] = round((r.get(sort_key, 0) or 0) / total, 4)
        items.append(item)

    # 标出变化最异常的项（如果有对比类字段）
    return {
        "dimension": dimension,
        "count": len(items),
        "sorted_by": sort_key,
        "items": items,
    }


def preprocess_tool_result(tool_name: str, result: Any) -> dict:
    """
    ★ 统一入口：把任意工具返回结果预处理成"可注入 Context 的形态"。

    这是"数据预处理"与"工具层"的衔接点。
    无论模型调用了什么工具，输出都经过这里，
    保证注入 Context 的数据结构一致、且已带好同比/环比。
    """
    # 工具失败：原样传递错误，不做加工
    if isinstance(result, dict) and result.get("ok") is False:
        return {"_tool_error": True, **result}

    if tool_name == "compare_weeks" and isinstance(result, dict):
        return enrich_period_compare(result)

    if tool_name == "get_period_summary" and isinstance(result, dict):
        # 单期数据：没有对比期，只做字段筛选
        return {
            "period": result.get("period"),
            "指标": {k: v for k, v in result.items() if k in _NUMERIC_KEYS},
            "sample_days": result.get("sample_days"),
        }

    if tool_name == "get_product_breakdown" and isinstance(result, list):
        return enrich_breakdown(result, "商品")

    if tool_name == "get_channel_breakdown" and isinstance(result, list):
        return enrich_breakdown(result, "渠道")

    if tool_name == "get_ad_trend" and isinstance(result, list):
        return _enrich_ad_trend(result)

    if tool_name in ("scan_stockout", "scan_price_change") and isinstance(result, list):
        return {"发现": len(result), "明细": result}

    if tool_name == "run_quality_checks" and isinstance(result, dict):
        return result

    if tool_name == "run_sql" and isinstance(result, dict):
        return result

    if tool_name == "get_data_range" and isinstance(result, dict):
        return result

    # 未知工具：原样返回（保证不丢数据）
    return result if isinstance(result, dict) else {"结果": result}


def _enrich_ad_trend(rows: list[dict]) -> dict:
    """投放趋势：直接给出首末端对比，模型不需要自己看十几行。"""
    valid = [r for r in rows if r.get("roi")]
    if len(valid) < 2:
        return {"分段": rows, "说明": "分段不足，无法判断趋势"}

    first, last = valid[0], valid[-1]
    return {
        "分段数": len(valid),
        "首段": {"期间": first.get("period"), "花费": first.get("ad_spend"),
                 "ROI": first.get("roi")},
        "末段": {"期间": last.get("period"), "花费": last.get("ad_spend"),
                 "ROI": last.get("roi")},
        "花费变化": _pct_change(last.get("ad_spend"), first.get("ad_spend")),
        "ROI变化": _pct_change(last.get("roi"), first.get("roi")),
        "解读提示": (
            "花费上升且 ROI 下降 → 边际递减，应优化投放结构而非关停渠道；"
            "花费持平且 ROI 下降 → 渠道效率真实恶化，才考虑收缩预算。"
        ),
        "全部分段": rows,
    }


def extract_key_numbers(tool_name: str, processed: dict) -> dict:
    """
    从预处理结果里抽取「关键数字」，用于滚动上下文的压缩。

    数字是结论的锚点：压缩历史时可以砍掉描述，但不能砍掉数字，
    否则后续轮次会失去判断依据（"前几轮查到多少来着？"）。
    """
    out: dict[str, Any] = {}

    if tool_name == "compare_weeks":
        cur = processed.get("current", {})
        chg = processed.get("change_pct", {})
        for k in ("gmv_gross", "pay_cvr", "paid_orders", "aov", "refund_rate"):
            if k in cur:
                out[k] = cur[k]
                if chg.get(k) is not None:
                    out[f"{k}_环比"] = chg[k]

    elif tool_name == "get_period_summary":
        out.update(processed.get("指标", {}))

    elif tool_name in ("get_product_breakdown", "get_channel_breakdown"):
        items = processed.get("items", [])
        if items:
            key_field = "product_id" if tool_name == "get_product_breakdown" else "channel"
            out["Top3"] = [
                {key_field: it.get(key_field), "gmv": it.get("gmv"),
                 "贡献占比": it.get("贡献占比")}
                for it in items[:3]
            ]

    elif tool_name == "get_ad_trend":
        out["首段ROI"] = processed.get("首段", {}).get("ROI")
        out["末段ROI"] = processed.get("末段", {}).get("ROI")
        out["花费变化"] = processed.get("花费变化")

    elif tool_name in ("scan_stockout", "scan_price_change"):
        out["发现数"] = processed.get("发现")

    elif tool_name == "run_quality_checks":
        out["blocking"] = processed.get("blocking_codes")
        out["has_blocking"] = processed.get("has_blocking")

    return out
