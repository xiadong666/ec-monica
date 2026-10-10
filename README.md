# EC-Monica 电商产运 Agent

面向产品、运营、研发的电商业务 Agent。支持**数据分析与报告生成、业务归因、需求文档解析**，
目标是把单次分析报告的产出从人工 1~2 小时压缩到 5~15 分钟，
并通过注入业务范文让输出风格与内部模板对齐。

**当前状态：企业级**（v2.0）。原型版（v1.0）缺少的认证鉴权、限流配额、
会话外置、结构化日志、审计留痕、容器化与 CI 已全部补齐。
验收：**411 项自动化检查**，五套脚本，退出码全 0。

---

## 零、企业级能力总览

| 能力 | 实现 | 验收 |
| --- | --- | --- |
| **认证** | JWT（HS256）+ PBKDF2 密码哈希（20 万轮） | 12 个接口未登录全部 401 |
| **授权** | 角色权限矩阵（admin/analyst/viewer），**默认拒绝** | 10 项权限断言 |
| **数据权限** | 类目/渠道白名单，**下沉到数据层**而非接口层过滤 | 受限用户只见 5/12 个商品 |
| **限流与配额** | 滑动窗口速率限制 + 按角色每日 LLM 配额 | 第 61 次请求触发 429 |
| **会话外置** | 可插拔存储（内存 / Redis），多副本共享 | TTL + LRU 淘汰 |
| **结构化日志** | JSON 行 + request_id 全链路追踪 | 可进 ELK/Loki |
| **审计留痕** | 独立于日志的追加型记录，含 user/role/scope | 8 项审计断言 |
| **并行工具执行** | **自适应**并行（快工具串行、慢工具并行） | 慢工具 3.93x 加速 |
| **容器化** | 多阶段构建 + 非 root 运行 + 健康检查 | CI 中构建并验证 |
| **CI** | 5 套验收接入流水线 + 镜像构建验证 | 防止能力退化 |

---

## 一、核心架构

```
用户请求
   │
   ├─ ⓪ 认证鉴权 ── JWT 校验 → 角色授权 → 限流配额 → 数据权限范围
   │      ★ 在业务逻辑之前，默认拒绝
   │
   ├─ ① 意图识别 ──── 规则优先（命中率 ~100%），LLM 仅兜底
   │
   ├─ ② 条件路由 ─┬─ 无需工具 → 【直接生成】一次调用 → 输出
   │              └─ 需要工具 → ③
   │
   ├─ ③ 数据预处理：先算好同比/环比 + 运行质控 → 注入 Context
   │      ★ 在循环之前做，不依赖模型自觉调用
   │
   ├─ ④ 工具循环 ─ 选工具 →（★自适应并行 + 超时重试错误分类）执行
   │              → 预处理算环比 → 注入 Context
   │              → LLM 出子结论 → 滚动 → 循环（上限保护）
   │
   ├─ ⑤ 汇总生成 ─ 压缩后的全部前轮结论 + 业务范文 → 最终输出
   │
   ├─ ⑥ 评审 ──── 确定性规则 + LLM 语义 →（不通过）回到 ⑤
   │
   └─ ⑦ 降级 ──── 工具全挂时，标注「未经数据验证」后直接生成

全程：结构化日志 + 审计留痕 + request_id 追踪
```


### 六个关键设计决策

| 决策 | 为什么这么做 |
| --- | --- |
| **计算归代码，解读归模型** | 同比/环比由 `workflow/preprocess.py` 算好再注入。让 LLM 自己算，它会算错而且错得看不出来（输出格式仍然正确）。**同理，相对时间也由代码解析成绝对日期**（见 `build_time_block`），模型算日期同样不可靠。 |
| **规则优先，LLM 兜底** | 意图识别能靠规则判定的绝不调模型 —— 省一次调用、省几秒延迟，且结果稳定可复现。 |
| **质控在循环之前跑** | 数据质控若依赖模型"记得调用"，它偶尔会漏，而漏一次结论就错了。所以由代码无条件运行。 |
| **单向硬约束** | 含数据信号的请求**绝不允许**免工具作答（详见下文）。两个方向的错误代价不对称：误判为免工具会让模型凭记忆编数字且无警示，误判为需工具只是多查一次数据。 |
| **显式循环而非现成 Agent** | 规格要求"预处理后注入 Context"，`create_agent` 的内部循环插不进这一步。多写几十行换取数据流完全可控。 |
| **容错是一等公民** | 工具失败返回结构化错误而非抛异常，让模型能自我修正；全挂时降级输出，而不是 500。 |

