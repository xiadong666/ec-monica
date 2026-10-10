"""
app/workflow/intent.py
======================
★★ 意图识别与条件路由。

对应规格要求：
    「意图识别条件路由（需工具 / 直接生成）」
    「意图识别后按需装配工具」

核心设计：**规则优先，LLM 兜底**
    为什么不让 LLM 判断所有意图？
      · 成本：每次请求都多一次模型调用，而意图往往一眼就能看出来；
      · 稳定性：「帮我写个周报」这种输入，规则命中率极高，
        让模型判断纯属浪费，还引入了不确定性（同一句话两次可能判得不一样）；
      · 延迟：规则判断是微秒级，模型判断是秒级。

    所以：能确定判定的走规则，只有真正模糊的才交给模型。
    这也是"确定性优先"原则在入口处的应用。

路由结果同时决定三件事：
    1. 走工具循环，还是直接生成（spec 里的条件路由）；
    2. 装配哪些工具（按 capabilities）；
    3. 用哪个输出 Schema 与范文模板。
"""
import re
from enum import Enum

from pydantic import BaseModel, Field


class Intent(str, Enum):
    """四类意图。"""

    ANALYSIS_REPORT = "analysis_report"      # 数据分析与报告生成（需工具）
    ATTRIBUTION = "attribution"              # 业务归因（需工具 + 质控）
    REQUIREMENT_PARSE = "requirement_parse"  # 需求文档解析（需工具，只读数据）
    DIRECT_GENERATE = "direct_generate"      # 直接生成（无需工具）


class IntentResult(BaseModel):
    """意图识别结果。"""

    intent: Intent
    confidence: float = Field(ge=0.0, le=1.0, description="置信度")
    method: str = Field(description="判定方式：rule / llm / fallback")
    reason: str = Field(description="判定依据，便于排查误判")
    need_tools: bool = Field(description="是否需要走工具循环（条件路由的核心输出）")
    capabilities: list[str] = Field(
        default_factory=list, description="需要装配的工具能力标签"
    )
    route: str = Field(description="路由目标，对应 workflow 中的分支")


# ---------------------------------------------------------------------------
# 规则表
# ---------------------------------------------------------------------------

# --- 归因类：出现这些词，几乎一定是问"为什么" ---
ATTRIBUTION_PATTERNS = [
    r"为什么", r"为何", r"什么原因", r"原因是什么", r"归因", r"怎么(回事|了)",
    r"(下滑|下降|下跌|掉了|跌了|变差|恶化|异常)", r"(上涨|上升|增长|涨了|变好).{0,6}(原因|为什么)",
    r"是不是.{0,12}(问题|导致|原因)", r"谁的责任", r"哪里出(了)?问题",
    r"排查", r"定位(问题|原因)",
]

# --- 报告类：要产出结构化汇报 ---
#   ★ 与 DATA_SIGNAL_PATTERNS 保持覆盖一致（见文件末尾的不变量断言）。
#     实测踩过的坑：这里原本只有"表现(如何|怎么样)""情况如何"，
#     而 has_data_signal 里有"情况""怎么样" —— 两套表不一致，
#     导致「分析一下上周的经营情况」规则未命中、落到 LLM 兜底。
#     虽然硬约束兜住了（不会误判成免工具），但白多了一次 LLM 调用。
#     **同一个概念的表之间不应该有宽严差异** —— 那是 bug 的温床。
REPORT_PATTERNS = [
    r"(周报|日报|月报|周复盘|月复盘|复盘)", r"(报告|汇报|总结|分析报告)",
    r"出(一份|个)?(报告|报表)", r"生成.{0,8}(报告|分析)", r"数据(分析|洞察)",
    r"(多少|几|排名|top|最高|最低|对比|环比|同比)",
    r"(表现|情况|经营|业绩|销售|数据).{0,4}(如何|怎么样|怎样|好吗|咋样)",
    r"(如何|怎么样|怎样|咋样)",
    r"(GMV|gmv|转化率|客单价|退款率|ROI|roi|uv|UV|pv|PV)",
    r"指标", r"销量", r"销售额", r"流量",
    r"经营情况", r"经营状况",
]

