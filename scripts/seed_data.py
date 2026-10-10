"""
scripts/seed_data.py
====================
生成模拟电商数据（90 天）。

⚠️ 这份数据是「故意带病」的，这是重点：
   一份干净到没有异常的数据，无法验证 Agent 的归因能力。
   我在 2025-06-01 ~ 2025-08-29 这 90 天里埋了 4 类真实归因陷阱：

   [陷阱1] 618 大促（06-16~06-20）
           → 流量 5 倍、转化翻倍、投放费暴涨。
           → 风险：Agent 会把大促峰值当成"运营策略有效"，必须靠质控层排除。

   [陷阱2] 爆款 P001 缺货（07-05~07-12）
           → 库存为 0，转化率断崖下跌，但流量基本不变（用户还在点）。
           → 风险：Agent 会误判成"详情页/价格出问题，建议优化详情页"。
           → 真相：补货即可。只有 fact_daily_inventory 表能证明这一点。

   [陷阱3] P002 悄悄涨价（07-14 起，售价 +18%）
           → 没有大促、没有缺货，纯粹价格变化导致的转化下滑。
           → 风险：这是"真·业务问题"，需要 Agent 识别出价格弹性。
           → 与陷阱2 形成对照：现象相似，病因完全不同。

   [陷阱4] 付费渠道边际递减（08-01 起投放预算 +30%）
           → 花费涨了，ROI 反而掉了。
           → 风险：Agent 会误判成"投放渠道不行，建议关停"。
           → 真相：是加预算加到效率更差的人群上了，应该优化而非关停。

运行：python scripts/seed_data.py
"""
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

# 让脚本能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings          # noqa: E402
from app.db.schema import ALL_DDL        # noqa: E402

# --- 时间窗口：固定 90 天，保证结果可复现 ---
START_DATE = date(2025, 6, 1)
DAYS = 90

# --- 商品维表 ---
# price/cost 决定毛利；lifecycle 影响自然流量趋势
PRODUCTS = [
    # (id,  标题,                     类目,   售价,   成本,   上架日,       生命周期)
    ("P001", "超薄折叠桌面收纳架",      "家居",  89.0,  38.0, "2025-03-01", "成熟"),
    ("P002", "北欧风陶瓷马克杯套装",    "家居",  129.0, 45.0, "2025-02-10", "成熟"),
    ("P003", "便携折叠露营椅",          "户外",  199.0, 82.0, "2025-04-15", "成长"),
    ("P004", "无线蓝牙降噪耳机",        "数码",  399.0, 168.0, "2025-01-20", "成熟"),
    ("P005", "智能温控保温杯",          "数码",  159.0, 62.0, "2025-05-01", "成长"),
    ("P006", "纯棉四件套床品",          "家纺",  299.0, 118.0, "2025-03-20", "成熟"),
    ("P007", "轻量跑步运动鞋",          "运动",  359.0, 145.0, "2025-04-01", "成长"),
    ("P008", "氨基酸温和洁面乳",        "美妆",  79.0,  22.0, "2025-02-25", "成熟"),
    ("P009", "复古机械键盘 87 键",      "数码",  499.0, 210.0, "2025-05-20", "新品"),
    ("P010", "大容量双肩通勤包",        "箱包",  239.0, 88.0, "2025-03-05", "成熟"),
    ("P011", "香薰蜡烛礼盒",            "家居",  149.0, 48.0, "2025-06-10", "新品"),
    ("P012", "不锈钢真空焖烧壶",        "家居",  169.0, 66.0, "2025-04-25", "衰退"),
]

CHANNELS = ["自然搜索", "推荐", "付费", "私域", "直播"]

# --- 各商品的基础量级：曝光基数 与 基础转化率 ---
BASE_TRAFFIC = {
    "P001": (9000, 0.038), "P002": (6000, 0.032), "P003": (4200, 0.028),
    "P004": (7500, 0.022), "P005": (3800, 0.030), "P006": (5000, 0.026),
    "P007": (4400, 0.024), "P008": (8200, 0.041), "P009": (2600, 0.019),
    "P010": (5100, 0.027), "P011": (1800, 0.033), "P012": (3400, 0.021),
}

# --- 陷阱参数（集中在这里，方便调整难度）---
PROMO_START, PROMO_END = date(2025, 6, 16), date(2025, 6, 20)      # 618
STOCKOUT_PRODUCT = "P001"
STOCKOUT_START, STOCKOUT_END = date(2025, 7, 5), date(2025, 7, 12)  # 爆款缺货
PRICE_HIKE_PRODUCT = "P002"
PRICE_HIKE_DATE = date(2025, 7, 14)
PRICE_HIKE_PCT = 0.18                                               # 涨价 18%
ADS_BOOST_DATE = date(2025, 8, 1)                                   # 投放加预算
ADS_BOOST_PCT = 0.30