### 单向硬约束（路由层的纵深防御）

`direct_generate` 是唯一"不查数据直接作答"的路径，因此也是最危险的路径。

```
含数据信号（为什么/归因/分析/下滑/指标/数据/报告…） → 绝不允许判为 direct_generate
不含数据信号                                       → 才允许
```

实现分三层，**入口宽松、出口必须严格**：

1. `classify_by_rules` 内部对写作类判定做前置检查；
2. LLM 兜底提示词写明"不确定时选择需要工具的类别"；
3. `enforce_data_signal()` 作为**唯一出口**做后置拦截 ——
   以后无论新增哪条规则、换成哪个模型，这条底线都不会被突破。

> ⚠️ 但**不能反过来**把"分析/归因"强行绑定到 attribution 意图。
> 那样「分析一下这个需求文档」会被推去跑数据指标工具 —— 方向完全错了，而且更贵。
> 这些词只声明"必须有数据支撑"，具体归到哪一类仍由完整规则表判定。

### 时间处理：两个必须由代码解决的问题

**① 相对时间必须预先解析。** 只注入一句"当前日期是 X"不够 ——
模型仍要做日期心算（今天 8/25，上周一是 8/18……），而模型算日期和算数字一样不可靠。
所以 `build_time_block()` 把「上周 / 上上周 / 最近 7 天 / 本月」全部算好再注入。

**② 时间错位必须强制声明。** 这是最隐蔽的陷阱：

```
今天              = 2026-10-09
用户问的"上周"      = 2026-09-28 ~ 2026-10-04
数据库实际范围      = 2025-06-01 ~ 2025-08-29（一年前）
系统实际回答的区间   = 2025-08-18 ~ 2025-08-24   ← 相差 406 天
```

报告标题会写成「上周转化率归因（2025-08-18 ~ 2025-08-24）」，
**格式完全正常、内容详实，用户根本看不出答案对应的是一年前的行情**。
这比返回空结果危险得多 —— 空结果一眼能看出问题，错位报告会被直接采信。

所以 `detect_time_mismatch()` 检测错位后注入最高优先级警告，
并在 `_review()` 里用硬规则校验模型**有没有在开头声明** —— 没声明就打回重做。

---

## 二、目录结构

```
ec-monica/
├── app/
│   ├── main.py                    # FastAPI 接口层
│   ├── config.py  llm.py  schemas.py
│   ├── db/                        # 数据层：建表、只读 SQL 护栏
│   │   ├── schema.py  database.py
│   ├── metrics/                   # 指标层：口径字典 + 纯函数计算
│   │   ├── definitions.py  compute.py
│   ├── quality/                   # 质控层：防归因污染
│   │   ├── checks.py              #   缺货/改价/大促/流量结构/边际递减检测
│   │   └── rules.py               #   确定性规则校验（能代码判的绝不交给 LLM）
│   ├── tools/
│   │   ├── registry.py            # ★ 动态 Tool Registry + 容错执行
│   │   └── builtin.py             #   12 个内置工具的注册
│   ├── context/
│   │   ├── templates.py           # 业务范文模板 + 业务规则 + 角色设定
│   │   ├── compressor.py          # 上下文压缩（近详远略）
│   │   └── builder.py             # Context 组装
│   ├── workflow/
│   │   ├── intent.py              # ★ 意图识别与条件路由
│   │   ├── preprocess.py          # ★ 数据预处理：算同比/环比
│   │   ├── loop.py                # ★ 工具循环执行器
│   │   ├── engine.py              # ★ 主引擎：串起全流程
│   │   └── requirement.py         # 需求文档解析链
│   ├── agents/                    # LangChain create_agent 对照实现
│   └── services/weekly_review.py  # 周复盘（结构化报告）
├── scripts/
│   ├── seed_data.py               # 生成模拟电商数据（含 4 类归因陷阱）
│   ├── verify_foundation.py       # 验收：数据层 + 指标层 + 质控层
│   ├── verify_pipeline.py         # 验收：编排骨架（含回环与降级）
│   ├── verify_architecture.py     # 验收：Registry/容错/意图/上下文
│   ├── verify_workflow.py         # 验收：工作流端到端（+ --live 真实模式）
│   └── run_agent.py               # 命令行入口
└── tests/eval/                    # 评估集（黄金测试）
    ├── golden_set.json            #   取数/归因 14 例
    └── golden_set_requirement.json#   需求解析 4 例
```