# --- 需求解析类：把文档/口语需求结构化 ---
REQUIREMENT_PATTERNS = [
    r"需求", r"PRD", r"prd", r"产品文档", r"业务文档", r"需求文档",
    r"拆解", r"解析(一下)?(这个|这)?(需求|文档)", r"排期", r"评估(工作量|工期|影响)",
    r"影响(面|范围|评估)", r"边界(情况|场景)", r"验收标准", r"澄清",
]

# --- 直接生成类：纯写作/文案/话术，不需要取数 ---
#   ⚠️ 实测踩过的坑：最初写成 `(想个|起个).{0,6}(名字|标题)`，
#      结果"给这个商品想个标题"漏网了 —— 因为"想个"和"标题"之间
#      隔了"商品"两个字，而 `.{0,6}` 我写在了后面而不是中间。
#      中文口语里动词与宾语之间经常插定语（想个 / 好听的 / 名字），
#      所以动作词与目标词之间必须留间隙，否则规则会非常脆。
DIRECT_PATTERNS = [
    r"(写|拟|起草|润色|改写|生成).{0,10}(文案|话术|标题|卖点|详情页|脚本|邮件|通知|公告|sop|SOP)",
    r"(想|起|取|拟|设计).{0,8}(名字|标题|slogan|口号|昵称)",
    r"(翻译|总结这段|提炼要点)",
    r"活动(创意|玩法|主题)",
    r"怎么(写|说|表达)",
]

# ---------------------------------------------------------------------------
# ★ 单向硬约束：有"数据信号"时，禁止判为免工具意图
# ---------------------------------------------------------------------------

# 出现这些词，说明用户要的是"基于真实数据"的产出，绝不允许凭记忆作答
DATA_SIGNAL_PATTERNS = [
    # 归因类
    r"为什么", r"为何", r"什么原因", r"原因", r"归因", r"怎么(回事|了)",
    r"下滑", r"下降", r"下跌", r"跌了", r"掉了", r"变差", r"恶化", r"异常",
    r"上涨", r"上升", r"增长", r"排查", r"定位",
    # 分析类
    r"分析", r"复盘", r"看一下", r"看看", r"查一下", r"查查",
    r"对比", r"环比", r"同比", r"表现", r"情况", r"怎么样",
    # 指标 / 报告类
    r"GMV", r"gmv", r"转化率", r"客单价", r"退款率", r"ROI", r"roi",
    r"销量", r"销售额", r"流量", r"指标", r"数据", r"报告", r"报表",
    r"周报", r"日报", r"月报", r"多少", r"排名", r"趋势",
]


def has_data_signal(message: str) -> bool:
    """
    判断请求是否含有"必须基于真实数据"的信号。

    ★ 这个函数实现一条**单向硬约束**：

        含 data_signal  →  **绝不允许**判为 direct_generate（免工具）
        不含 data_signal →  才允许判为 direct_generate

      为什么是单向的？因为两个方向的错误代价完全不对称：

        · 误判为 direct_generate：模型凭记忆编数字，输出看起来专业但完全错误，
          **而且没有任何警示**。在数据分析场景里这是最危险的失败模式。
        · 误判为需要工具：多查一次数据，慢几秒、多花几分钱，但结论仍然正确。

      所以宁可多查，不可编造。

    ⚠️ 但**不能反过来**把"分析/归因"这些词强行绑定到 attribution 意图。
       那样「分析一下这个需求文档」会被推去跑数据指标工具 ——
       方向完全错了，而且更贵（白跑好几轮工具循环）。
       正确的做法是：这些词只声明"必须有数据支撑"，
       具体归到哪一类，仍由下面的完整规则表判定。
    """
    text = message or ""
    return any(re.search(p, text, re.IGNORECASE) for p in DATA_SIGNAL_PATTERNS)


# 强信号：出现则优先级最高
_STRONG_REQUIREMENT = re.compile(r"(需求文档|PRD|prd|需求拆解|拆解.{0,4}需求)")
_STRONG_DIRECT = re.compile(
    r"((写|拟|起草|润色|改写|生成).{0,10}(文案|话术|标题|卖点|详情页|脚本)"
    r"|(想|起|取).{0,8}(名字|标题|slogan))"
)


def _hit(patterns: list[str], text: str) -> list[str]:
    """返回命中的模式列表。"""
    return [p for p in patterns if re.search(p, text, re.IGNORECASE)]