def is_promo(d: date) -> bool:
    return PROMO_START <= d <= PROMO_END


def is_stockout(d: date, pid: str) -> bool:
    return pid == STOCKOUT_PRODUCT and STOCKOUT_START <= d <= STOCKOUT_END


def current_price(d: date, pid: str, base_price: float) -> float:
    """P002 在 PRICE_HIKE_DATE 之后涨价，其余商品价格不变。"""
    if pid == PRICE_HIKE_PRODUCT and d >= PRICE_HIKE_DATE:
        return round(base_price * (1 + PRICE_HIKE_PCT), 2)
    return base_price


def build_rows(rng: random.Random):
    """生成所有表的行数据。"""
    dim_rows, daily_rows, inv_rows, comp_rows = [], [], [], []
    # 渠道流量按「日期 -> 渠道」聚合，先累加再落库
    traffic_acc: dict[tuple[str, str], dict] = {}

    for p in PRODUCTS:
        pid, title, cat, price, cost, launch, life = p
        dim_rows.append((pid, title, cat, price, cost, launch, life))

        base_imp, base_cvr = BASE_TRAFFIC[pid]

        for i in range(DAYS):
            d = START_DATE + timedelta(days=i)
            ds = d.isoformat()
            dow = d.weekday()

            # ---------- 1. 曝光：周末 +15%，缓慢自然趋势 ----------
            imp = base_imp * (1 + 0.15 * (dow >= 5))
            # 新品爬坡 / 衰退品下滑
            if life == "新品":
                imp *= 1 + 0.012 * i
            elif life == "衰退":
                imp *= 1 - 0.006 * i
            # 大促期间曝光暴涨
            if is_promo(d):
                imp *= rng.uniform(4.2, 5.5)
            imp *= rng.uniform(0.92, 1.08)

            # ---------- 2. 点击率 ----------
            ctr = rng.uniform(0.035, 0.075)
            if is_promo(d):
                ctr *= 1.25          # 大促期间点击意愿更强
            clicks = int(imp * ctr)
            visitors = int(clicks * rng.uniform(0.86, 0.94))

            # ---------- 3. 转化率：四类陷阱在这里体现 ----------
            cvr = base_cvr * rng.uniform(0.9, 1.1)

            if is_promo(d):
                cvr *= rng.uniform(1.7, 2.1)          # 陷阱1：大促转化翻倍
            if is_stockout(d, pid):
                cvr *= 0.12                            # 陷阱2：缺货，转化断崖
            if pid == PRICE_HIKE_PRODUCT and d >= PRICE_HIKE_DATE:
                # 陷阱3：涨价后转化下滑，且随时间恢复一点（价格弹性）
                days_after = (d - PRICE_HIKE_DATE).days
                cvr *= 0.62 + min(days_after, 20) * 0.008
            if life == "衰退":
                cvr *= 1 - 0.004 * i

            paid_orders = max(0, int(visitors * cvr))
            paid_units = paid_orders + int(paid_orders * rng.uniform(0.05, 0.25))

            eff_price = current_price(d, pid, price)
            gmv = round(paid_orders * eff_price * rng.uniform(0.97, 1.03), 2)

            # 退款率：大促冲动消费退货更高
            refund_rate = rng.uniform(0.10, 0.18) if is_promo(d) else rng.uniform(0.03, 0.08)
            refund_amount = round(gmv * refund_rate, 2)

            daily_rows.append((ds, pid, int(imp), clicks, visitors,
                               paid_orders, paid_units, gmv, refund_amount, eff_price))

            # ---------- 4. 库存快照 ----------
            if is_stockout(d, pid):
                stock, so_flag = 0, 1
            else:
                # 库存随销量消耗、周期性补货，制造锯齿形
                cycle = i % 14
                stock = int(rng.uniform(600, 1200) * (1 - cycle / 18))
                so_flag = 0
            inv_rows.append((ds, pid, max(stock, 0), so_flag))

            # ---------- 5. 渠道拆分：按固定权重把当日量分摊到各渠道 ----------
            weights = {"自然搜索": 0.32, "推荐": 0.24, "付费": 0.22, "私域": 0.12, "直播": 0.10}
            for ch, w in weights.items():
                key = (ds, ch)
                acc = traffic_acc.setdefault(
                    key, {"visitors": 0, "orders": 0, "gmv": 0.0, "ad_spend": 0.0}
                )
                ch_visitors = int(visitors * w * rng.uniform(0.85, 1.15))
                ch_orders = int(paid_orders * w * rng.uniform(0.85, 1.15))
                ch_gmv = gmv * w * rng.uniform(0.85, 1.15)
                acc["visitors"] += ch_visitors
                acc["orders"] += ch_orders
                acc["gmv"] += ch_gmv

                # 只有付费渠道有投放花费（含直播投流）
                if ch == "付费":
                    # 基础花费约为付费 GMV 的 22%（即 ROI≈4.5）
                    spend = ch_gmv * 0.22
                    if is_promo(d):
                        spend *= rng.uniform(1.8, 2.4)      # 大促抢量，费用暴涨
                    if d >= ADS_BOOST_DATE:
                        spend *= (1 + ADS_BOOST_PCT)        # 陷阱4：加预算
                    acc["ad_spend"] += spend

    # 直播渠道也有投流花费，单独补一部分
    for (ds, ch), acc in traffic_acc.items():
        if ch == "直播":
            d = date.fromisoformat(ds)
            spend = acc["gmv"] * 0.12
            if is_promo(d):
                spend *= 2.0
            if d >= ADS_BOOST_DATE:
                spend *= (1 + ADS_BOOST_PCT)
            acc["ad_spend"] += spend

    # ---------- 6. 竞品价格：围绕我方均价小幅波动 ----------
    for i in range(DAYS):
        d = START_DATE + timedelta(days=i)
        ds = d.isoformat()
        for comp in ["竞品A", "竞品B"]:
            for cat in ["家居", "数码", "美妆"]:
                base = {"家居": 150.0, "数码": 320.0, "美妆": 95.0}[cat]
                price = base * rng.uniform(0.88, 1.12)
                # P002 涨价后，竞品A 在家居类目跟涨一点（制造竞争信号）
                if cat == "家居" and d >= PRICE_HIKE_DATE and comp == "竞品A":
                    price *= 1.05
                comp_rows.append((ds, comp, cat, round(price, 2)))

    traffic_rows = [
        (ds, ch, a["visitors"], a["orders"], round(a["gmv"], 2), round(a["ad_spend"], 2))
        for (ds, ch), a in sorted(traffic_acc.items())
    ]

    return dim_rows, daily_rows, traffic_rows, inv_rows, comp_rows