---

## 三、快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 生成模拟数据（不需要 API Key）

```bash
python scripts/seed_data.py
```

生成 90 天数据（2025-06-01 ~ 2025-08-29），**故意埋入 4 类归因陷阱**：

| 陷阱 | 内容 | 考察点 |
| --- | --- | --- |
| 618 大促 | 06-16~06-20 流量 5 倍、投放费暴涨 | Agent 会不会把大促峰值当成"策略有效" |
| 爆款缺货 | P001 于 07-05~07-12 缺货 8 天 | Agent 会不会把缺货归因为"详情页问题" |
| 悄悄涨价 | P002 自 07-14 起 +18% | Agent 能否识别价格弹性而非产品力下降 |
| 投放边际递减 | 08-01 起预算 +30%，ROI 从 16 掉到 12 | Agent 会不会建议"关掉渠道" |

### 3. 跑验收（强烈建议先跑，不需要 API Key）

```bash
python scripts/verify_foundation.py     # 43 项：数据/指标/质控
python scripts/verify_pipeline.py       # 34 项：编排骨架
python scripts/verify_architecture.py   # 184 项：Registry/容错/意图/上下文/硬约束/时间/SQL
python scripts/verify_workflow.py       # 55 项：工作流端到端（含回调与降级）
python scripts/verify_enterprise.py     # 95 项：认证/授权/数据权限/限流/会话/审计/并行
python scripts/verify_workflow.py --live # 用真实 LLM 跑端到端（需 API Key）
```

共 **411 项**检查。四个陷阱都会被自动检出。**先确认确定层全绿，再接 LLM** ——
否则指标或质控的错误会被模型放大成"看起来很专业但完全错误"的建议。

> ⚠️ 但要注意：**离线全绿不等于能跑通**。本项目实际踩到的最严重的三个问题
> （结构化输出被 DeepSeek 拒绝、回调签名冲突、工具参数名不匹配）
> 全都是集成层问题，只有真实端到端调用才会暴露。
> 所以改了流程后请务必跑一次 `--live` 或 CLI。

### 4. 配置环境变量

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

**必须配置的三项**（否则启动时会有明确警告）：

```bash
# ① LLM 密钥（必须支持 Function Calling，R1 不行）
DEEPSEEK_API_KEY=sk-...

# ② JWT 签名密钥（生成：python -c "import secrets; print(secrets.token_hex(32))"）
EC_JWT_SECRET=<32 字节随机串>

# ③ 用户（留空则启用开发默认账号，生产必须配置）
# 生成密码哈希：python -c "from app.auth import hash_password; print(hash_password('你的密码'))"
EC_USERS={"alice":{"password_hash":"pbkdf2_sha256$200000$...","role":"admin"},
          "bob":{"password_hash":"pbkdf2_sha256$200000$...","role":"analyst",
                 "categories":["美妆","家居"],"daily_llm_quota":100}}
```

> ⚠️ `.env.example` 是**模板**，必须保持为空值 —— 它会被提交到版本库。
> 真实密钥只放 `.env`（已在 `.gitignore` / `.dockerignore` 中排除）。

### 5. 启动服务

```bash
# 方式一：本地运行
uvicorn app.main:app --reload

# 方式二：容器（含 Redis，多副本就绪）
docker compose up -d --build
docker compose logs -f api
```

- 交互式文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/health（**不需要认证**，供探针使用）

### 6. 登录并调用

```bash
# 获取令牌
curl -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# 用令牌调用（所有业务接口都需要）
curl http://127.0.0.1:8000/api/metrics \
  -H "Authorization: Bearer <access_token>"
```

响应头会返回配额信息：`X-Quota-Remaining`、`X-Quota-Limit`、`X-Request-ID`。

---

## 四、API 接口

### 接口分四层