def classify_by_rules(message: str) -> IntentResult | None:
    """
    规则判定。返回 None 表示规则无法确定，需要交给 LLM。

    判定顺序很重要：越"专"的意图越先判，避免被泛化模式抢走。
    """
    text = message or ""

    req_hits = _hit(REQUIREMENT_PATTERNS, text)
    attr_hits = _hit(ATTRIBUTION_PATTERNS, text)
    direct_hits = _hit(DIRECT_PATTERNS, text)
    report_hits = _hit(REPORT_PATTERNS, text)

    # --- 强信号优先 ---
    if _STRONG_REQUIREMENT.search(text):
        return _make(Intent.REQUIREMENT_PARSE, 0.95, "rule",
                     f"命中需求解析强信号：{_STRONG_REQUIREMENT.search(text).group(0)}")

    # --- 需求解析：需要至少一个需求词，且不是纯粹的取数问题 ---
    #   ★ 顺序很关键：需求类判定必须排在"单向硬约束"之前。
    #     否则「分析一下这个需求文档」会因为含"分析"（数据信号）而被
    #     硬约束改判为 analysis_report —— 方向完全错了（它要的是需求拆解，
    #     不是数据指标）。实测就踩到了这个坑。
    if req_hits and not attr_hits and not report_hits:
        return _make(Intent.REQUIREMENT_PARSE, 0.8, "rule",
                     f"命中需求解析信号：{req_hits[:3]}")

    if _STRONG_DIRECT.search(text):
        # ★ 单向硬约束：写作强信号也压不住"必须基于数据"的要求。
        #   例如"写个文案，说说上周转化率为什么下滑" —— 既要写作又要数据，
        #   此时必须走工具，不能凭记忆编数字。
        if has_data_signal(text):
            return _make(
                Intent.ATTRIBUTION, 0.65, "rule",
                "命中写作信号，但同时含数据信号，按单向硬约束改走需要工具的路径"
                "（宁可多查，不可编造）。",
            )
        return _make(Intent.DIRECT_GENERATE, 0.9, "rule",
                     f"命中直接生成强信号：{_STRONG_DIRECT.search(text).group(0)}")

    # --- 归因：出现"为什么/原因"类词（优先级高于报告类）---
    #   ★ 顺序很关键：先判归因再判报告，否则
    #     "转化率为什么下滑" 会被"转化率"命中的报告规则抢走。
    if attr_hits and report_hits:
        return _make(Intent.ATTRIBUTION, 0.85, "rule",
                     f"同时命中归因({attr_hits[:2]})与数据({report_hits[:2]})信号")
    if attr_hits:
        return _make(Intent.ATTRIBUTION, 0.8, "rule", f"命中断因信号：{attr_hits[:3]}")

    # --- 报告：明确要汇报/要数据 ---
    if len(report_hits) >= 1:
        conf = 0.85 if len(report_hits) >= 2 else 0.7
        return _make(Intent.ANALYSIS_REPORT, conf, "rule",
                     f"命中数据/报告信号：{report_hits[:3]}")

    # --- 直接生成：只有写作类词，且没有任何数据类词 ---
    if direct_hits and not report_hits:
        # ★ 单向硬约束再兜一道：即使 report_hits 为空，
        #   只要出现任何数据信号，也不允许免工具作答。
        if has_data_signal(text):
            return _make(
                Intent.ATTRIBUTION, 0.6, "rule",
                "命中写作信号，但存在数据信号，按单向硬约束改走工具路径"
                "（归因路径带质控，能力更全）。",
            )
        return _make(Intent.DIRECT_GENERATE, 0.75, "rule",
                     f"命中写作信号且无数据需求：{direct_hits[:3]}")

    # --- 兜底：含数据信号但未命中任何具体规则 → 走分析报告 ---
    #   ★ 这一步很重要：它保证"有数据信号的请求一定能被路由"，
    #     而不必依赖 LLM 兜底判定。
    #     实测中「分析一下上周的经营情况」就落到了这里 ——
    #     规则表没覆盖"经营情况"这个说法，但数据信号表覆盖了"情况"。
    #     与其反复补规则，不如加一条保守兜底：
    #     **有数据信号就一定走工具路径**（宁可多查，不可编造）。
    if has_data_signal(text):
        return _make(Intent.ANALYSIS_REPORT, 0.6, "rule",
                     "含数据信号但未命中具体规则，按「需要工具」保守处理。")

    # --- 规则无法确定 ---
    return None


