"""
app/metrics/definitions.py
==========================
★ 口径字典（Metric Dictionary）—— 整个项目最该先写、最容易被跳过的一层。

为什么单独一层？
    "上周 GMV 多少" 这句话，运营、财务、Agent 三方给的答案可能都不一样：
    含不含退款？含不含未付款？跨天退款算哪天的？
    如果口径不写成代码，Agent 每次算出来的数字都无法互相比较，
    更无法验证对错 —— 那么"Agent 说的数字可不可信"就永远是个玄学。

本文件同时是「给 Agent 看的说明书」：EXPLAIN_FOR_AGENT 会注入到 prompt 里，
     让模型知道每个指标的确切定义，而不是自己臆测。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricDef:
    """一个指标的定义。"""
    key: str              # 程序内用的键名
    name: str             # 中文名
    formula: str          # 计算公式（人话）
    note: str             # 口径注意事项（踩坑点）


# ---------------------------------------------------------------------------
# 核心指标定义
# ---------------------------------------------------------------------------

GMV_GROSS = MetricDef(
    key="gmv_gross",
    name="成交额（GMV，未扣退款）",
    formula="SUM(fact_daily_product.gmv)",
    note="这是平台/店铺口径的成交额，不扣退款。对外汇报常用此口径。",
)

GMV_NET = MetricDef(
    key="gmv_net",
    name="净成交额（扣退款）",
    formula="SUM(gmv) - SUM(refund_amount)",
    note="看真实经营质量必须用这个口径。与 GMV 差额大 = 退货问题严重。",
)

REFUND_RATE = MetricDef(
    key="refund_rate",
    name="退款率",
    formula="SUM(refund_amount) / SUM(gmv)",
    note="大促期间会显著升高（冲动消费）。跨期退款归在退款发生日，因此大促后的退款率会偏高。",
)

AOV = MetricDef(
    key="aov",
    name="客单价",
    formula="SUM(gmv) / SUM(paid_orders)",
    note="注意分母是「订单数」不是「人数」。若需要人均需另算 ARPU。",
)

PAY_CVR = MetricDef(
    key="pay_cvr",
    name="支付转化率",
    formula="SUM(paid_orders) / SUM(visitors)",
    note=(
        "★最容易被误读的指标。转化率下跌有 4 种完全不同的病因："
        "①缺货（查 fact_daily_inventory.stockout_flag）"
        "②改价（查 dim_product.price 与竞品价）"
        "③流量结构变化（付费/直播流量转化天然低，占比升高会拉低整体）"
        "④详情页/评价问题。"
        "不做这三项交叉验证就下结论，等于瞎猜。"
    ),
)

UV = MetricDef(
    key="uv",
    name="访客数（UV）",
    formula="SUM(fact_daily_product.visitors)",
    note="商品日粒度求和，同一用户看多个商品会重复计数。全店去重 UV 需从流量表按渠道求和。",
)

ROI = MetricDef(
    key="roi",
    name="投放 ROI",
    formula="SUM(gmv) / SUM(ad_spend)  [仅 fact_daily_traffic 表]",
    note=(
        "★注意：只能用 fact_daily_traffic 表算，因为只有它有 ad_spend。"
        "绝对不要把 fact_daily_product 与 fact_daily_traffic 直接 join 后算 ROI —— "
        "两表粒度不同，join 会让 GMV 翻倍、ROI 虚高。这是本项目最常见的取数事故。"
    ),
)

GROSS_MARGIN_RATE = MetricDef(
    key="gross_margin_rate",
    name="毛利率",
    formula="SUM(paid_orders * (price - cost)) / SUM(gmv)",
    note="需 join dim_product 取 price/cost。注意涨价后要用当期价格，不能用当前维表价格倒推历史。",
)

STOCKOUT_DAYS = MetricDef(
    key="stockout_days",
    name="缺货天数",
    formula="SUM(fact_daily_inventory.stockout_flag)",
    note="识别「缺货型转化下跌」的唯一证据。看到转化跌必查此指标。",
)

ALL_METRICS = [GMV_GROSS, GMV_NET, REFUND_RATE, AOV, PAY_CVR,
               UV, ROI, GROSS_MARGIN_RATE, STOCKOUT_DAYS]


# ---------------------------------------------------------------------------
# 给 Agent 的口径说明书：会被注入 prompt
# ---------------------------------------------------------------------------
EXPLAIN_FOR_AGENT = """【指标口径字典 —— 引用数字时必须遵守，不得自行发明口径】

1. gmv_gross 成交额：SUM(fact_daily_product.gmv)，不扣退款。
2. gmv_net 净成交额：SUM(gmv) - SUM(refund_amount)。判断经营质量用这个。
3. refund_rate 退款率：SUM(refund_amount)/SUM(gmv)。
4. aov 客单价：SUM(gmv)/SUM(paid_orders)，分母是订单数。
5. pay_cvr 支付转化率：SUM(paid_orders)/SUM(visitors)。
6. uv 访客数：SUM(fact_daily_product.visitors)，同用户多商品会重复计数。
7. roi 投放 ROI：只能用 fact_daily_traffic 表算（只有它有 ad_spend）。
8. gross_margin_rate 毛利率：SUM(paid_orders*(price-cost))/SUM(gmv)。
9. stockout_days 缺货天数：SUM(fact_daily_inventory.stockout_flag)。

【三条硬规矩】
A. 禁止把 fact_daily_product 与 fact_daily_traffic join 后算 GMV 或 ROI，
   两表粒度不同，join 会导致数字翻倍。要分渠道就用流量表单独算。
B. 转化率(pay_cvr)下跌时，必须先排除这三种非病因，再谈"详情页优化"：
   ① 缺货          → 查 fact_daily_inventory.stockout_flag
   ② 改价/竞品降价 → 查 dim_product.price 与 fact_daily_competitor.avg_price
   ③ 流量结构变化  → 查 fact_daily_traffic 各渠道 visitors 占比变化
C. 任何对比都必须说明对比的两段时间，并排除大促区间（2025-06-16~06-20）。
"""


# ---------------------------------------------------------------------------
# 已知的特殊日期区间：质控层用它把"被污染的日子"标出来
# ---------------------------------------------------------------------------
class SpecialPeriod:
    """特殊日期区间。"""

    def __init__(self, name: str, start: str, end: str, reason: str):
        self.name = name
        self.start = start
        self.end = end
        self.reason = reason

    def __repr__(self) -> str:
        return f"<{self.name} {self.start}~{self.end}>"


# 大促区间：这些天的数据不能用来代表日常经营水平
SPECIAL_PERIODS = [
    SpecialPeriod("618大促", "2025-06-16", "2025-06-20",
                  "大促期间流量/转化/投放费用均异常放大，不可与日常对比"),
]
