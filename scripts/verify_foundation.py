"""
scripts/verify_foundation.py
============================
★ 地基验收脚本：在接 LLM 之前，先证明「确定层」是对的。

为什么必须单独验收？
    如果指标层或质控层有错，接上 LLM 之后错误会被放大成
    "看起来很专业但完全错误"的运营建议 —— 而且极难排查。
    所以先把不依赖模型的部分测到全绿，再上模型。

运行：python scripts/verify_foundation.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import (          # noqa: E402
    SQLRejected, readonly_query, table_schema_text, validate_sql,
)
from app.metrics.compute import (      # noqa: E402
    ad_efficiency_trend, channel_breakdown, get_data_date_range,
    latest_complete_week, period_compare, period_summary, previous_week_of,
    price_change_scan, product_breakdown, stockout_scan,
)
from app.quality.checks import run_all_checks   # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


# ---------------------------------------------------------------------------
section("1. 数据层：只读 SQL 护栏")

# 正常查询可用
try:
    rows = readonly_query("SELECT COUNT(*) AS n FROM dim_product")
    check("正常 SELECT 可执行", rows[0]["n"] == 12, f"实际 {rows[0]['n']} 个商品")
except Exception as e:
    check("正常 SELECT 可执行", False, str(e))

# 六类危险语句必须被拒
for bad_sql, label in [
    ("DELETE FROM fact_daily_product", "DELETE 被拒"),
    ("DROP TABLE dim_product", "DROP 被拒"),
    ("UPDATE dim_product SET price = 1", "UPDATE 被拒"),
    ("INSERT INTO dim_product VALUES ('X')", "INSERT 被拒"),
    ("SELECT * FROM secret_table", "未授权表被拒"),
    ("SELECT * FROM dim_product; DROP TABLE dim_product", "多语句被拒"),
    ("ATTACH DATABASE 'x.db' AS x", "ATTACH 被拒"),
    ("PRAGMA table_info(dim_product)", "PRAGMA 被拒"),
]:
    try:
        validate_sql(bad_sql)
        check(label, False, "未被拦截！")
    except SQLRejected:
        check(label, True)

# 自动补 LIMIT
try:
    auto = validate_sql("SELECT * FROM fact_daily_product")
    check("自动补 LIMIT", auto.upper().endswith("LIMIT 500"), auto)
except Exception as e:
    check("自动补 LIMIT", False, str(e))

# 即使绕过校验，只读连接也会阻止写入（双重保险）
try:
    from app.db.database import _connect_readonly
    c = _connect_readonly()
    c.execute("DELETE FROM dim_product")
    c.close()
    check("只读连接阻止写入", False, "居然写成功了！")
except Exception:
    check("只读连接阻止写入", True)

# 表结构说明可生成（供 prompt 注入）
try:
    schema_text = table_schema_text()
    check("表结构说明生成", "fact_daily_product" in schema_text and "channel" in schema_text)
except Exception as e:
    check("表结构说明生成", False, str(e))


# ---------------------------------------------------------------------------
section("2. 指标层：日期区间与基础指标")

d_min, d_max = get_data_date_range()
print(f"  数据范围：{d_min} ~ {d_max}")
check("数据范围正确", (d_min, d_max) == ("2025-06-01", "2025-08-29"), f"实际 {d_min}~{d_max}")

w_start, w_end = latest_complete_week()
print(f"  最后一个完整自然周：{w_start} ~ {w_end}")
from datetime import date as _date   # noqa: E402
check("完整周起始是周一", _date.fromisoformat(w_start).weekday() == 0, w_start)
check("完整周结束是周日", _date.fromisoformat(w_end).weekday() == 6, w_end)
check("完整周不晚于数据末日", w_end <= d_max, f"{w_end} vs {d_max}")

p_start, p_end = previous_week_of(w_start, w_end)
print(f"  上一周：{p_start} ~ {p_end}")
check("上一周紧邻且等长",
      (_date.fromisoformat(p_end) - _date.fromisoformat(p_start)).days == 6
      and _date.fromisoformat(p_end) < _date.fromisoformat(w_start))

s = period_summary(w_start, w_end)
print(f"  本周指标：GMV={s['gmv_gross']:.0f}  订单={s['paid_orders']}  "
      f"AOV={s['aov']:.1f}  CVR={s['pay_cvr']:.2%}  退款率={s['refund_rate']:.2%}")
check("sample_days 为 7", s["sample_days"] == 7, str(s["sample_days"]))
check("GMV 与净额关系正确", abs(s["gmv_net"] - (s["gmv_gross"] - s["refund_amount"])) < 0.01)
check("AOV = GMV/订单数", s["paid_orders"] > 0 and
      abs(s["aov"] - s["gmv_gross"] / s["paid_orders"]) < 0.05)
check("CVR 在合理区间", 0 < s["pay_cvr"] < 0.5, f"{s['pay_cvr']}")
check("毛利率在合理区间", 0 < s["gross_margin_rate"] < 1, f"{s['gross_margin_rate']}")

# 剔除大促应使 GMV 下降（因为大促日被排除）
promo_sum = period_summary("2025-06-16", "2025-06-20")
promo_clean = period_summary("2025-06-16", "2025-06-20", exclude_special=True)
print(f"  大促周 GMV={promo_sum['gmv_gross']:.0f}；"
      f"剔除大促日后样本天数={promo_clean['sample_days']} / GMV={promo_clean['gmv_gross']:.0f}")
check("剔除特殊日期后样本天数减少",
      promo_clean["sample_days"] < promo_sum["sample_days"])
check("剔除特殊日期后 GMV 下降", promo_clean["gmv_gross"] < promo_sum["gmv_gross"])


# ---------------------------------------------------------------------------
section("3. 指标层：GMV 不得因 join 而翻倍（口径护栏）")

gmv_from_product = readonly_query(
    "SELECT SUM(gmv) AS g FROM fact_daily_product WHERE stat_date BETWEEN ? AND ?",
    (w_start, w_end))[0]["g"]
gmv_from_traffic = readonly_query(
    "SELECT SUM(gmv) AS g FROM fact_daily_traffic WHERE stat_date BETWEEN ? AND ?",
    (w_start, w_end))[0]["g"]
diff = abs(gmv_from_product - gmv_from_traffic) / gmv_from_product
print(f"  商品事实表 GMV={gmv_from_product:,.0f}")
print(f"  渠道流量表 GMV={gmv_from_traffic:,.0f}   差异={diff:.2%}")
check("两表 GMV 口径一致（差异<10%）", diff < 0.10, f"差异 {diff:.2%}")


# ---------------------------------------------------------------------------
section("4. ★ 归因陷阱检测：4 个陷阱能否被自动发现")

# 陷阱1：大促
flags = run_all_checks("2025-06-16", "2025-06-20")
codes = [f["code"] for f in flags["flags"]]
print(f"  大促周检查 → {codes}")
check("陷阱1 大促被识别", "SPECIAL_PERIOD_INCLUDED" in codes, str(codes))

# 陷阱2：爆款缺货
so = stockout_scan("2025-07-01", "2025-07-20")
so_ids = {r["product_id"]: r["stockout_days"] for r in so}
print(f"  缺货扫描 → {so_ids}")
check("陷阱2 爆款P001缺货被识别", so_ids.get("P001", 0) >= 8, str(so_ids))
check("陷阱2 其他商品未误报", len(so_ids) == 1, str(so_ids))

# 陷阱3：悄悄涨价
pc = price_change_scan(days=60, threshold=0.10)
pc_sig = {r["product_id"]: r["change_pct"] for r in pc}
print("  价格突变扫描 →")
for r in pc[:5]:
    print(f"    {r['product_id']} {r['title']}: "
          f"{r['baseline_unit_price']} → {r['current_unit_price']} "
          f"({r['change_pct']:+.1%}) 自 {r['detected_from']}")
check("陷阱3 P002涨价被识别", "P002" in pc_sig, str(pc_sig))
if "P002" in pc_sig:
    check("陷阱3 涨幅方向正确（上涨）", pc_sig["P002"] > 0.10,
          f"{pc_sig['P002']:.1%}")
check("陷阱3 无误报（不应识别出大量商品）", len(pc) <= 4,
      f"识别出 {len(pc)} 个商品：{list(pc_sig)}")

# 陷阱4：投放边际递减
buckets = [b for b in ad_efficiency_trend(7) if b["roi"]]
print("  投放效率分段趋势 →")
for b in buckets[-6:]:
    extra = ""
    if "spend_change_pct" in b:
        extra = f"  花费{b['spend_change_pct']:+.1%}  ROI{b['roi_change_pct']:+.1%}"
    print(f"    {b['period']}  花费={b['ad_spend']:>9,.0f}  ROI={b['roi']:.2f}{extra}")
flg = run_all_checks(w_start, w_end)
codes = [f["code"] for f in flg["flags"]]
check("陷阱4 投放边际递减被识别", "AD_DIMINISHING_RETURN" in codes, str(codes))

# 投放加预算后 ROI 应下降
early = [b["roi"] for b in buckets if b["period"] < "2025-08-01"]
late = [b["roi"] for b in buckets if b["period"] >= "2025-08-01"]
if early and late:
    avg_e = sum(early) / len(early)
    avg_l = sum(late) / len(late)
    print(f"  8月前平均 ROI={avg_e:.2f} → 8月后平均 ROI={avg_l:.2f}")
    check("陷阱4 ROI 确实下降", avg_l < avg_e, f"{avg_e:.2f} → {avg_l:.2f}")


# ---------------------------------------------------------------------------
section("5. 质控层：blocking 与文本渲染")

mid = "2025-07-01"
flg2 = run_all_checks("2025-07-01", "2025-07-07")
print(f"  含缺货周的检查：blocking={flg2['has_blocking']}  "
      f"codes={[f['code'] for f in flg2['flags']]}")
check("含缺货周产生 blocking 级告警", flg2["has_blocking"],
      str([f["code"] for f in flg2["flags"]]))
check("质控文本可渲染", len(flg2["text"]) > 50)

flg3 = run_all_checks("2025-06-02", "2025-06-08")   # 干净周
print(f"  干净周检查：blocking={flg3['has_blocking']}  "
      f"codes={[f['code'] for f in flg3['flags']]}")
check("干净周无 blocking", not flg3["has_blocking"], str([f['code'] for f in flg3['flags']]))


# ---------------------------------------------------------------------------
section("6. 下钻：商品与渠道")

prods = product_breakdown(w_start, w_end)
print(f"  商品下钻 {len(prods)} 行，Top3：")
for p in prods[:3]:
    print(f"    {p['product_id']} {p['title']:<20} GMV={p['gmv']:>10,.0f} "
          f"CVR={p['pay_cvr']:.2%} 售价={p['stat_price']:.1f} 缺货={p['stockout_days']}天")
check("商品下钻行数正确", len(prods) == 12, str(len(prods)))
check("商品下钻含 stockout_days 字段", "stockout_days" in prods[0])
check("商品下钻含 stat_price 字段", "stat_price" in prods[0])
check("GMV 降序排列", all(prods[i]["gmv"] >= prods[i + 1]["gmv"]
                          for i in range(len(prods) - 1)))

chans = channel_breakdown(w_start, w_end)
print(f"  渠道下钻 {len(chans)} 行：")
for c in chans:
    roi = f"{c['roi']:.2f}" if c["roi"] else "—"
    print(f"    {c['channel']:<6} 访客占比={c['visitor_share']:.1%}  "
          f"GMV占比={c['gmv_share']:.1%}  CVR={c['cvr']:.2%}  ROI={roi}")
check("渠道下钻行数正确", len(chans) == 5, str(len(chans)))
check("渠道访客占比合计≈100%",
      abs(sum(c["visitor_share"] for c in chans) - 1.0) < 0.02)
check("仅付费/直播渠道有 ROI",
      all(c["roi"] is not None for c in chans if c["channel"] in ("付费", "直播")))


# ---------------------------------------------------------------------------
print()
print("=" * 72)
print(f"验收结果：{PASS} 项通过，{FAIL} 项失败")
print("=" * 72)

if FAIL == 0:
    print()
    print("✅ 确定层（数据 + 指标 + 质控）全部通过，可以接 LLM 了。")
    print("   四个归因陷阱均能被自动检测，说明 Agent 不会被假象带偏。")
else:
    print()
    print(f"❌ 有 {FAIL} 项失败，先修好再上模型 —— 否则错误会被 LLM 放大。")

sys.exit(1 if FAIL else 0)