| 层 | 接口 | 认证 | 说明 |
| --- | --- | --- | --- |
| **公共** | `/api/health` | ❌ 免认证 | 健康探针专用；只暴露布尔/计数，不泄露配置 |
| **认证** | `/api/auth/login` `/api/auth/me` | 部分 | 登录、查用户与配额 |
| **确定性** | `/api/metrics` `/api/quality` `/api/products` `/api/channels` `/api/schema` | ✅ | 不消耗 LLM；只读角色也能用 |
| **工作流** | `/api/chat` `/api/requirement` `/api/weekly-review` | ✅ | 消耗 LLM 配额 |
| **可观测** | `/api/tools` `/api/tools/stats` `/api/audit/stats` | ✅ | 审计状态仅管理员 |

### 角色权限矩阵

| 角色 | 确定性接口 | SQL 接口 | 工作流 | 工具统计 | 审计状态 | 日 LLM 配额 |
| --- | --- | --- | --- | --- | --- | --- |
| `admin` | ✅ | ✅ | ✅ | ✅ | ✅ | 1000 |
| `analyst` | ✅ | ✅ | ✅ | ✅ | ❌ | 200 |
| `viewer` | ✅ | ❌ | ❌ | ❌ | ❌ | 0 |

> **默认拒绝**：权限表中未配置的路径只有管理员能访问。
> 新加接口忘了配权限时，结果是"访问不了"（可发现），而不是"谁都能访问"（静默漏洞）。

### 接口清单

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（免认证） |
| POST | `/api/auth/login` | 登录获取 JWT |
| GET | `/api/auth/me` | 当前用户与配额 |
| GET | `/api/schema` | 数据表结构（与注入 prompt 的内容一致） |
| GET | `/api/metrics` | 核心指标 + 环比（默认最后一个完整自然周） |
| GET | `/api/quality` | **数据质控**：这周数据能不能用来下结论 |
| GET | `/api/products` | 商品下钻（含缺货天数、实际售价，**受数据权限限制**） |
| GET | `/api/channels` | 渠道下钻（含投放 ROI） |
| POST | `/api/sql` | 只读 SQL（四道护栏） |
| GET | `/api/tools` | 已注册工具及能力标签 |
| GET | `/api/tools/stats` | 工具调用统计（调用/失败/重试/耗时） |
| GET | `/api/audit/stats` | 审计与安全状态（**仅管理员**） |
| POST | `/api/chat` | **统一入口**，自动意图识别与条件路由 |
| POST | `/api/requirement` | 需求文档解析（结构化输出） |
| POST | `/api/weekly-review` | 周复盘报告（结构化） |

`/api/quality` 是其中最实用的一个：**不接 LLM 也能告诉你数据有没有问题。**

### 调用示例

```bash
# 1) 登录
TOKEN=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}' | jq -r .access_token)

# 2) 数据质控
curl "http://127.0.0.1:8000/api/quality?start=2025-07-05&end=2025-07-11" \
  -H "Authorization: Bearer $TOKEN"

# 3) 统一入口：自动路由到"归因分析"
curl -X POST http://127.0.0.1:8000/api/chat \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message":"上周转化率为什么下滑？","session_id":"u1"}'

# 4) 需求解析
curl -X POST http://127.0.0.1:8000/api/requirement \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"text":"订单状态要加一个部分退款状态，退款金额小于订单金额时即为部分退款"}'
```

`/api/chat` 的响应含 `meta` 字段，记录完整执行过程：

```json
{
  "intent": "attribution", "intent_method": "rule", "need_tools": true,
  "capabilities": ["schema","metric","drilldown","quality","sql"],
  "route": "tool_loop",
  "tool_calls": ["get_data_range","run_quality_checks","compare_weeks"],
  "rounds": 3, "tool_errors": [], "degraded": false,
  "elapsed_ms": 18432.1, "context_tokens": 4120,
  "critique_passed": true, "revision_rounds": 0, "warnings": []
}
```

---

## 五、命令行

```bash
# 自动意图路由
python scripts/run_agent.py "上周的 GMV 是多少"
python scripts/run_agent.py "转化率为什么下滑了"
python scripts/run_agent.py "拆解这个需求：加一个加购转化率指标"

# 需求解析（结构化）
python scripts/run_agent.py --requirement "订单状态要加一个部分退款状态"

# 查看已注册工具 / 意图路由表
python scripts/run_agent.py --tools
python scripts/run_agent.py --routes

# 只跑确定性部分（不需要 API Key，用于快速确认数据侧正常）
python scripts/run_agent.py --dry-run "上周GMV多少"

# 真实 LLM 端到端验收
python scripts/verify_workflow.py --live
```

---

## 六、扩展一个新工具（成本 ≈ 写一个函数）