def main():
    rng = random.Random(20250601)   # 固定种子 → 数据可复现
    db_path = Path(settings.DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    dim_rows, daily_rows, traffic_rows, inv_rows, comp_rows = build_rows(rng)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        for ddl in ALL_DDL:
            cur.execute(ddl)

        # 清空重建，保证多次运行结果一致
        for t in ["dim_product", "fact_daily_product", "fact_daily_traffic",
                  "fact_daily_inventory", "fact_daily_competitor"]:
            cur.execute(f"DELETE FROM {t}")

        cur.executemany("INSERT INTO dim_product VALUES (?,?,?,?,?,?,?)", dim_rows)
        cur.executemany(
            "INSERT INTO fact_daily_product VALUES (?,?,?,?,?,?,?,?,?,?)", daily_rows
        )
        cur.executemany(
            "INSERT INTO fact_daily_traffic VALUES (?,?,?,?,?,?)", traffic_rows
        )
        cur.executemany(
            "INSERT INTO fact_daily_inventory VALUES (?,?,?,?)", inv_rows
        )
        cur.executemany(
            "INSERT INTO fact_daily_competitor VALUES (?,?,?,?)", comp_rows
        )
        conn.commit()

        print(f"[完成] 数据库：{db_path}")
        print(f"  商品       : {len(dim_rows)} 条")
        print(f"  商品日事实 : {len(daily_rows)} 条（{DAYS} 天 × {len(PRODUCTS)} 商品）")
        print(f"  渠道流量   : {len(traffic_rows)} 条")
        print(f"  库存快照   : {len(inv_rows)} 条")
        print(f"  竞品价格   : {len(comp_rows)} 条")
        print(f"  日期范围   : {START_DATE} ~ {START_DATE + timedelta(days=DAYS - 1)}")
        print()
        print("  已埋入的归因陷阱：")
        print(f"    [1] 618 大促        : {PROMO_START} ~ {PROMO_END}")
        print(f"    [2] 爆款缺货        : {STOCKOUT_PRODUCT} {STOCKOUT_START} ~ {STOCKOUT_END}")
        print(f"    [3] 悄悄涨价        : {PRICE_HIKE_PRODUCT} 自 {PRICE_HIKE_DATE} 起 +{PRICE_HIKE_PCT:.0%}")
        print(f"    [4] 投放边际递减    : 自 {ADS_BOOST_DATE} 起预算 +{ADS_BOOST_PCT:.0%}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