def _make(intent: Intent, confidence: float, method: str, reason: str) -> IntentResult:
    """根据意图填充路由信息。"""
    spec = ROUTING[intent]
    return IntentResult(
        intent=intent, confidence=confidence, method=method, reason=reason,
        need_tools=spec["need_tools"], capabilities=spec["capabilities"],
        route=spec["route"],
    )


# ---------------------------------------------------------------------------
# ★ 路由表：意图 → 是否用工具 / 装配什么工具 / 走哪条分支
# ---------------------------------------------------------------------------

ROUTING: dict[Intent, dict] = {
    # 经营分析报告：需要指标 + 下钻 + 质控 + 自由探索
    #   ★ 为什么 analysis_report 也要装配 sql？
    #     实测踩过的坑：最初它不带 sql，结果用户问
    #     「按商品类目拆分各渠道的 GMV」这类**交叉维度**问题时，
    #     固定指标工具（get_product_breakdown 按商品、get_channel_breakdown
    #     按渠道）都答不了，而模型又没有 run_sql 可用，
    #     于是它绕道跑了 5 轮、重复调用 describe_tables 4 次才勉强作答。
    #     结论：**固定工具覆盖不了所有交叉组合，必须给一条受约束的自由探索通道**，
    #     否则模型会用"多轮绕路"来替代一次简单的 JOIN，更慢也更不准。
    Intent.ANALYSIS_REPORT: {
        "need_tools": True,
        "capabilities": ["schema", "metric", "drilldown", "quality", "sql"],
        "route": "tool_loop",
        "template": "analysis_report",
    },
    # 业务归因：必须带质控（否则归因会被污染带偏）
    Intent.ATTRIBUTION: {
        "need_tools": True,
        "capabilities": ["schema", "metric", "drilldown", "quality", "sql"],
        "route": "tool_loop",
        "template": "attribution",
    },
    # 需求解析：需要看数据结构才能评估影响面，但不需要指标
    Intent.REQUIREMENT_PARSE: {
        "need_tools": True,
        "capabilities": ["schema", "sql"],
        "route": "tool_loop",
        "template": "requirement",
    },
    # 直接生成：不装配任何工具，直接一次生成
    Intent.DIRECT_GENERATE: {
        "need_tools": False,
        "capabilities": [],
        "route": "direct_generate",
        "template": "copywriting",
    },
}


# ---------------------------------------------------------------------------
# LLM 兜底判定
# ---------------------------------------------------------------------------

_LLM_INTENT_PROMPT = """你是一个意图分类器。请判断用户请求属于哪一类，只输出分类结果。

分类标准：
- analysis_report：要数据、要指标、要报告、要汇报（如"上周GMV多少""出个周报""分析下销售情况"）
- attribution：问原因、问为什么、要归因（如"转化率为什么跌了""GMV下滑是什么原因"）
- requirement_parse：要拆解需求文档、评估影响面、写PRD（如"拆解这个需求""评估改动影响"）
- direct_generate：纯写作任务，**完全不需要任何数据**（如"写个促销文案""起个标题"）

【最高优先级的硬性规则 —— 违反即为错误】
如果请求中出现下列任何一类信号，**绝对不允许**判为 direct_generate：

  ① 问原因的：为什么、为何、什么原因、归因、怎么回事
  ② 涉及变化的：下滑、下降、下跌、变差、恶化、上涨、增长、异常
  ③ 涉及数据的：GMV、转化率、客单价、退款率、ROI、销量、销售额、流量、
                指标、数据、报告、报表、周报、日报、排名、多少、趋势
  ④ 要求分析的：分析、复盘、评估、对比、环比、同比、看一下、查一下

判断原则：
- **只要需要真实数据才能回答，就必须归为前两类之一**（analysis_report 或 attribution）；
- 问原因的一律归为 attribution，即使用户没提"数据"二字；
- 只有当请求是纯粹的写作任务、且完全不依赖任何业务数据时，才可判为 direct_generate；
- **不确定时，选择需要工具的类别** —— 误判为需要工具只是多查一次数据，
  误判为免工具会导致模型凭记忆编造数字，后者危险得多。

反例（这些都不是 direct_generate）：
- "分析一下上周的经营情况" → analysis_report（需要数据）
- "看看这个" → analysis_report（信息不足，但保守选择需要工具）
- "上周转化率为什么下滑" → attribution
"""