```python
# app/tools/builtin.py

@registry.register(
    capabilities=["drilldown"],          # 能力标签决定它会被哪种意图装配
    description="查询某个用户分层的复购率。分层取值：新客/活跃/沉默/流失",
    timeout_s=15, max_retries=2,
)
def get_repurchase_rate(segment: str, days: int = 30) -> dict:
    return {"segment": segment, "rate": ...}
```

Function Schema 由函数签名**自动生成**，无需手写 JSON Schema。
工具会自动获得超时保护、按错误类型重试、结构化错误回灌。

**扩展一类新任务**（如"库存预警报告"）：新增一个模板 + 一组能力标签 + 一个输出 Schema，
复用 `workflow/` 的骨架即可，不需要改引擎。

---

## 七、护栏清单

1. **数字只来自工具**：LLM 不许自算，同比/环比由代码算好注入。
2. **四道 SQL 护栏**：表名白名单 + 强制 SELECT + 强制 LIMIT + 只读连接
   （最后一层在数据库驱动级，即使绕过前三层也无法写入）。
3. **归因纪律**：转化率下滑必须依次排查缺货 → 改价 → 流量结构 → 大促 → 商品自身。
   "详情页问题"是最后一项候选。
4. **评审双层**：能由代码判定的绝不交给 LLM（正则 + 字段校验零漏判、零成本）。
5. **审批纪律**：涉及改价/投放预算的建议一律标注"需人工审批"。
6. **不确定纪律**：样本不足（< 7 天）时禁止使用"趋势"表述，置信度必须降级。
7. **配置错误快速失败**：未配 Key 返回 503 明确指引，不伪装成"降级成功"。

---

## 八、已修复的典型 bug（踩坑记录）

这些是开发过程中被验收脚本抓出来的真实缺陷，记录下来避免重犯：

