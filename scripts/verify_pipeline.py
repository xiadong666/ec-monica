"""
scripts/verify_pipeline.py
==========================
★ 流水线骨架验收：用「假 LLM」验证编排逻辑，不需要 API Key。

为什么值得单独做这件事？
    编排层的 bug（回环不触发、受限路径走错、重做次数不生效）
    和模型质量的 bug 是两类问题。如果混在一起用真模型测，
    你永远分不清"这次结果不好"是流程错了还是模型抽风了 —— 
    而且每次调试都要花钱、要等几十秒。

    把 LLM 换成可控的假实现后，编排逻辑可以被精确、快速、免费地验证。
    这也是"确定性骨架 + 可替换 LLM"这个设计的直接收益。

运行：python scripts/verify_pipeline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas import ActionProposal, Critique, Insight   # noqa: E402
import app.services.weekly_review as wr                     # noqa: E402
import app.agents.critic as critic_mod                      # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def section(t: str) -> None:
    print()
    print("=" * 72)
    print(t)
    print("=" * 72)


# ---------------------------------------------------------------------------
# 假 LLM 实现
# ---------------------------------------------------------------------------

def make_good_insight() -> Insight:
    """一条合规的 Insight：有证据、置信度匹配、承认了污染因素。"""
    return Insight(
        metric="pay_cvr",
        description=(
            "支付转化率环比下滑，经核查主因是 P001 超薄折叠桌面收纳架缺货 8 天，"
            "以及 P002 涨价 18% 导致的价格弹性反应；流量结构变化也有小幅影响。"
        ),
        current_value=0.0271,
        previous_value=0.0295,
        change_pct=-0.0814,
        evidence=["fact_daily_product 2025-08-18~2025-08-24",
                  "fact_daily_inventory stockout_flag"],
        confidence="medium",
        is_confounded=True,
        confounders=["P001 缺货 8 天", "P002 涨价 18%"],
    )


def make_bad_insight() -> Insight:
    """故意违规：无证据 + 声称受污染却给 high 置信度 + 没提缺货。"""
    return Insight(
        metric="pay_cvr",
        description="转化率下滑说明详情页不行，运营策略效果不佳。",
        current_value=0.0271,
        previous_value=0.0295,
        change_pct=-0.0814,
        evidence=[],                      # 违规：无证据
        confidence="high",                # 违规：受污染却 high
        is_confounded=True,
        confounders=[],                   # 违规：受污染但没写因素
    )


def make_actions() -> list[ActionProposal]:
    return [
        ActionProposal(
            action_type="restock",
            target="P001",
            summary="立即补货 P001 超薄折叠桌面收纳架",
            rationale="P001 本周缺货 8 天，直接压低了整体转化率，是本期最大失血点。",
            expected_impact="预计恢复转化率约 0.4 个百分点",
            risk="若备货过多会形成库存积压，建议按 2 周销量备货",
            priority="P0",
            needs_approval=True,
        ),
        ActionProposal(
            action_type="price_change",
            target="P002",
            summary="评估 P002 涨价 18% 后的价格弹性，考虑回调",
            rationale="P002 涨价后转化率下滑幅度明显超过价格弹性预期。",
            expected_impact="回调 5% 预计可恢复部分转化",
            risk="回调会直接降低单件毛利，需按毛利总额而非转化率判断",
            priority="P1",
            needs_approval=True,          # 涉及价格，必须为 True
        ),
    ]


def install_fakes(insight_factory, critique_factory, record_calls: list):
    """把流水线里的 LLM 环节替换成假实现。"""
    def fake_analyst_single(prompt: str):
        record_calls.append(("analyst", prompt))
        return [insight_factory()]

    def fake_planner(period, metrics, quality_text, insights):
        record_calls.append(("planner", period))
        return make_actions()

    wr._run_analyst_single = fake_analyst_single
    wr._run_planner = fake_planner
    wr.critic_review = critique_factory


# ---------------------------------------------------------------------------
# ===========================================================================
# ★ 未捕获异常的诊断钩子
# ===========================================================================
# 背景：CI 上出现过「exit 1 但日志里没有 [FAIL]、也没有结论行」的情况 ——
#      脚本是被未捕获异常中断的，而 traceback 混在长日志里很难发现。
#      这个钩子把异常打成醒目块，直接 grep 即可：
#          grep -A 40 "未捕获异常"
#
# 判定方式：用 sys.exc_info() 而不是自己维护标志位 ——
#      正常退出时它返回 (None, None, None)，只有真的抛异常才有内容。
#      （第一版用标志位判断，结果正常结束也误报，反而制造噪声。）
def _install_crash_reporter(script_path: str) -> None:
    import atexit
    import os
    import sys as _sys
    import traceback as _tb

    def _report() -> None:
        exc_type, exc_value, exc_tb = _sys.exc_info()
        if exc_type is None:
            return                      # 正常退出，不打扰
        print()
        print("!" * 74)
        print(f"!! {os.path.basename(script_path)} 未捕获异常 —— "
              f"脚本被中断，未跑到结论行")
        print("!" * 74)
        _tb.print_exception(exc_type, exc_value, exc_tb)
        print("!" * 74)
        _sys.stdout.flush()

    atexit.register(_report)


_install_crash_reporter(__file__)


section("1. 正常路径：合规结论 → 评审通过")
# ---------------------------------------------------------------------------

good_critique = Critique(
    passed=True, problems=[], must_fix=[],
    verdict="证据充分，污染因素已识别并说明，结论可以放行。",
)
calls: list = []
install_fakes(make_good_insight, lambda **kw: good_critique, calls)

review = wr.run_weekly_review("2025-07-01", "2025-07-07")
print(f"  区间：{review.period}")
print(f"  归因 {len(review.insights)} 条，建议 {len(review.actions)} 条，"
      f"重做 {review.revision_rounds} 轮，评审通过={review.critique.passed}")

check("产出归因", len(review.insights) >= 1)
check("产出建议", len(review.actions) >= 1)
check("评审通过", review.critique.passed)
check("未发生重做", review.revision_rounds == 0, str(review.revision_rounds))
check("质控识别到缺货", any(f["code"] == "STOCKOUT_FOUND" for f in review.quality_flags),
      str([f["code"] for f in review.quality_flags]))
check("指标由指标层填入（有 sample_days）", review.metrics.get("sample_days") == 7,
      str(review.metrics.get("sample_days")))
check("正文含核心指标表", "| 成交额(GMV) |" in review.narrative)
check("正文数字与指标层一致",
      f"{review.metrics['gmv_gross']:,.2f}" in review.narrative,
      "报告里的 GMV 未按指标层数值渲染")
check("正文含评审章节", "## 五、评审意见" in review.narrative)
check("headline 非空", bool(review.headline.strip()))


# ---------------------------------------------------------------------------
section("2. ★ 回环路径：违规结论 → 评审打回 → 重做")
# ---------------------------------------------------------------------------

# 第一轮给违规结论，第二轮给合规结论，验证回环确实生效并修正了结果
state = {"n": 0}


def evolving_insight():
    state["n"] += 1
    return make_bad_insight() if state["n"] == 1 else make_good_insight()


fail_then_pass = [
    Critique(passed=False, problems=["第一轮结论缺少证据"],
             must_fix=["补充 evidence 并修正置信度"],
             verdict="不通过，需补充证据。"),
    Critique(passed=True, problems=[], must_fix=[], verdict="修正后通过。"),
]
crit_state = {"n": 0}


def sequencing_critique(**kw):
    c = fail_then_pass[min(crit_state["n"], 1)]
    crit_state["n"] += 1
    return c


calls2: list = []
install_fakes(evolving_insight, sequencing_critique, calls2)

# 用 max_revisions=1 允许重做一轮
review2 = wr.run_weekly_review("2025-07-01", "2025-07-07", max_revisions=1)
analyst_calls = [c for c in calls2 if c[0] == "analyst"]
print(f"  分析 Agent 被调用 {len(analyst_calls)} 次（首轮 + 重做）")
print(f"  重做轮次：{review2.revision_rounds}")
print(f"  最终结论：{review2.insights[0].description[:50]}...")

check("回环确实触发（分析被调用 2 次）", len(analyst_calls) == 2, str(len(analyst_calls)))
check("重做次数被记录", review2.revision_rounds == 1, str(review2.revision_rounds))
check("重做后结论已修正为合规版本",
      "缺货" in review2.insights[0].description,
      review2.insights[0].description[:60])

# 重做时是否把问题回灌进 prompt？
if len(analyst_calls) == 2:
    second_prompt = analyst_calls[1][1]
    check("重做请求中回灌了上一轮问题",
          "未通过校验" in second_prompt or "上一轮" in second_prompt,
          "第二轮 prompt 未包含反馈")


# ---------------------------------------------------------------------------
section("3. 确定性规则：能否抓住违规结论（不依赖 LLM 评审）")
# ---------------------------------------------------------------------------

from app.quality.rules import (      # noqa: E402
    check_action_safety, check_confounder_acknowledgement, check_insight_text,
)

bad = [make_bad_insight()]
probs = check_insight_text(bad, [{"code": "STOCKOUT_FOUND", "level": "error"}], 7, 7)
print("  对违规 Insight 的判定：")
for p in probs:
    print(f"    - {p[:78]}")
check("抓到「缺少 evidence」", any("evidence" in p for p in probs))
check("抓到「受污染却 high 置信度」", any("置信度" in p for p in probs))
check("抓到「受污染却未写因素」", any("confounders" in p for p in probs))
check("抓到「blocking 下的因果结论」", any("因果" in p for p in probs))

probs2 = check_confounder_acknowledgement(bad, [{"code": "STOCKOUT_FOUND"}])
print("  对「忽略缺货」的判定：")
for p in probs2:
    print(f"    - {p[:78]}")
check("抓到「忽略质控报告的缺货」", any("STOCKOUT_FOUND" in p for p in probs2))

# 行动建议安全校验
unsafe = [ActionProposal(
    action_type="price_change", target="P002", summary="直接降价 20%",
    rationale="提升转化率", expected_impact="转化提升",
    risk="毛利下降", needs_approval=False)]      # 违规：改价却不需要审批
probs3 = check_action_safety(unsafe)
print("  对「改价不需审批」的判定：")
for p in probs3:
    print(f"    - {p[:78]}")
check("抓到「改价未标审批」", any("needs_approval" in p for p in probs3))

# 合规建议不应被误判
safe = make_actions()
probs4 = check_action_safety(safe)
print(f"  合规建议的判定：{probs4 if probs4 else '无问题（正确）'}")
check("合规建议不被误判", not probs4, str(probs4))


# ---------------------------------------------------------------------------
section("4. 受限路径：有 blocking 时不做因果归因")
# ---------------------------------------------------------------------------

# 缺货周必然有 blocking
review3 = wr.run_weekly_review("2025-07-05", "2025-07-11", max_revisions=0)
blocking = [f for f in review3.quality_flags if f["level"] == "error"]
print(f"  区间 2025-07-05~07-11 的 blocking 项：{[f['code'] for f in blocking]}")
check("缺货周产生 blocking", len(blocking) > 0, str(review3.quality_flags))

# 干净周不应有 blocking
review4 = wr.run_weekly_review("2025-06-02", "2025-06-08", max_revisions=0)
blocking4 = [f for f in review4.quality_flags if f["level"] == "error"]
print(f"  干净周 2025-06-02~06-08 的 blocking 项：{[f['code'] for f in blocking4]}")
check("干净周无 blocking", not blocking4, str(blocking4))


# ---------------------------------------------------------------------------
section("5. LangGraph 版编排：图能构建且节点齐全")
# ---------------------------------------------------------------------------

try:
    import app.orchestrator as orch
    graph = orch.build_review_graph()
    nodes = set(graph.get_graph().nodes.keys())
    print(f"  图中节点：{sorted(nodes)}")
    for n in ["prepare", "limited", "analyst", "planner", "critic", "render"]:
        check(f"节点 {n} 存在", n in nodes)
    check("条件边函数可用（有 blocking → limited）",
          orch.route_after_prepare({"has_blocking": True}) == "limited")
    check("条件边函数可用（无 blocking → analyst）",
          orch.route_after_prepare({"has_blocking": False}) == "analyst")
    check("回环判定：未通过 → 回到 analyst",
          orch.route_after_critic({
              "critique": Critique(passed=False, verdict="x"),
              "revisions": 1, "max_revisions": 2}) == "analyst")
    check("回环判定：超出轮次上限 → 终止",
          orch.route_after_critic({
              "critique": Critique(passed=False, verdict="x"),
              "revisions": 3, "max_revisions": 1}) == "render")
    check("回环判定：通过 → 终止",
          orch.route_after_critic({
              "critique": Critique(passed=True, verdict="ok"),
              "revisions": 0, "max_revisions": 1}) == "render")
except Exception as e:
    check("LangGraph 编排可用", False, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
print()
print("=" * 72)
print(f"流水线验收：{PASS} 项通过，{FAIL} 项失败")
print("=" * 72)
if FAIL == 0:
    print()
    print("✅ 编排骨架（含回环、受限路径、确定性护栏）全部通过。")
    print("   这些结论不依赖真实 LLM —— 换成真模型后流程行为一致。")
else:
    print()
    print(f"❌ 有 {FAIL} 项失败。")

sys.exit(1 if FAIL else 0)