def classify_by_llm(message: str) -> IntentResult:
    """LLM 兜底判定（仅在规则无法确定时调用）。"""
    from langchain_core.messages import HumanMessage, SystemMessage
    from pydantic import BaseModel as BM

    from app.llm import LLMNotConfigured, structured_llm

    class _Out(BM):
        intent: Intent
        confidence: float = Field(ge=0.0, le=1.0)
        reason: str

    try:
        llm = structured_llm(_Out, temperature=0.0, cheap=True)
        out = llm.invoke([
            SystemMessage(content=_LLM_INTENT_PROMPT),
            HumanMessage(content=message),
        ])
        return _make(out.intent, out.confidence, "llm", out.reason)

    except LLMNotConfigured:
        # ★ 配置错误必须向上抛，不能被兜底吞掉。
        #   实测踩过的坑：最初这里把所有异常都兜底成"按需工具处理"，
        #   结果没配 API Key 时请求会一路走下去、在更深处以语焉不详的
        #   500 结束。配置错误应该快速失败并给出明确指引。
        #   注意：`except ... as e` 的 e 在离开该子句时会被清除，
        #   所以这里不能引用 e（否则报 NameError）。
        raise

    except Exception as exc:                            # noqa: BLE001
        # 兜底：判定失败时走"需工具"路径 ——
        # 宁可多查一次数据，也不要因为分类失败而凭空生成内容
        return _make(
            Intent.ANALYSIS_REPORT, 0.3, "fallback",
            f"意图判定失败（{type(exc).__name__}: {exc}），"
            f"保守地按「需要工具」处理。",
        )


def classify(message: str, allow_llm: bool = True) -> IntentResult:
    """
    意图识别主入口。

    流程：规则 → （无法确定且允许时）LLM → 兜底 → 单向硬约束后置校验
    """
    result = classify_by_rules(message)
    if result is None:
        if allow_llm:
            result = classify_by_llm(message)
        else:
            result = _make(Intent.ANALYSIS_REPORT, 0.3, "fallback",
                           "规则未命中且未启用 LLM 判定，按「需要工具」保守处理。")

    return enforce_data_signal(result, message)


def enforce_data_signal(result: IntentResult, message: str) -> IntentResult:
    """
    ★ 最后一道单向硬约束：有数据信号就绝不允许免工具作答。

    为什么要在出口再兜一道？—— 因为 direct_generate 的判定可能来自
    三个地方，每个都可能出错：
        ① 规则里的 DIRECT_PATTERNS（会随新写法不断漏网）
        ② LLM 兜底判定（模型可能误判，实测"看看这个"就被判成 direct_generate）
        ③ _STRONG_DIRECT 强信号（优先级太高，可能压过数据需求）

    与其在三个地方分别堵，不如在**唯一出口**统一兜底。
    这样以后无论新增哪条规则、换成哪个模型，这条底线都不会被突破。

    这是"纵深防御"在路由层的应用：入口可以宽松，出口必须严格。
    """
    if result.intent != Intent.DIRECT_GENERATE:
        return result

    if not has_data_signal(message):
        return result

    # 命中数据信号却要被判为免工具 → 强制改为需要工具的分析路径
    return IntentResult(
        intent=Intent.ANALYSIS_REPORT,
        confidence=min(result.confidence, 0.6),
        method=result.method,
        reason=(
            f"[单向硬约束拦截] 原判定为 direct_generate（{result.reason}），"
            f"但请求含数据信号，不允许凭记忆作答，已强制改走工具路径。"
        ),
        need_tools=ROUTING[Intent.ANALYSIS_REPORT]["need_tools"],
        capabilities=ROUTING[Intent.ANALYSIS_REPORT]["capabilities"],
        route=ROUTING[Intent.ANALYSIS_REPORT]["route"],
    )