| 问题 | 根因 | 修法 |
| --- | --- | --- |
| **模型写 JOIN 反复报 no such column** | 模型凭通用电商系统命名直觉编列名：`p.product_name`/`p.name`/`date`，而本项目真实列名是 `title`/`stat_date`；且 `dim_product` 根本没有日期列 | ① `describe_tables` 输出增加「表关系与命名约定」；② `friendly_sql_error()` 把裸报错翻译成带**正确列名建议**的提示；③ 注册 `get_cross_breakdown` 交叉维度工具，把最绕的三表关联固化进代码 |
| **交叉维度问题被迫绕路 5 轮** | `analysis_report` 未装配 `sql`，模型无法写 JOIN，只能用单维度工具拼凑，重复调用 `describe_tables` 4 次 | `analysis_report` 补上 `sql` 能力；新增交叉维度工具后降到 **2 轮 / 3 次调用 / 0 错误** |
| **重复调用元信息工具浪费轮次** | `describe_tables` 是幂等的，但模型会反复调用（实测 6 轮里 5 次） | 第二次起返回"已提供过，请直接写 SQL"的硬提示；每次运行开始时重置计数 |
| **交叉维度的笛卡尔积放大** | 两张事实表只能按 `stat_date` 关联，直接 JOIN 使 GMV 放大 N 倍且各类目在各渠道数值相同 | 改为「类目结构 × 渠道结构」重构，各渠道合计与真实值完全一致；并在返回值里显式标注「结构重构（非直接联表）」与"交叉格为估算值，不可用于汇报" |
| **压缩把 35 格砍成 8 格** | 固定 `max_items=8`，模型只看到局部，报告"工具只返回前 8 项"——数据其实拿到了，是被我们自己砍的 | 改为按规模自适应：≤12 项全量保留，大列表保留 20 项并注明省略数 |
| **把 406 天前的数据当"上周"汇报** | 系统日期 2026-10-09，数据止于 2025-08-29。系统返回"数据中最后一个完整周"，报告标题看起来完全正常，用户看不出错位 | `detect_time_mismatch()` 检测错位 → 注入最高优先级警告 → `_review()` 硬校验有没有在开头声明，没声明就打回 |
| **所有结构化输出全部失效** | LangChain 的 `with_structured_output` 默认发 `response_format: json_schema`，**DeepSeek 不支持该类型**，返回 400。基础对话却完全正常，极具迷惑性 | 新增 `llm.structured_llm()`，显式用 `method="function_calling"`，并做三级回退 + 缓存探测结果 |
| **CLI 一跑就崩（`TypeError: missing 1 required positional argument`）** | 引擎里写 `on_progress(msg)`（1 参数），循环里写 `on_progress(round, msg)`（2 参数），回调签名没有契约 | 抽出 `safe_progress()` 自适应两种签名，且回调异常不影响主流程 |
| **工具因参数名报错，白跑一整轮** | `Registry.execute` 把 kwargs 直接透传给函数，绕过了自动生成的 schema；模型传 `start_date` 而签名是 `start` | 执行前加 schema 校验 + 别名映射（静默纠正），schema 描述里直接写出正确参数名 |
| **`def f(**kw)` 型工具必报字段缺失** | schema 生成器把 `**kwargs` 当成了名为 `kw` 的必填字段 | 跳过 `VAR_KEYWORD` / `VAR_POSITIONAL` 可变参数 |
| **"看看这个"被误判为免工具作答** | 规则未命中 → 走 LLM 兜底 → 模型判为 direct_generate | 单向硬约束：`enforce_data_signal()` 在出口强制拦截 |
| **阻断纪律评审过严** | 把 warning 级问题（投放边际递减）也纳入强制提及，导致合格报告被判不通过 | 只有 error 级才有强制提及的效力；warning 不阻断 |
| 改价检测漏报且 8 个商品误报 | `GMV/件数` 会被"一次买几件"的件数结构干扰，不是价格信号 | 事实表增加 `unit_price` 价格快照列，改用变点检测 |
| 变点检测算不出涨幅 | 涨价发生在窗口中间时，中位数漂移到涨价后，baseline == current | 枚举切分点取差异最大者，而非与全期中位数比 |
| 投放边际递减漏报 | ① 618 花费污染了趋势基线 ② 只比最后一个分桶 | 趋势计算剔除特殊日期；改用水平位移 + 斜率双判据 |
| **工具连续失败 6 轮，降级从未触发** | 失败记录被塞进 `new_data`，导致 `if not new_data` 恒为假 | 成功与失败数据分开统计 |
| 压缩率 108%（越压越大） | 每轮无条件加的头比省掉的内容还长 | 加净收益检查；但轮次编号必须保留（审计追溯） |
| 规则识别"想个标题"失败 | 正则没给中文动词与宾语之间留定语间隙 | 动作词与目标词之间允许 `.{0,8}` |
| 需求文档被判为数据分析 | 硬约束优先于需求类判定，把「分析一下这个需求文档」推去跑指标工具 | 需求类判定前移，硬约束只做单向兜底 |
| 无 Key 时返回 500 而非 503 | 兜底逻辑把 `LLMNotConfigured` 一并吞掉 | 配置错误向上抛，快速失败 |
| `NameError: name 'e' is not defined` | `except ... as e` 的 `e` 离开子句后被清除 | 改用不同变量名 |

**共同教训：确定性骨架的错误必须靠验收脚本抓，靠肉眼看代码是看不出来的。**
而更贵的教训是 —— **只有真实端到端调用才会暴露集成层的问题**
（结构化输出、回调签名、参数名、时间错位），离线验收全绿不代表能跑通。

---

## 九、注意事项

1. **模型**：必须用支持 Function Calling 的模型（`deepseek-chat` / V3）。
   ⚠️ 并且**结构化输出必须走 `method="function_calling"`** ——
   DeepSeek 不支持 `response_format: json_schema`（LangChain 的默认模式），
   直接调用会返回 400 `This response_format type is unavailable now`。
   本项目已封装 `app/llm.py` 的 `structured_llm()` 处理这一点，新增代码请复用它，
   不要直接写 `build_llm().with_structured_output(...)`。
2. **API Key**：只放 `.env`，不要提交 Git。
   Key 缺失时工作流接口返回 503 并给出指引，确定性接口不受影响。
3. **版本**：`langchain==1.2.18` 需配 `langgraph>=1.1.10`，
   否则导入 `create_agent` 会报 `ExecutionInfo` 缺失。
4. **网络**：工作流接口会多次调用 LLM，单次耗时 30~90 秒属正常。
5. **数据**：`data/ec.db` 是生成的模拟数据，`python scripts/seed_data.py` 可重置。
6. **会话历史**：`/api/chat` 的 `session_id` 用于携带前轮结论，
   目前存在进程内存中，重启即失效；生产环境应改为 Redis。
