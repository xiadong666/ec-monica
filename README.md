# EC-Monica 电商产运 Agent

[![CI](https://github.com/xiadong666/ec-monica/actions/workflows/ci.yml/badge.svg)](https://github.com/xiadong666/ec-monica/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)
![checks](https://img.shields.io/badge/验收-411%20项-brightgreen)
![accuracy](https://img.shields.io/badge/评测准确率-100%25-brightgreen)

面向产品、运营、研发的电商业务 Agent。覆盖**数据分析与报告生成、业务归因、需求文档解析**三类任务，
把单次分析报告的产出从人工 **1~2 小时压缩到 5~15 分钟**。

> **核心原则：计算归代码，解读归模型。**
> 所有同比/环比、相对时间、交叉聚合都由代码算好再注入上下文 ——
> 模型只负责决定"看什么"和"如何解读"，不负责算数。
> 这样即使模型出错，也不会污染数字本身。

---

## 目录

- [30 秒了解这个项目](#30-秒了解这个项目)
- [为什么需要它：一个具体例子](#为什么需要它一个具体例子)
- [快速开始](#快速开始)
- [核心架构](#核心架构)
- [六个关键设计决策](#六个关键设计决策)
- [企业级能力](#企业级能力)
- [双数据库后端](#双数据库后端)
- [评测与质量保障](#评测与质量保障)
- [API 接口](#api-接口)
- [命令行](#命令行)
- [扩展一个新工具](#扩展一个新工具)
- [护栏清单](#护栏清单)
- [踩坑记录](#踩坑记录)
- [已知不足](#已知不足)

---

## 30 秒了解这个项目

| 维度 | 内容 |
| --- | --- |
| **是什么** | 四类意图自动路由的电商业务 Agent：经营分析报告 / 业务归因 / 需求解析 / 直接生成 |
| **最核心的能力** | **防归因污染** —— 下结论前先用代码排除缺货、改价、大促、流量结构等干扰因素 |
| **技术栈** | Python 3.11 · LangChain 1.2 · FastAPI · Pydantic v2 · SQLite / PostgreSQL · Docker |
| **验证方式** | 411 项分层验收脚本 + 46 项评测断言（准确率 100%），全部可复现 |
| **不需要 Key 也能跑** | 确定性部分（指标、质控、路由、容错）完全不依赖 LLM |

```bash
git clone https://github.com/xiadong666/ec-monica.git && cd ec-monica
pip install -r requirements.txt
python scripts/seed_data.py     # 生成含 4 类归因陷阱的模拟数据
python scripts/demo.py          # 跑一遍演示（不需要 API Key）
```

---

## 为什么需要它：一个具体例子

这是本项目最有说服力的部分。假设运营问：**"上周转化率为什么跌了 20%？"**

```
天真 Agent 的回答：
    详情页转化不行，建议优化主图和卖点。

真实原因可能是（四选一，应对动作完全不同）：
    ① 爆款缺货        → 应立刻补货，优化详情页毫无用处
    ② 悄悄涨过价      → 应评估价格弹性，不是改页面
    ③ 大促流量退了    → 属于正常回落，不需要动作
    ④ 付费流量占比升高 → 应优化投放结构
```

**四种情况的应对动作完全相反。** 猜错的代价不是"回答不准确"，而是**团队去优化没坏的东西**。

所以本项目把「先排除干扰因素」做成代码层的硬约束，而不是指望模型自觉：

```
质控层（6 类检测，在工具循环之前无条件运行）
    ├─ 缺货检测      → error 级：阻断因果归因
    ├─ 改价检测      → 变点检测，识别悄悄涨价
    ├─ 大促污染      → 特殊日期剔除
    ├─ 流量结构变化  → 各渠道占比位移
    ├─ 样本量不足    → < 7 天禁止用"趋势"表述
    └─ 投放边际递减  → 水平位移 + 斜率双判据

检出阻断级问题时，Agent 必须输出：
    ① 事实陈述（只描述数据，不解释原因）
    ② 待核查清单（要核查什么 + 用什么数据 + 由谁确认）
    —— 而不是断言"是详情页的问题"
```

📄 [查看完整演示输出](docs/demo_output.txt)

---

## 快速开始

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
| 618 大促 | 06-16~06-20 流量 5 倍、投放费暴涨 | 会不会把大促峰值当成"策略有效" |
| 爆款缺货 | P001 于 07-05~07-12 缺货 8 天 | 会不会把缺货归因为"详情页问题" |
| 悄悄涨价 | P002 自 07-14 起 +18% | 能否识别价格弹性而非产品力下降 |
| 投放边际递减 | 08-01 起预算 +30%，ROI 从 16.6 掉到 12.8 | 会不会建议"关掉渠道" |

> 演示要能测出"会不会被带偏"，数据里就必须先埋好陷阱。
> 干净的数据没有可归因的对象，也测不出模型的倾向性。

### 3. 跑一遍演示（推荐，不需要 API Key）

```bash
python scripts/demo.py                # 7 个部分的终端演示
python scripts/demo.py --no-color     # 便于复制到文本环境
python scripts/demo.py > demo.txt     # 存档
python scripts/demo.py --live         # 额外跑真实 LLM 端到端（需 Key）
```

演示覆盖：数据基座 → 指标层 → **质控层（4 类陷阱检出）** → 意图路由 → 工具层容错 → 评测规模。

> **演示脚本的数字全部来自真实代码，没有一个是写死的。**
> 演示最容易犯的错是把好看的数字硬编码，那样代码一改演示就开始撒谎，
> 而且没人会立刻发现。这里每个指标、每次工具报错都是当场调用 `app/` 下的函数算出来的。

### 4. 跑验收（不需要 API Key）

```bash
python scripts/verify_foundation.py     #  43 项：数据 / 指标 / 质控
python scripts/verify_pipeline.py       #  34 项：编排骨架（假 LLM，验证回环与降级）
python scripts/verify_architecture.py   # 184 项：Registry / 容错 / 意图 / 上下文 / 硬约束 / 时间 / SQL
python scripts/verify_workflow.py       #  55 项：工作流端到端
python scripts/verify_enterprise.py     #  95 项：认证 / 授权 / 限流 / 会话 / 审计
                                        # ─────
                                        # 411 项，退出码全 0

pytest tests/ -v --accuracy-report      # 46 项评测断言 + 准确率报告
pytest tests/ --min-accuracy 0.90       # 准确率门槛（不达标失败，CI 用）
```

**先确认确定性层全绿，再接 LLM** —— 否则指标或质控的错误会被模型放大成
"看起来很专业但完全错误"的建议。

> ⚠️ 但要注意：**离线全绿不等于能跑通**。本项目最严重的三个问题
> （结构化输出被 DeepSeek 拒绝、回调签名冲突、工具参数名不匹配）
> 全都是集成层问题，只有真实端到端调用才会暴露。
> 所以改了流程后请务必跑一次 `--live` 或 CLI。

### 5. 配置环境变量

```bash
copy .env.example .env      # Windows
cp .env.example .env        # macOS / Linux
```

**必须配置的三项**（否则启动时会有明确警告）：

```bash
# ① LLM 密钥（必须支持 Function Calling，deepseek-reasoner / R1 不行）
DEEPSEEK_API_KEY=sk-...

# ② JWT 签名密钥（生成：python -c "import secrets; print(secrets.token_hex(32))"）
EC_JWT_SECRET=<32 字节随机串>

# ③ 用户（留空则启用开发默认账号，生产必须配置）
# 生成密码哈希：python -c "from app.auth import hash_password; print(hash_password('你的密码'))"
EC_USERS={"alice":{"password_hash":"pbkdf2_sha256$200000$...","role":"admin"},
          "bob":{"password_hash":"pbkdf2_sha256$200000$...","role":"analyst",
                 "categories":["美妆","家居"],"daily_llm_quota":100}}

# ④ 数据库（默认 SQLite，无需配置；切换 PostgreSQL 见下文）
EC_DB_BACKEND=sqlite
EC_DB_PATH=data/ec.db
```

> ⚠️ `.env.example` 是**模板**，必须保持为空值 —— 它会被提交到版本库。
> 真实密钥只放 `.env`（已在 `.gitignore` / `.dockerignore` 中排除）。

### 6. 启动服务

```bash
# 方式一：本地运行
uvicorn app.main:app --reload

# 方式二：容器（含 Redis，多副本就绪）
docker compose up -d --build
docker compose logs -f api
```

- 交互式文档：<http://127.0.0.1:8000/docs>（四层接口分组：系统 / 认证 / 经营分析 / 可观测 / 工作流）
- 健康检查：<http://127.0.0.1:8000/api/health>（**免认证**，供探针使用）

### 7. 登录并调用

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

## 核心架构

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
数据层：SQLite / PostgreSQL 可插拔后端
```

### 目录结构

```
ec-monica/
├── app/
│   ├── main.py                    # FastAPI 接口层（四层分组）
│   ├── auth.py                    # ★ JWT + PBKDF2 + 角色矩阵 + 数据权限
│   ├── deps.py                    #   依赖：认证 → 授权 → 限流（顺序不可换）
│   ├── ratelimit.py               #   滑动窗口限流 + 按角色每日 LLM 配额
│   ├── session.py                 #   会话外置（内存 / Redis 可插拔）
│   ├── observability.py           #   结构化日志 + 审计 + request_id
│   ├── db/
│   │   ├── adapter.py             # ★ 双后端适配（方言差异封装）
│   │   ├── schema.py              #   建表 DDL + 表白名单 + DDL 方言转换
│   │   └── database.py            #   只读 SQL 护栏 + JOIN 指南 + 友好错误
│   ├── metrics/                   # 指标层：口径字典 + 纯函数计算
│   ├── quality/                   # 质控层：防归因污染（6 类检测）
│   ├── tools/
│   │   ├── registry.py            # ★ 动态 Tool Registry + 容错执行
│   │   ├── parallel.py            # ★ 自适应并发（快工具串行 / 慢工具并行）
│   │   └── builtin.py             #   13 个内置工具注册
│   ├── context/                   # 模板 + 压缩 + Context 组装
│   ├── workflow/
│   │   ├── intent.py              # ★ 意图识别 + 条件路由 + 单向硬约束
│   │   ├── preprocess.py          # ★ 数据预处理：算同比/环比
│   │   ├── loop.py                # ★ 工具循环执行器
│   │   ├── engine.py              # ★ 主引擎：串起全流程
│   │   └── requirement.py         #   需求文档解析链
│   └── services/weekly_review.py  #   周复盘（结构化报告）
├── scripts/
│   ├── seed_data.py               # 生成模拟数据（含 4 类归因陷阱）
│   ├── demo.py                    # ★ 终端演示（数字全部来自真实代码）
│   ├── migrate_to_postgres.py     # ★ SQLite → PostgreSQL 迁移（幂等 + 三层校验）
│   ├── verify_*.py                # 五套验收脚本（共 411 项）
│   └── run_agent.py               # 命令行入口
├── tests/
│   ├── conftest.py                # pytest 钩子 + 准确率汇总
│   ├── eval_runner.py             # 评测框架（判分 + 加权准确率）
│   ├── test_eval.py               # ★ 两层评测，46 项断言
│   ├── test_db_adapter.py         # ★ 双后端适配验收（无 PG 也能验证）
│   └── eval/                      # 评估集（取数 14 例 + 需求解析 4 例）
├── Dockerfile                     # 多阶段构建 + 非 root + 健康检查
├── docker-compose.yml             # api + redis 编排
├── pytest.ini                     # marker 分层（offline / live / pg）
└── .github/workflows/ci.yml       # 6 个 job
```

---

## 六个关键设计决策

| 决策 | 为什么这么做 |
| --- | --- |
| **计算归代码，解读归模型** | 同比/环比由 `workflow/preprocess.py` 算好再注入。让 LLM 自己算，它会算错而且错得看不出来（输出格式仍然正确）。**同理，相对时间也由代码解析成绝对日期**（见 `build_time_block`），模型算日期同样不可靠。 |
| **规则优先，LLM 兜底** | 意图识别能靠规则判定的绝不调模型 —— 省一次调用、省几秒延迟，且结果稳定可复现。实测规则命中率 100%。 |
| **质控在循环之前跑** | 数据质控若依赖模型"记得调用"，它偶尔会漏，而漏一次结论就错了。所以由代码无条件运行。 |
| **单向硬约束** | 含数据信号的请求**绝不允许**免工具作答（详见下文）。两个方向的错误代价不对称。 |
| **显式循环而非现成 Agent** | 规格要求"预处理后注入 Context"，`create_agent` 的内部循环插不进这一步。多写几十行换取数据流完全可控。 |
| **容错是一等公民** | 工具失败返回结构化错误而非抛异常，让模型能自我修正；全挂时降级输出，而不是 500。 |

### 单向硬约束（路由层的纵深防御）

`direct_generate` 是唯一"不查数据直接作答"的路径，因此也是最危险的路径。

```
含数据信号（为什么/归因/分析/下滑/指标/数据/报告…） → 绝不允许判为 direct_generate
不含数据信号                                       → 才允许
```

**为什么是单向的？两个方向的错误代价不对称：**

- 误判为免工具 → 模型凭记忆编数字，输出格式仍完全正确，**用户看不出问题**
- 误判为需工具 → 只是多查一次数据，**成本可接受**

所以宁可多查，不可编造。实现分三层，**入口宽松、出口必须严格**：

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

### 并行工具执行：为什么不是"无脑并行"

实测（6 个本地 SQLite 工具）：

```
串行实测      : 20.3 ms
并行墙钟      : 27.4 ms      ← 更慢！
单个工具耗时  : 12~20 ms
线程池开销    : 约 20~30 ms
```

**并行墙钟 ≈ max(单个耗时) + 线程池开销，串行墙钟 ≈ sum(单个耗时)。**
本地工具的耗时不敌线程池开销，所以并行是负优化。

因此做成**自适应**：用工具的真实历史耗时预估收益，只有加速比 > 1.15 才启用并行。

| 场景 | 决策 | 实测 |
| --- | --- | --- |
| 快工具（总 19ms） | 串行 | 与纯串行偏差 0.2ms |
| 慢工具（4×150ms） | 并行 | 606ms → 154ms，加速 **3.93x** |

> 这条策略的价值在**未来**：接上真实数仓后单次查询会升到百毫秒级，
> 同一个策略会自动切换到并行，**不需要改代码**。判据用的是真实历史耗时，不靠猜。

并行化必须守住三件事：**返回顺序与请求一致**（否则预处理与日志错位）、
**容错行为完全保留**（走同一份 `_run` 实现）、**不嵌套线程**
（`execute` 内部本就会开线程做超时控制，worker 里再开一层会让线程数翻倍，
所以并行路径用 `execute_inline`）。

---

## 企业级能力

| 能力 | 实现 | 验收 |
| --- | --- | --- |
| **认证** | JWT（HS256）+ PBKDF2 密码哈希（20 万轮）+ 常量时间比较 | 12 个接口未登录全部 401 |
| **授权** | 角色权限矩阵（admin/analyst/viewer），**默认拒绝** | 10 项权限断言 |
| **数据权限** | 类目/渠道白名单，**下沉到数据层**而非接口层过滤 | 受限用户只见 5/12 个商品 |
| **限流与配额** | 滑动窗口速率限制 + 按角色每日 LLM 配额 | 第 61 次请求触发 429 |
| **会话外置** | 可插拔存储（`InMemorySessionStore` / `RedisSessionStore`），多副本共享 | TTL + LRU 淘汰 |
| **结构化日志** | JSON 行 + request_id 全链路追踪 | 可进 ELK / Loki |
| **审计留痕** | 独立于日志的追加型记录，含 user / role / scope | 8 项审计断言 |
| **容器化** | 多阶段构建 + 非 root 运行 + HEALTHCHECK | CI 中构建并验证健康检查 |
| **CI** | 6 个 job：lint / pytest / verify / postgres / e2e-live / docker | 准确率门槛 90%，不达标即失败 |

### 三个容易被忽略的安全细节

**① 数据权限必须下沉到数据层。** 很多系统做了认证和接口授权就以为安全了。
但如果一个只看美妆类目的运营能通过 `/api/products` 看到全部类目数据，权限体系就是纸糊的。
在**接口层过滤容易在某个分支漏掉**，所以过滤发生在数据访问层。

**② 密码比较必须用常量时间函数。** 用 `==` 会在第一个不同字符处提前返回，
攻击者可借响应时间差异逐字符推断（时序攻击），所以用 `hmac.compare_digest`。
同时用户不存在时也做一次假哈希校验，避免通过响应时间判断用户是否存在。

**③ 权限矩阵要默认拒绝。** 实测踩过：配了 `/api/tools` 却漏了 `/api/tools/stats`
导致分析师被误拒（FastAPI 路径匹配是精确的，不是前缀匹配）。
默认拒绝让"忘了配权限"的结果是**访问不了（可发现）**，
而不是**谁都能访问（静默漏洞）**。

---

## 双数据库后端

数据层抽象为可插拔后端，业务代码保持统一写法（位置参数用 `?`）。
没有引入 ORM —— 本项目只用到 SELECT / 建表 / 批量插入这一小撮操作，
引入 SQLAlchemy 等于为很小的子集付出重量级依赖，还会多一层查询语义抽象让排查变难。

**只封装真正有差异的四处：**

| 差异点 | SQLite | PostgreSQL | 不封装的后果 |
| --- | --- | --- | --- |
| 占位符 | `?` | `%s` | SQL 完全无法执行 |
| 只读强制 | 文件 `mode=ro` | 会话变量 `default_transaction_read_only` | 只读护栏失效 |
| 元数据查询 | `PRAGMA table_info` | `information_schema.columns` | Agent 拿不到表结构就无法写 SQL |
| **数值类型** | `REAL` = 8 字节 | **`REAL` = 4 字节**（须转 `DOUBLE PRECISION`） | ★ 金额精度静默丢失 |

> ⚠️ **最危险的是"同名不同类型"。** `REAL` 这个差异不报错、不崩，
> 只让数据悄悄变形：`123012.88` 可能变成 `123012.875`。
> 单条数据上只差几分钱毫不可疑，**只有汇总求和时才暴露**。
> 所以迁移校验必须比对 `SUM(gmv)` 这类金额汇总，只比对行数是发现不了的。

### 切换到 PostgreSQL

```bash
# ① 配置连接串
set EC_DB_BACKEND=postgres
set DATABASE_URL=postgresql://user:pwd@localhost:5432/ecmonica

# ② 迁移（幂等，可重复运行）
python scripts/migrate_to_postgres.py --dry-run     # 干跑：只检查
python scripts/migrate_to_postgres.py               # 正式迁移
python scripts/migrate_to_postgres.py --verify-only # 只做一致性校验
```

迁移脚本的设计要点：**幂等**（先 TRUNCATE，否则重复执行数据翻倍）、
**分批提交**（500 行一批）、**三层校验**（行数 / 列结构 / **金额汇总**）。

> **当前状态**：SQLite 是默认且实际运行的后端；
> PostgreSQL 适配层与 Redis 会话后端已实现，但**尚未在真实环境部署验证**。
> `.github/workflows/ci.yml` 里有一个 `postgres` job 会起真实 PG 服务跑迁移与一致性校验。

---

## 评测与质量保障

### 双层评测：离线层免费，在线层真实

```
离线层（不需要 LLM，秒级、免费、100% 可复现）—— 可进 CI 每次提交都跑
    路由准确率 · 数值准确率 · 质控准确率 · 工具健康度 · 并行策略 · 认证与数据权限
    → 6 个能力层，46 项断言

在线层（需要 LLM，分钟级、有成本、有随机性）
    端到端行为 · 陷阱抵御 · 数字保真度
    → 用 pytest marker 隔离，避免误跑产生费用
```

**关键设计：离线层的准确率本身就是有价值的指标** ——
它衡量的是"确定性骨架"的健康度。改 prompt、换模型、重构指标层时，
离线准确率下降就说明出了问题，而且能立刻定位到是哪一类能力退化。

```bash
pytest tests/ -m offline                    # 只跑离线层（不花钱）
pytest tests/ -m live                       # 在线层（真实 LLM）
pytest tests/ --accuracy-report             # 输出完整准确率报告
pytest tests/ --min-accuracy 0.90           # 门槛，不达标失败
```

### 加权准确率

陷阱题的权重是普通题的 **1.5~3 倍**。为什么？
因为"会不会被归因陷阱带偏"比"能不能取对数"更能区分系统能力。
只看未加权准确率，会让"取数全对但归因全错"的系统看起来还不错。

### CI：防止能力退化

```
lint      语法编译 + 模块导入
pytest    ★ 跑评测，准确率门槛 90%，写入 GitHub Step Summary
verify    五套验收脚本（411 项）
postgres  ★ 起真实 PostgreSQL，跑迁移 + 一致性校验 + 在 PG 上重跑验收
e2e-live  真实 LLM 端到端（仅 push 时，需配置 secrets）
docker    构建镜像 + 启动容器验证健康检查
```

**改了 prompt 导致能力退化会在合并前被拦住**，而不是上线后靠用户反馈发现。

---

## API 接口

### 接口分四层

| 层 | 接口 | 认证 | 说明 |
| --- | --- | --- | --- |
| **公共** | `/api/health` | ❌ 免认证 | 健康探针专用；只暴露布尔/计数，不泄露配置 |
| **认证** | `/api/auth/login` `/api/auth/me` | 部分 | 登录、查用户与配额 |
| **确定性** | `/api/metrics` `/api/quality` `/api/products` `/api/channels` `/api/schema` | ✅ | **不消耗 LLM**；只读角色也能用 |
| **工作流** | `/api/chat` `/api/requirement` `/api/weekly-review` | ✅ | 消耗 LLM 配额 |
| **可观测** | `/api/tools` `/api/tools/stats` `/api/audit/stats` | ✅ | 审计状态仅管理员 |

### 角色权限矩阵

| 角色 | 确定性接口 | SQL 接口 | 工作流 | 工具统计 | 审计状态 | 日 LLM 配额 |
| --- | --- | --- | --- | --- | --- | --- |
| `admin` | ✅ | ✅ | ✅ | ✅ | ✅ | 1000 |
| `analyst` | ✅ | ✅ | ✅ | ✅ | ❌ | 200 |
| `viewer` | ✅ | ❌ | ❌ | ❌ | ❌ | 0 |

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
| POST | `/api/sql` | 只读 SQL（五道护栏） |
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

## 命令行

```bash
# 终端演示（不需要 API Key）
python scripts/demo.py
python scripts/demo.py --live            # 加跑真实 LLM 端到端

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

## 扩展一个新工具

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

**工具的数量不是越多越好。** 按能力标签装配，给模型 3~5 个相关工具，
比一次给 13 个的选错率低得多。例如需求解析只装配 `schema + sql`
（看表结构、评估影响面），不需要 `metric`。

**扩展一类新任务**（如"库存预警报告"）：新增一个模板 + 一组能力标签 + 一个输出 Schema，
复用 `workflow/` 的骨架即可，不需要改引擎。

---

## 护栏清单

1. **数字只来自工具**：LLM 不许自算，同比/环比由代码算好注入。
2. **五道 SQL 护栏**：语法白名单 + 关键字黑名单 + 表名白名单 + 强制 LIMIT + 只读连接
   （最后一层在数据库驱动/服务端级，即使绕过前四层也无法写入）。
3. **归因纪律**：转化率下滑必须依次排查缺货 → 改价 → 流量结构 → 大促 → 商品自身。
   "详情页问题"是最后一项候选。
4. **阻断纪律**：检出 `error` 级质控问题时，必须输出"事实 + 待核查清单"，
   禁止出现因果词汇。
5. **评审双层**：能由代码判定的绝不交给 LLM（正则 + 字段校验零漏判、零成本）。
6. **审批纪律**：涉及改价/投放预算的建议一律标注"需人工审批"。
7. **不确定纪律**：样本不足（< 7 天）时禁止使用"趋势"表述，置信度必须降级。
8. **配置错误快速失败**：未配 Key 返回 503 明确指引，不伪装成"降级成功"。
9. **错误分类**：`transport`（瞬时，退避重试）/ `data`（确定性，不重试，给出修正建议）/
   `permanent`（权限，不重试，直接降级）。

---

## 踩坑记录

开发过程中被验收脚本或真实调用抓出来的缺陷。**这一节比功能列表更能说明工程质量。**

### 集成层：离线全绿但真实调用必崩

| 问题 | 根因 | 修法 |
| --- | --- | --- |
| **所有结构化输出全部失效** | LangChain 的 `with_structured_output` 默认发 `response_format: json_schema`，**DeepSeek 不支持该类型**，返回 400。基础对话却完全正常，极具迷惑性 | 新增 `llm.structured_llm()`，显式用 `method="function_calling"`，三级回退 + 缓存探测 |
| **CLI 一跑就崩** | 引擎里写 `on_progress(msg)`（1 参数），循环里写 `on_progress(round, msg)`（2 参数），回调签名没有契约 | 抽出 `safe_progress()` 自适应两种签名，回调异常不影响主流程 |
| **工具因参数名报错，白跑一轮** | `Registry.execute` 把 kwargs 直接透传，绕过了自动生成的 schema；模型传 `start_date` 而签名是 `start` | 执行前 schema 校验 + 别名映射；schema 描述里直接写出正确参数名 |

### 数据层与容错

| 问题 | 根因 | 修法 |
| --- | --- | --- |
| **错误参数名被静默忽略** | 别名表过度宽松（同时收了 `begin`/`finish` 与 `begin_date`/`finish_date`），模型传完全错误的参数名时系统静默纠偏、**用默认值执行并返回看似正常的结果** | 别名表只保留语义明确等价的写法；新增断言"错误参数名必须被拒"。**静默算错比直接报错危险得多** |
| **SQL 掩码长度不一致导致 IndexError** | 注释清洗时把换行符也吞掉又补了空格，掩码比原文多一个字符，按下标比对就越界 | 换行符留给主循环处理；加显式断言"掩码长度必须与原文一致" |
| **工具连续失败 6 轮，降级从未触发** | 失败记录被塞进 `new_data`，导致 `if not new_data` 恒为假，降级分支根本执行不到 | 成功与失败数据分开统计（`new_data` / `failed_data`） |
| **会话淘汰上限失效** | `_evict()` 在插入**之前**调用：淘汰时 3 个，插入后变 4 个 | 先插入/更新再淘汰；`stats()` 去掉副作用 |
| **PostgreSQL 的 `REAL` 是 4 字节** | 与 SQLite 同名但精度不同，金额静默丢精度 | `ddl_for()` 转 `DOUBLE PRECISION`；迁移校验必须比对金额汇总 |

### 业务逻辑与归因

| 问题 | 根因 | 修法 |
| --- | --- | --- |
| **把 406 天前的数据当"上周"汇报** | 系统日期 2026-10-09，数据止于 2025-08-29。报告标题看起来完全正常，用户看不出错位 | `detect_time_mismatch()` 注入最高优先级警告 → `_review()` 硬校验有没有声明，没声明就打回 |
| **压缩把 35 格砍成 8 格** | 固定 `max_items=8`，模型只看到局部，报告"工具只返回前 8 项"——数据其实拿到了，是被我们自己砍的 | 按规模自适应：≤12 项全量保留，大列表保留 20 项并注明省略数 |
| **投放边际递减漏报** | ① 618 花费污染了趋势基线（12.8 万，是平时的 19 倍）② 只比最后一个分桶 | 趋势计算剔除特殊日期；改用**水平位移 + 斜率双判据**（"上台阶"形态线性斜率≈0） |
| **改价检测漏报 + 8 个商品误报** | `GMV/件数` 会被"一次买几件"的件数结构干扰，不是价格信号 | 事实表增加 `unit_price` 价格快照列，改用**变点检测** |
| **变点检测算不出涨幅** | 涨价发生在窗口中间时，中位数漂移到涨价后，`baseline == current` | 枚举切分点取差异最大者，而非与全期中位数比 |
| **需求文档被判为数据分析** | 硬约束优先于需求类判定，把「分析一下这个需求文档」推去跑指标工具 | 需求类判定前移；硬约束只做**单向**兜底 |
| **两套模式表宽严不一致** | `has_data_signal` 认「情况」，但 `REPORT_PATTERNS` 只认「情况如何」，导致规则未命中、白多一次 LLM 调用 | 补齐模式表；加保守兜底：含数据信号但未命中规则时走分析报告 |
| **交叉维度的笛卡尔积放大** | 两张事实表只能按 `stat_date` 关联，直接 JOIN 使 GMV 放大 N 倍 | 改为「类目结构 × 渠道结构」重构，各渠道合计与真实值一致；显式标注"结构重构（非直接联表）" |
| **阻断纪律评审过严** | 把 warning 级问题也纳入强制提及，导致合格报告被判不通过，模型为讨好评审堆砌无关内容 | 只有 error 级才有强制提及的效力 |
| **压缩率 108%（越压越大）** | 每轮无条件加的头比省掉的内容还长 | 加净收益检查；但**轮次编号必须保留**（审计追溯的唯一线索） |

### 安全与工程实践

| 问题 | 根因 | 修法 |
| --- | --- | --- |
| **`.env.example` 里写着真实 API Key** | 模板文件通常会被提交到版本库，等于公开泄露；且项目当时**没有 `.gitignore`** | 模板清空加警告；补 `.gitignore` / `.dockerignore`；验收加断言"模板不得含真实密钥" |
| **权限矩阵漏配父子路径** | 配了 `/api/tools` 却漏了 `/api/tools/stats`（FastAPI 路径匹配是精确的，不是前缀匹配） | 改为**默认拒绝**：未配置路径仅管理员可访问 |
| **登录审计 `user` 字段为空** | 登录是唯一在认证之前就要写审计的地方，此时还没绑定用户上下文 | 登录时显式绑定请求上下文 |
| **pytest 钩子放错文件** | `pytest_addoption` 写在测试模块里，而 pytest 在**解析命令行参数时**还没导入测试模块 | 钩子必须放 `conftest.py` |
| **评测报告跨模块丢失（静默无输出）** | pytest 把 `conftest.py` 作为顶层模块 `conftest` 导入，与 `tests.conftest` 是**两个不同的模块对象**，模块级 list 不共享 | 改用进程级共享状态（报告落盘 JSON）。**关键产物必须有断言**，否则"没输出"会被当成"没问题" |
| **报告文件累积导致重复计数** | 报告落盘后不清空，第二次运行会读进上一次的 `layer_*.json` | `pytest_sessionstart` 显式清理（不能假设环境是干净的） |
| **`def f(**kw)` 型工具必报字段缺失** | schema 生成器把 `**kwargs` 当成了名为 `kw` 的必填字段 | 跳过 `VAR_KEYWORD` / `VAR_POSITIONAL` |
| **`NameError: name 'e' is not defined`** | `except ... as e` 的 `e` 离开子句后被自动清除 | 改用不同变量名 |

### 两条最贵的教训

**① 确定性骨架的错误必须靠验收脚本抓，肉眼看代码看不出来。**
上面 20 多个问题里，绝大多数在代码审阅时都"看起来完全正确"。

**② 离线验收全绿不代表能跑通。**
本项目最严重的三个问题（结构化输出、回调签名、参数名）
都是集成层问题，只有真实端到端调用才暴露 —— 而它们一度让整个项目
只有确定性部分能用，表面上却一切正常。

---

## 已知不足

诚实列出，避免过度承诺：

1. **数据是模拟的** —— 真实数仓的复杂度（脏数据、口径不一致、多数据源）尚未覆盖。
2. **PostgreSQL / Redis 未在生产环境验证** —— 适配层与双实现已完成并通过离线验收，
   真实 PG 集成测试在 CI 中运行，但**没有经过生产流量检验**。
3. **交叉维度是结构重构而非精确联表** —— 受限于数据模型（缺商品×渠道粒度），
   单个交叉格是估算值，已在返回值中显式标注"不可用于汇报精确数字"。
4. **密钥管理仍是环境变量** —— 生产应接入 KMS / Vault。
5. **认证是自实现的 HS256** —— 生产建议换 RS256 + 成熟库（PyJWT），
   本项目为减少供应链依赖而选择标准库实现。
6. **缺少真实用户反馈闭环** —— 建议被采纳后的效果没有回流到评测集。
7. **评测集规模有限**（取数 14 例 + 需求解析 4 例 + 评测 46 项断言）——
   覆盖了核心能力与 4 类陷阱，但不足以代表长尾场景。

---

## 注意事项

1. **模型**：必须用支持 Function Calling 的模型（`deepseek-chat` / V3）。
   ⚠️ 并且**结构化输出必须走 `method="function_calling"`** ——
   DeepSeek 不支持 `response_format: json_schema`（LangChain 的默认模式），
   直接调用会返回 400 `This response_format type is unavailable now`。
   本项目已封装 `app/llm.py` 的 `structured_llm()`，新增代码请复用它，
   **不要直接写 `build_llm().with_structured_output(...)`**。
2. **API Key**：只放 `.env`，不要提交 Git。
   Key 缺失时工作流接口返回 503 并给出指引，确定性接口不受影响。
3. **版本**：`langchain==1.2.18` 需配 `langgraph>=1.1.10`，
   否则导入 `create_agent` 会报 `ExecutionInfo` 缺失
   （`langgraph 1.0.x` 有该 bug）。
4. **网络**：工作流接口会多次调用 LLM，单次耗时 30~90 秒属正常。
5. **数据**：`data/ec.db` 是生成的模拟数据，`python scripts/seed_data.py` 可重置。
6. **会话历史**：`/api/chat` 的 `session_id` 用于携带前轮结论。
   默认存进程内存（重启即失效）；配置 `EC_SESSION_BACKEND=redis` 可外置到 Redis，
   供多副本部署共享。
