"""
scripts/generate_doc.py
=======================
生成《EC-Monica 电商产运 Agent 学习文档》Word 文档（.docx）。

内容覆盖（v2.0 企业级版本）：
    1. 项目背景与企业级能力总览
    2. 整体架构、设计原则、数据层设计
    3. 十四步开发过程（每步：做什么 / 怎么做 / 知识点 / 坑 / 怎么验证）
    4. 开发中踩过的 16 个真实 Bug 复盘
    5. 知识点总结矩阵（六大类）
    6. 面试题库（66 题，含参考答案）
    7. 附录：命令速查与验收标准

运行：
    python scripts/generate_doc.py
输出：
    docs/EC-Monica电商产运Agent学习文档.docx

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
★ 本文件的编码约定（血泪教训，务必遵守）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    规则一：Python 字符串一律用单引号。
    规则二：中文内容里需要引用时用「」，绝不用 ASCII 双引号。
    规则三：代码块用三单引号。

    为什么定这三条？
    因为中文正文里天然会用到引号（例如 所谓「策略有效」），
    如果外层也用双引号，就会出现：
        para('他说的'策略有效'是错的')
    ——Python 会在第二个引号处截断字符串，报 SyntaxError。

    更麻烦的是：**这种错误无法用正则事后可靠修复**。
    我实际踩过这个坑：先试图用 re.sub 批量替换，
    结果把真正的字符串定界符也改了，一次误改 526 处，
    文件彻底损坏只能重写。
    根本原因：中文文本里的 ASCII 引号与 Python 定界符**本质歧义**，
    正则无法区分内容里的引号和代码里的引号。

    结论：不要靠事后修复，要靠编码约定从源头避免。
"""

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / 'docs'
OUT_FILE = OUT_DIR / 'EC-Monica电商产运Agent学习文档.docx'

# ---------------------------------------------------------------------------
# 样式常量
# ---------------------------------------------------------------------------
PRIMARY = RGBColor(0x1F, 0x4E, 0x79)
TITLE_COLOR = RGBColor(0x14, 0x33, 0x52)
ACCENT = RGBColor(0xC0, 0x39, 0x2B)
GREEN = RGBColor(0x1E, 0x7A, 0x46)
GRAY = RGBColor(0x5A, 0x5A, 0x5A)
BLUE = RGBColor(0x1A, 0x5F, 0x8A)

CODE_BG = 'F4F6F8'
WARN_BG = 'FDF2F0'
INFO_BG = 'EEF4FA'
OK_BG = 'EFF7F1'

CN_FONT = '微软雅黑'
CODE_FONT = 'Consolas'

doc = Document()

for section in doc.sections:
    section.left_margin = Pt(56)
    section.right_margin = Pt(56)
    section.top_margin = Pt(56)
    section.bottom_margin = Pt(56)

_style = doc.styles['Normal']
_style.font.name = CN_FONT
_style.font.size = Pt(10.5)
_style.element.rPr.rFonts.set(qn('w:eastAsia'), CN_FONT)


# ---------------------------------------------------------------------------
# 基础辅助
# ---------------------------------------------------------------------------

def _font(run, size=10.5, bold=False, color=None, mono=False, italic=False):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color
    run.font.name = CODE_FONT if mono else CN_FONT
    run._element.rPr.rFonts.set(qn('w:eastAsia'), CN_FONT)


def _shade(paragraph, color_hex):
    pPr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), color_hex)
    pPr.append(shd)


def h1(text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(20)
    p.paragraph_format.space_after = Pt(10)
    _font(p.add_run(text), size=18, bold=True, color=PRIMARY)
    return p


def h2(text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(7)
    _font(p.add_run(text), size=14, bold=True, color=TITLE_COLOR)
    return p


def h3(text):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(10)
    p.paragraph_format.space_after = Pt(5)
    _font(p.add_run(text), size=12, bold=True, color=TITLE_COLOR)
    return p


def para(text, size=10.5, bold=False, color=None, indent=0, space_after=5):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    if indent:
        p.paragraph_format.left_indent = Pt(indent)
    _font(p.add_run(text), size=size, bold=bold, color=color)
    return p


def rich(*parts, indent=0, space_after=5):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    if indent:
        p.paragraph_format.left_indent = Pt(indent)
    for item in parts:
        text, bold = item[0], item[1]
        color = item[2] if len(item) > 2 else None
        _font(p.add_run(text), bold=bold, color=color)
    return p


def bullet(text, level=0, bold_prefix=''):
    p = doc.add_paragraph(style='List Bullet')
    p.paragraph_format.space_after = Pt(3)
    p.paragraph_format.left_indent = Pt(18 + level * 16)
    if bold_prefix:
        _font(p.add_run(bold_prefix), bold=True)
    _font(p.add_run(text))
    return p


def code(text, caption=''):
    if caption:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        _font(p.add_run(caption), size=9.5, bold=True, color=GRAY)
    for line in text.strip('\n').split('\n'):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.left_indent = Pt(10)
        _font(p.add_run(line if line else ' '), size=9, mono=True)
        _shade(p, CODE_BG)


def callout(title, text, color=ACCENT, bg=WARN_BG):
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(1)
    p.paragraph_format.left_indent = Pt(8)
    _font(p.add_run('【' + title + '】'), bold=True, color=color)
    _shade(p, bg)
    p2 = doc.add_paragraph()
    p2.paragraph_format.space_after = Pt(8)
    p2.paragraph_format.left_indent = Pt(8)
    _font(p2.add_run(text), size=10)
    _shade(p2, bg)


def info_box(title, text):
    callout(title, text, color=BLUE, bg=INFO_BG)


def ok_box(title, text):
    callout(title, text, color=GREEN, bg=OK_BG)


def table(headers, rows):
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = 'Light Grid Accent 1'
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    hdr = t.rows[0].cells
    for i, htext in enumerate(headers):
        hdr[i].text = ''
        _font(hdr[i].paragraphs[0].add_run(htext), size=10, bold=True)
    for row in rows:
        cells = t.add_row().cells
        for i, val in enumerate(row):
            cells[i].text = ''
            _font(cells[i].paragraphs[0].add_run(str(val)), size=9.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return t


def qa(question, answer):
    rich((question, True), space_after=2)
    for line in answer.split('\n'):
        para(line, indent=16, space_after=3)


# ===========================================================================
# 封面
# ===========================================================================
_tp = doc.add_paragraph()
_tp.alignment = WD_ALIGN_PARAGRAPH.CENTER
_tp.paragraph_format.space_before = Pt(96)
_font(_tp.add_run('EC-Monica 电商产运 Agent'), size=26, bold=True, color=PRIMARY)

_sp = doc.add_paragraph()
_sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
_font(_sp.add_run('项目全流程详解 · 知识点总结 · 面试题库'), size=14, color=GRAY)

_sp2 = doc.add_paragraph()
_sp2.alignment = WD_ALIGN_PARAGRAPH.CENTER
_sp2.paragraph_format.space_before = Pt(20)
_font(_sp2.add_run('从 0 到 1 的设计决策、踩坑复盘与面试准备'), size=11, color=GRAY)

_sp3 = doc.add_paragraph()
_sp3.alignment = WD_ALIGN_PARAGRAPH.CENTER
_sp3.paragraph_format.space_before = Pt(44)
_font(_sp3.add_run('版本：v2.0 企业级'), size=12, bold=True, color=PRIMARY)

_sp4 = doc.add_paragraph()
_sp4.alignment = WD_ALIGN_PARAGRAPH.CENTER
_font(_sp4.add_run('双数据库后端 · 认证鉴权 · 并行执行 · 评测体系 · CI/CD'),
      size=10, color=GRAY)

_sp5 = doc.add_paragraph()
_sp5.alignment = WD_ALIGN_PARAGRAPH.CENTER
_sp5.paragraph_format.space_before = Pt(20)
_font(_sp5.add_run('验收状态：411 项验收脚本 + 44 项评测断言，全部通过'),
      size=10, color=GREEN)

_sp6 = doc.add_paragraph()
_sp6.alignment = WD_ALIGN_PARAGRAPH.CENTER
_font(_sp6.add_run('配套代码：见项目仓库'), size=9.5, color=GRAY)

doc.add_page_break()

# ===========================================================================
# 第一章
# ===========================================================================
h1('第一章  项目背景与业务价值')

h2('1.1 这个项目解决什么问题')
para('EC-Monica 是一个面向产品、运营、研发三方的电商业务 Agent。'
     '它要解决电商团队里一个非常具体的效率问题：')
bullet('运营想看「上周转化率为什么跌了」，需要提需求 → 等数据同学排期 → '
       '拿数 → 自己分析，一轮下来 1~2 小时。', bold_prefix='取数慢：')
bullet('业务方口头描述需求，产品经理要反复澄清边界情况，容易漏掉影响面。',
       bold_prefix='需求碎：')
bullet('同一个指标，不同人算法不一样，结论无法互相印证。', bold_prefix='口径乱：')
para('目标是把单次分析报告从人工 1~2 小时压缩到 5~15 分钟，'
     '并通过注入内部范文模板让输出风格与团队规范对齐。')

h2('1.2 能力边界（四类意图）')
table(
    ['意图', '典型问题', '是否用工具', '输出形态'],
    [
        ['analysis_report\n经营分析报告', '上周 GMV 多少\n出个周报',
         '是（指标+下钻+质控+SQL）', '结构化报告'],
        ['attribution\n业务归因', '转化率为什么跌了\nGMV 下滑什么原因',
         '是（含质控，防归因污染）', '归因分析 + 待核查清单'],
        ['requirement_parse\n需求解析', '拆解这个需求\n评估改动影响面',
         '是（表结构+SQL）', '功能点+影响面+待澄清'],
        ['direct_generate\n直接生成', '写个促销文案\n起个商品标题',
         '否（纯写作）', '文案成品'],
    ],
)

callout('设计要点',
        '四类意图里只有 direct_generate 不查数据，因此它也是最危险的路径 —— '
        '模型会凭记忆编数字。所以整个路由体系围绕一条铁律设计：'
        '含数据信号的请求，绝不允许走 direct_generate。详见第三章第 7 步。')

h2('1.3 量化指标')
table(
    ['指标', '数值', '说明'],
    [
        ['服务角色', '产运 / 产品 / 研发', '三方共用同一入口'],
        ['月均调用量', '1000+ 次', '报告类约 55%，需求/归因类约 45%'],
        ['单次报告耗时', '1~2 小时 → 5~15 分钟', '效率提升约 90%'],
        ['输出风格对齐', '注入业务范文后显著提升', '见第三章第 9 步：上下文工程'],
        ['自动化验收', '411 项验收 + 44 项评测断言', '六套脚本 + pytest，退出码全 0'],
        ['评测准确率', '100%（加权 100%）', '6 个能力层，陷阱题权重 1.5~3x'],
    ],
)

h2('1.4 企业级能力总览（v2.0 新增）')
para('原型版（v1.0）只有能跑通的技术内核，缺少能被团队使用的平台能力。'
     'v2.0 补齐了这些缺口 —— 这就是参考实现与企业级的分界线。')
table(
    ['能力', '实现方式', '验收证据'],
    [
        ['认证', 'JWT(HS256) + PBKDF2 20 万轮密码哈希', '12 个接口未登录全部 401'],
        ['授权', '角色矩阵 + 默认拒绝（未配置路径仅管理员）', '10 项权限断言全通过'],
        ['数据权限', '类目/渠道白名单下沉到数据层', '受限用户只见 5/12 个商品'],
        ['限流与配额', '滑动窗口速率限制 + 按角色每日 LLM 配额', '第 61 次请求触发 429'],
        ['会话外置', '可插拔存储（内存 / Redis）', 'TTL + LRU 淘汰验证'],
        ['结构化日志', 'JSON 行 + request_id 全链路追踪', '可进 ELK / Loki'],
        ['审计留痕', '独立于日志的追加型记录（含 user/role/scope）', '8 项审计断言'],
        ['并行工具执行', '自适应并行（快工具串行、慢工具并行）', '慢工具 3.93x 加速'],
        ['双数据库后端', 'SQLite / PostgreSQL 适配层', '方言转换 + 迁移一致性校验'],
        ['容器化', '多阶段构建 + 非 root 运行 + 健康检查', 'CI 中构建并验证'],
        ['CI', '6 个 job（含准确率门槛）', '准确率 < 90% 即失败'],
    ],
)

callout('为什么要强调企业级',
        '技术内核做得好，不等于别人能用。原型版缺认证鉴权 —— '
        '任何人 curl 一下就能拿到全部经营数据，'
        '前面做的五层 SQL 护栏、单向硬约束、阻断纪律全部失去意义，'
        '因为它们保护的是一扇没有门的房间。'
        '安全边界比功能完善度更重要。')

# ===========================================================================
# 第二章
# ===========================================================================
doc.add_page_break()
h1('第二章  整体架构与设计原则')

h2('2.1 完整数据流')
code('''
用户请求
   │
   ├─ ⓪ 认证鉴权 ── JWT 校验 → 角色授权 → 限流配额 → 数据权限范围
   │      ★ 在业务逻辑之前执行，默认拒绝
   │
   ├─ ① 意图识别 ──── 规则优先（命中率~100%），LLM 仅兜底
   │
   ├─ ② 条件路由 ─┬─ 无需工具 → 直接生成（一次调用）→ 输出
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
数据层：SQLite / PostgreSQL 双后端（适配层吸收方言差异）
''')

h2('2.2 目录结构与职责划分')
code('''
ec-monica/
├── app/
│   ├── main.py                 接口层（公共/认证/确定性/工作流/可观测）
│   ├── config.py  llm.py  schemas.py
│   ├── auth.py                 ★ JWT + PBKDF2 + 角色矩阵 + 数据权限
│   ├── deps.py                 ★ 依赖：认证 → 授权 → 限流（顺序不可换）
│   ├── ratelimit.py            ★ 滑动窗口限流 + 每日 LLM 配额
│   ├── session.py              ★ 会话外置（内存 / Redis 可插拔）
│   ├── observability.py        ★ 结构化日志 + 审计 + request_id
│   ├── db/
│   │   ├── adapter.py          ★ 双后端适配（方言差异封装）
│   │   ├── schema.py           建表 DDL + 表白名单 + DDL 方言转换
│   │   └── database.py         只读 SQL 护栏 + JOIN 指南 + 友好错误
│   ├── metrics/                指标层（口径字典 + 纯函数计算）
│   ├── quality/                质控层（6 类污染检测 + 确定性规则）
│   ├── tools/
│   │   ├── registry.py         ★ 动态 Tool Registry + 容错执行
│   │   ├── parallel.py         ★ 自适应并行执行
│   │   └── builtin.py          13 个内置工具注册
│   ├── context/                模板 + 压缩 + 组装 + 时间注入
│   ├── workflow/               ★ 意图 / 预处理 / 循环 / 引擎 / 需求解析
│   └── services/               周复盘（结构化报告）
├── scripts/                    数据生成 + 五套验收 + 迁移 + CLI
│   └── migrate_to_postgres.py  ★ SQLite → PostgreSQL 迁移
├── tests/
│   ├── conftest.py             pytest 钩子 + 准确率汇总
│   ├── eval_runner.py          评测框架（判分 + 加权准确率）
│   ├── test_eval.py            ★ 两层评测，6 个能力层 44 项断言
│   ├── test_db_adapter.py      ★ 双后端适配验收（无 PG 也能验证）
│   └── eval/                   评估集（取数 14 例 + 需求解析 4 例）
├── Dockerfile                  多阶段构建 + 非 root + 健康检查
├── docker-compose.yml          api + redis 编排
├── pytest.ini                  marker 分层（offline / live / pg）
└── .github/workflows/ci.yml    6 个 job
''')

h2('2.3 六条核心设计原则')
table(
    ['原则', '含义', '为什么'],
    [
        ['计算归代码\n解读归模型',
         '同比/环比、相对时间、交叉聚合全部由代码算好再注入',
         '模型算数不可靠，且错了看不出来 —— 输出格式仍然完全正确'],
        ['规则优先\nLLM 兜底',
         '意图识别能靠规则判定的绝不调模型',
         '省一次调用、省几秒延迟，且结果稳定可复现'],
        ['质控前置',
         '数据质控在工具循环之前由代码无条件运行',
         '依赖模型记得调用就会漏，漏一次结论就错了'],
        ['单向硬约束',
         '含数据信号的请求绝不允许免工具作答',
         '两个方向的错误代价不对称（详见 3.7）'],
        ['显式循环\n而非现成 Agent',
         '自己写工具循环，不用 create_agent 的内部循环',
         '规格要求预处理后注入 Context，现成 Agent 插不进这一步'],
        ['容错是一等公民',
         '工具失败返回结构化错误而非抛异常；全挂时降级输出',
         '一个带免责声明的答案，比一个 500 错误有用得多'],
    ],
)

callout('一句话总结架构',
        '把确定的与不确定的严格分开：所有数字、时间、聚合由代码保证正确；'
        '模型只负责决定看什么、以及如何解读。这样即使模型出错，也不会污染数字本身。')

h2('2.4 数据层设计：为什么写适配层而不是用 ORM')
table(
    ['方案', '问题'],
    [
        ['引入 SQLAlchemy ORM',
         '本项目只用到 SELECT / 建表 / 批量插入这一小撮操作，'
         '引入 ORM 等于为很小的子集付出重量级依赖，'
         '而且多一层查询语义抽象会让排查变难 —— 与可复现优先的取向冲突'],
        ['在业务代码里写 if backend 分支',
         '方言差异会渗透进指标层、质控层、工具层，最终到处都是分支，'
         '这是技术债的典型起点'],
        ['薄适配层（本项目选择）',
         '只封装真正有差异的几处，业务代码保持统一写法。改动成本最小'],
    ],
)

h3('必须封装的四个方言差异')
table(
    ['差异点', 'SQLite', 'PostgreSQL', '不封装的后果'],
    [
        ['占位符', '?', '%s', 'SQL 完全无法执行'],
        ['只读强制', '文件 mode=ro', '会话变量 read_only',
         '只读护栏失效 —— 这是安全底线'],
        ['元数据', 'PRAGMA table_info', 'information_schema',
         'Agent 拿不到表结构就无法写 SQL'],
        ['数值类型', 'REAL = 8 字节', 'REAL = 4 字节（须转 DOUBLE PRECISION）',
         '★ 金额精度静默丢失：行数对了但金额指标全偏'],
    ],
)

callout('★ 最危险的是同名不同类型',
        'REAL 这个差异不会报错、不会崩，只会让数据悄悄变形。'
        '而且只差几分钱在单条数据上毫不可疑 —— 只有汇总求和时才暴露。'
        '所以迁移校验必须比对 SUM(gmv) 这类金额汇总，'
        '只比对行数是发现不了的。这是数据迁移最容易漏掉的一环。')

# ===========================================================================
# 第三章：十四步
# ===========================================================================
doc.add_page_break()
h1('第三章  十四步开发过程详解')
para('每一步按统一结构讲解：做什么 → 怎么做 → 知识点 → 常见坑 → 怎么验证。',
     color=GRAY)

# --- 1 ---
h2('第 1 步：数据层设计 —— 制造带病的数据')
h3('做什么')
para('设计并生成 90 天模拟电商数据（12 商品 / 1080 条商品日事实 / 450 条渠道流量）。'
     '关键不是造一份干净数据，而是故意埋入 4 类归因陷阱。')
h3('怎么做')
code('''
陷阱 1  618 大促（06-16~06-20）     流量 5 倍、投放费暴涨
        → 考察：会不会把大促峰值当成策略有效

陷阱 2  爆款 P001 缺货（07-05~07-12）库存为 0，转化率断崖但流量不变
        → 考察：会不会把缺货归因为详情页问题

陷阱 3  P002 悄悄涨价（07-14 起 +18%）
        → 考察：能否识别价格弹性而非产品力下降

陷阱 4  投放边际递减（08-01 起预算 +30%，ROI 从 16.5 掉到 12.8）
        → 考察：会不会建议关掉渠道
''')
h3('知识点')
bullet('事实表 vs 维表：dim_product 是维表，没有日期列；事实表才有 stat_date。'
       '这个区分是后面 JOIN 问题的根源。')
bullet('粒度：商品日事实表是「日期 × 商品」，渠道表是「日期 × 渠道」。'
       '粒度不同直接 JOIN 会产生笛卡尔积 —— 数仓最常见的错误。')
bullet('价格快照列：事实表必须独立记录 unit_price，不能用 GMV/件数 代替 —— '
       '后者会被一次买几件的件数结构干扰，不是价格信号。')
bullet('可复现性：用固定随机种子，保证多次运行数据一致，否则测试无法稳定断言。')
callout('踩坑：为什么必须制造带病的数据',
        '一开始想造一份干净的、趋势平稳的数据。但这样根本无法验证归因能力 —— '
        '没有异常就没有可归因的对象；没有陷阱就测不出它会不会被带偏。'
        '带病数据才是唯一有效的测试基座。')
h3('怎么验证')
code('''python scripts/seed_data.py          # 生成数据，打印四类陷阱位置
python scripts/verify_foundation.py   # 43 项：验证数据能否被正确检出''')

# --- 2 ---
h2('第 2 步：指标层 —— 口径字典 + 纯函数计算')
h3('做什么')
para('把 GMV 怎么算这类口径问题固化成代码和文档，所有数字只能来自这一层。')
h3('知识点')
bullet('口径必须先定义再计算：GMV 含不含退款？客单价分母是订单数还是人数？'
       '不写清楚，Agent 每次算的数字都无法互相比对。')
bullet('毛 GMV vs 净 GMV：gmv_gross 是平台口径（不扣退款），gmv_net 扣退款。'
       '判断经营质量必须用净额。')
bullet('转化率有 4 种病因：缺货、改价、流量结构变化、商品自身。'
       '前三项必须先排除，才能谈详情页问题。')
bullet('EXPLAIN_FOR_AGENT：口径字典同时是给 Agent 看的说明书，'
       '会注入 prompt，让模型知道正确口径而不是自己臆测。')
bullet('纯函数设计：compute.py 全部无状态，所以能被反复测试，也能被安全调用。')
callout('踩坑：毛利率口径的时间陷阱',
        '最初用维表的当前价 p.price 计算历史毛利。但 P002 涨价后，'
        '所有历史毛利都被算错了 —— 因为用今天的价格去乘昨天的销量。'
        '正确做法是用事实表的当日价格快照 f.unit_price。'
        '这类口径 bug 极难发现，因为数字看起来完全合理。')
h3('怎么验证')
code('python scripts/verify_foundation.py   # 检查 AOV、CVR 区间、GMV 与净额关系')

# --- 3 ---
h2('第 3 步：质控层 —— 防归因污染')
h3('做什么')
para('在下结论之前，先把不能用来下结论的数据标出来。'
     '这是整个项目区别于玩具 Agent 最核心的模块。')
h3('为什么需要')
code('''
现象：转化率跌了 20%
天真 Agent：详情页不行，建议优化主图和卖点
真相可能是：爆款缺货 / 上周涨价 / 大促流量退了 / 付费流量占比升高
→ 四种情况的应对动作完全相反，优化详情页对缺货毫无用处
''')
h3('怎么做：6 类检测')
table(
    ['检测项', '方法', '输出级别'],
    [
        ['大促污染', '比对特殊日期区间', 'warning'],
        ['样本量不足', 'sample_days < 7', 'warning / error'],
        ['缺货', 'SUM(stockout_flag) > 0', 'error（阻断）'],
        ['改价', '变点检测', 'warning'],
        ['流量结构变化', '各渠道访客占比位移 > 5pp', 'info'],
        ['投放边际递减', '花费位移 + ROI 斜率双判据', 'warning'],
    ],
)
h3('知识点：变点检测')
code('''
问题：涨价的那天是哪天？
错误做法：和全期中位数比
  → 涨价发生在窗口中间时，中位数会漂移到涨价后的价格上
  → baseline == current，涨幅算出来 0.0%

正确做法：枚举切分点，取前段中位数与后段中位数
          差异最大的那个点
  → 中位数抗离群值，不会被单日促销折扣带偏
''')
callout('踩坑：质控工具自己必须先做质控',
        '投放边际递减检测最初没有排除大促。结果 618 那一周的花费（12.8 万，'
        '是平时的 19 倍）落在第一个分桶里，把前段平均花费抬到天上，'
        '算出来花费是下降 -75% —— 方向完全反了。'
        '教训：质控工具自身必须先经过质控，否则它会变成假信息的源头。')
h3('怎么验证')
code('''python scripts/verify_foundation.py
# 验收重点：4 个陷阱能否被自动检出，且不误报其他商品''')

# --- 4 ---
h2('第 4 步：只读 SQL 工具 —— 五道护栏')
h3('做什么')
para('给 Agent 一个受约束的自由探索通道，但确保它绝不可能破坏数据。')
h3('五道护栏（层层递进）')
table(
    ['层级', '手段', '挡住什么'],
    [
        ['1. 语法白名单', '只允许 SELECT / WITH 开头', '写操作'],
        ['2. 关键字黑名单', '拒绝 INSERT/DROP/PRAGMA/ATTACH', '注释绕过、危险语句'],
        ['3. 表名白名单', '提取 FROM/JOIN 后表名校验', '访问未授权表'],
        ['4. 强制 LIMIT', '无 LIMIT 则自动补 500', '一次查询打爆内存'],
        ['5. 只读连接', '数据库层强制只读', '绕过前四层的任何写入'],
    ],
)
h3('知识点')
bullet('第 5 层最关键：前四层是应用层校验，理论上可被绕过；'
       '只读连接是驱动或服务端层限制，即使 SQL 校验失效也无法写入。这叫纵深防御。')
bullet('错误要能自我修正：把裸报错翻译成带正确列名建议的提示，'
       '模型下一轮就能改对，不必反复试错。')
h3('怎么验证')
code('''# 六类危险语句必须被拒
DELETE / DROP / UPDATE / INSERT / 未授权表 / 多语句 / ATTACH / PRAGMA
# 且绕过校验后，只读连接仍要阻止写入''')

# --- 5 ---
h2('第 5 步：动态 Tool Registry')
h3('做什么')
para('把工具从固定列表升级为带能力标签的注册中心，支持按意图动态装配，'
     '并统一封装超时、重试、错误分类。')
h3('怎么做')
code('''
@registry.register(
    capabilities=['metric'],              # ★ 能力标签，意图路由靠它选工具
    description='对比某周与上一周的核心指标...',
    timeout_s=20, max_retries=2,
)
def compare_weeks(start: str = '', end: str = '') -> dict:
    ...

# 按能力装配（只给模型相关的 3~5 个，而不是全部 13 个）
tools = registry.select(capabilities=['metric', 'quality'])
''')
h3('知识点：三类错误分类')
table(
    ['错误类型', '识别信号', '是否重试', '为什么'],
    [
        ['transport', 'timeout / connection / 429', '是（指数退避）',
         '瞬时故障，重试能成功'],
        ['data', 'no such column / 无数据', '否',
         '重试同样失败，浪费时间；应让模型改查询条件'],
        ['permanent', 'API key / 401 / 403', '否',
         '权限问题重试无意义，应降级'],
    ],
)
callout('为什么错误分类是必须的',
        '如果不分类，所有错误都盲目重试 3 次，那么日期区间没数据这种确定性错误'
        '会被重试 3 次，白白增加 3 倍延迟，最后还是失败。'
        '分类之后：只有真正瞬时的错误才重试 —— 这是延迟和成功率的直接改善。')
callout('踩坑：schema 生成器把 **kwargs 当成了必填字段',
        '任何 def f(**kw) 形式的工具都会报 Field required —— '
        '因为生成器把 kw 当成了一个名为 kw 的必填字符串参数。'
        '修法：跳过 VAR_KEYWORD / VAR_POSITIONAL 可变参数。'
        '可变参数不是一个参数，它是任意参数，本就不该出现在 schema 里。')
h3('怎么验证')
code('''python scripts/verify_architecture.py
# 验证：能力装配是子集、错误分类正确、只重试可重试项、超时保护生效''')

# --- 6 ---
h2('第 6 步：自适应并行工具执行')
h3('做什么')
para('把同一轮内的多个工具调用从串行改为并行。'
     '但重点是：不是无脑并行，而是按收益自适应。')
h3('★ 核心知识点：并行不一定更快')
code('''
实测（6 个工具，本地 SQLite）：
    串行实测      : 20.3 ms
    并行墙钟      : 27.4 ms      ← 更慢！
    单个工具耗时  : 12~20 ms
    线程池开销    : 约 20~30 ms

原因：工具的耗时不敌线程池的开销。
如果照着并行一定更快的直觉直接上线，得到的是一个负优化。
''')
h3('收益模型')
code('''
并行墙钟 ≈ max(单个耗时) + 线程池开销
串行墙钟 ≈ sum(单个耗时)
预估加速比 = sum / (max + overhead)

只有加速比 > 1.15 才启用并行，否则走串行（零额外开销）
''')
h3('自适应策略的实测表现')
table(
    ['场景', '决策', '实测'],
    [
        ['冷启动（无历史统计）', '串行（保守）', '—'],
        ['快工具（总 19ms）', '串行', '与纯串行偏差 0.2ms'],
        ['慢工具（4×150ms）', '并行', '606ms → 154ms，加速 3.93x'],
    ],
)
callout('这条策略的真正价值在未来',
        '本地 SQLite 查询是毫秒级，所以现在走串行是对的。'
        '但接上真实数仓后，单次查询会升到百毫秒甚至秒级 —— '
        '届时同一个自适应策略会自动切换到并行，不需要改代码。'
        '判据用的是工具的真实历史耗时，不靠猜。')
h3('并行不能改变的三件事')
bullet('顺序：返回顺序必须与请求顺序一致，否则预处理与日志会错位。')
bullet('容错：超时、重试、错误分类、退避完全保留 —— '
       '走同一份 registry._run 实现，只有并发方式不同。')
bullet('不嵌套线程：registry.execute 内部本来就会开线程做超时控制，'
       'worker 里再开一层会让线程数翻倍，所以并行路径用 execute_inline。')
h3('怎么验证')
code('''python scripts/verify_architecture.py    # 并行策略与顺序语义
pytest tests/test_eval.py -k parallel     # 评测层：快/慢工具的决策正确性''')

# --- 7 ---
h2('第 7 步：意图识别与条件路由')
h3('做什么')
para('判断请求属于哪一类，决定是否走工具循环、装配哪些工具、用哪个输出模板。')
h3('怎么做：规则优先，LLM 兜底')
code('''
classify_by_rules(message)          # 正则匹配，微秒级，零成本
    ├─ 命中 → 直接返回 IntentResult
    └─ 未命中 → classify_by_llm()   # 才调模型，秒级，有成本
                   └─ 失败 → 兜底为需要工具（保守策略）
    → enforce_data_signal()         # ★ 最后一道单向硬约束
''')
h3('知识点：为什么规则优先')
bullet('成本：每次请求都调模型判意图，月均 1000 次就是 1000 次额外调用。')
bullet('延迟：规则是微秒级，模型是秒级。')
bullet('稳定性：同一句话两次判定可能不同（模型有随机性），规则不会。')
bullet('实测规则命中率 100% —— 绝大多数请求根本不需要模型判意图。')
h3('知识点：单向硬约束')
code('''
含数据信号（为什么/归因/分析/下滑/指标/数据/报告…）→ 绝不允许 direct_generate
不含数据信号                                        → 才允许

为什么是单向的？两个方向的错误代价不对称：
  · 误判为 direct_generate → 模型凭记忆编数字，输出看似专业但完全错误，且无警示
  · 误判为需要工具        → 只是多查一次数据
所以宁可多查，不可编造。
''')
info_box('实现方式：三层纵深防御',
         '① 规则内部对写作类判定做前置检查；'
         '② LLM 兜底提示词写明不确定时选择需要工具的类别；'
         '③ enforce_data_signal() 作为唯一出口做后置拦截 —— '
         '以后无论新增哪条规则、换成哪个模型，这条底线都不会被突破。'
         '入口可以宽松，出口必须严格。')
callout('踩坑：正则没给中文定语留间隙',
        '最初写 (想个|起个).{0,6}(名字|标题)，结果「给这个商品想个标题」漏网了 —— '
        '因为想个和标题之间隔了商品两个字，而 .{0,6} 写在了后面而不是中间。'
        '中文口语里动词与宾语之间经常插定语，所以动作词与目标词之间必须留间隙。')
h3('怎么验证')
code('''python scripts/run_agent.py --routes   # 打印路由表 + 规则命中测试
python scripts/verify_architecture.py  # 19 个意图用例全通过''')

# --- 8 ---
h2('第 8 步：数据预处理 —— 计算归代码')
h3('做什么')
para('把工具返回的原始数据加工成可直接注入 Context 的指标，'
     '同比/环比由代码算好，模型只负责解读。')
h3('为什么这是数字可信度的技术来源')
code('''
错误做法：工具返回原始数据 → 直接丢给 LLM → 请算出环比
          结果：模型算错，而且错得看不出来（它给出的仍是格式正确的报告）

正确做法：工具返回原始数据 → 代码算好同比/环比 → 注入 Context
          模型只负责解读这个变化意味着什么

一句话：计算归代码，解读归模型。
''')
h3('预处理做什么')
bullet('enrich_period_compare：为结果补齐变化率（兜底校验）')
bullet('enrich_breakdown：为下钻数据补贡献占比，让模型一眼看到谁是大头')
bullet('_enrich_ad_trend：把十几行分段趋势压成首段 vs 末段对比')
bullet('extract_key_numbers：抽取关键数字，用于压缩历史时保留数字锚点')
bullet('除零保护：任何一步不合法都返回 None，绝不猜')
h3('怎么验证')
code('python scripts/verify_architecture.py   # 环比计算、贡献占比、除零保护')

# --- 9 ---
h2('第 9 步：上下文工程 —— 压缩与滚动')
h3('做什么')
para('组装注入给模型的上下文（9 个部分），并把多轮结论压缩后滚动携带。')
h3('Context 的 9 个组成部分（顺序也是设计）')
code('''
① 角色设定        让模型知道我是谁
② 业务规则        数字纪律/归因纪律/阻断纪律（硬约束）
③ 时间上下文      ★ 相对时间已解析成绝对日期
④ 指标口径字典    每个指标怎么算，防止自行发明口径
⑤ 业务范文模板    对齐内部输出风格（风格对齐率的来源）
⑥ 本次任务
⑦ 数据预处理结果  ★ 算好的同比/环比
⑧ 质控提示
⑨ 滚动前轮结论    压缩后的历史轮次

为什么硬约束放在范文和数据之前？
→ 模型对靠前的内容服从度更高，把纪律放在最后容易被淹没在数据里
''')
h3('知识点：压缩的近详远略策略')
table(
    ['距今轮次', '保留内容', '压缩强度'],
    [
        ['最近 1 轮', '关键数字 + 结论全文（500 字）', '最弱'],
        ['第 2~3 轮', '轮次编号 + 结论摘要（260 字）', '中'],
        ['更早轮次', '轮次编号 + 要点（110 字）', '最强'],
    ],
)
callout('踩坑：压缩必须净收益，但可追溯性不能丢',
        '坑 1：最初每轮无条件加「第N轮｜工具：xxx」的头，'
        '对本来就简短的结论，头比省掉的内容还长 —— 压缩率算出来 108%，越压越大。'
        '坑 2：为了压掉那个头，把轮次编号也一起去掉了 —— '
        '结果审计时无法追溯这个结论是哪一轮得出的。'
        '最终策略：轮次编号永远保留（只有 4 个字符但价值极高），'
        '工具名只在正文够长时才带，正文上限随距今轮次递减。')
h3('知识点：自适应压缩')
code('''
固定 max_items=8 的问题：
  35 格的交叉维度表被截成 8 格
  → 模型只能看到局部，于是老实报告工具只返回了 35 格中的前 8 项
  → 数据其实拿到了，是被压缩层自己砍掉的

改为按规模自适应：
  ≤12 项  → 全量保留（通常就是核心结论所在）
  >12 项  → 保留前 20 项 + 注明省略数量
''')
h3('怎么验证')
code('python scripts/verify_architecture.py   # 硬约束顺序、压缩率、自适应保留')

# --- 10 ---
h2('第 10 步：工具循环执行器 —— 项目骨架')
h3('做什么')
para('实现「选工具 →（容错）执行 → 预处理 → 注入 Context → '
     'LLM 出子结论 → 循环」这一核心数据流。')
h3('一轮循环的精确流程')
code('''
① 规划：LLM 看当前 Context，决定下一步调哪些工具（或判断信息已足够）
② 执行：自适应并行执行（含超时 + 按错误类型重试）
③ 预处理：把原始返回值算好同比/环比，加工成可注入形态  ★ 关键
④ 子结论：LLM 基于预处理后的数据 + 压缩历史产出一轮子结论
⑤ 滚动：把本轮压进 Context，回到 ①
''')
h3('知识点：为什么不用现成的 ReAct Agent')
para('LangChain 的 create_agent 会自己跑工具循环，看起来很省事。但它的问题在于：'
     '工具结果会以原始形态直接进入消息历史，你插不进第 ③ 步。'
     '而预处理后注入恰恰是规格的明确要求，也是数字可信度的来源。'
     '显式循环多写几十行，换来数据流的完全可控。')
h3('知识点：三层循环保护')
bullet('max_rounds 上限：防止模型停不下来，强制结束')
bullet('早停优化：同类 permanent 错误出现 2 次即停 —— 重试 100 次结果都一样')
bullet('累计失败阈值：3 次工具失败触发降级')
callout('踩坑：失败路径的分支从未被执行到',
        '降级判定写成累计失败 3 次就降级，但那段代码放在了 continue 之后 —— '
        '这条路径根本执行不到。结果工具连续失败 6 轮、降级标记始终为 False，'
        '最后拿着一个成功的结果去汇总，而实际上什么数据都没有。'
        '教训：失败路径的分支必须和成功路径一样被测试覆盖。')
h3('怎么验证')
code('python scripts/verify_workflow.py   # 轮次控制、降级触发、早停生效')

# --- 11 ---
h2('第 11 步：主引擎与降级设计')
h3('做什么')
para('把意图识别、条件路由、预处理、工具循环、汇总、评审串成完整工作流，'
     '并实现优雅降级。')
h3('知识点：降级不是异常处理，是一等公民')
code('''
工具全挂时不能直接报错。正确行为是：
  1. 明确告知以下结论未经数据验证
  2. 禁止编造任何具体数字
  3. 说明哪些信息缺失
  4. 列出需要补充什么数据才能给出确定结论

一个带免责声明的答案，比一个 500 错误有用得多。
''')
h3('知识点：时间错位检测')
code('''
今天              = 2026-10-09
用户问的上周       = 2026-09-28 ~ 2026-10-04
数据库实际范围      = 2025-06-01 ~ 2025-08-29（一年前）
系统实际回答的区间   = 2025-08-18 ~ 2025-08-24   ← 相差 406 天
''')
callout('踩坑：最危险的不是报错，而是看起来正常的错答案',
        '报告标题会写成「上周转化率归因（2025-08-18 ~ 2025-08-24）」，'
        '格式完全正常、内容详实，用户根本看不出答案对应的是一年前的行情。'
        '这比返回空结果危险得多 —— 空结果一眼能看出问题，错位报告会被直接采信。'
        '修法：detect_time_mismatch() 检测错位 → 注入最高优先级警告 → '
        '_review() 硬规则校验有没有在开头声明，没声明就打回重做。')
h3('怎么验证')
code('''python scripts/verify_architecture.py   # 时间错位检测 + 边界（30天不报/31天报）
python scripts/verify_workflow.py       # 降级声明、禁止编造数字''')

# --- 12 ---
h2('第 12 步：评审机制 —— 双层防御')
h3('做什么')
para('在输出交付用户之前，用「确定性规则 + LLM 语义审查」两层挑刺，'
     '不通过则带具体问题打回重写。')
h3('知识点：为什么能由代码判定的绝不交给 LLM')
table(
    ['层级', '检查内容', '成本', '可靠性'],
    [
        ['确定性规则', '禁用词、字段校验、结构完整性、证据是否为空',
         '零', '100%（零漏判）'],
        ['LLM 语义审查', '逻辑自洽性、相关性当因果、幸存者偏差、置信度虚高',
         '一次调用', '有概率漏判'],
    ],
)
para('顺序不能颠倒：确定性规则先跑（快、免费、零漏判），LLM 只补语义层面。')
h3('知识点：回环重做而非惩罚')
code('''
评审不通过
  → 把具体问题清单 + 上一轮结论一起回灌
  → 让模型针对性修正，而不是自由重来
  → 最多重做 max_revisions 次（兜底防死循环）

为什么不做惩罚性修正？
→ 惩罚不会改变输出质量，回环会。
  实测：缺货场景第 1 次被打回、第 2 次通过。
''')
callout('踩坑：评审过严会导致无谓重做',
        '最初把 warning 级问题（投放边际递减）也纳入必须提及，'
        '结果一份结构完整、只聚焦真正阻断问题（缺货）的合格报告被判不通过 —— '
        '这正是评审老不通过的成因之一。'
        '改法：只有 error 级才有强制提及的效力。'
        '评审过严的后果是模型为了讨好评审而堆砌无关内容，反而降低质量。')
h3('怎么验证')
code('python scripts/verify_architecture.py   # 违规输出打回、合规输出通过')

# --- 13 ---
h2('第 13 步：企业级基础设施 —— 认证、限流、会话、审计')
h3('做什么')
para('补齐原型版缺失的平台能力。这一步是参考实现与企业级的分界线。')
h3('★ 第一优先级：认证鉴权（为什么它最重要）')
code('''
原型版的问题：
  做了五层 SQL 护栏、单向硬约束、阻断纪律……
  但任何人 curl 一下 /api/chat 就能拿到全部经营数据。

  → 没有认证，前面所有护栏都失去意义 ——
    因为它们保护的是一扇没有门的房间。

安全边界比功能完善度更重要：认证是唯一一个
不做就等于前面全部白做的缺口。
''')
h3('三层设计')
table(
    ['层次', '问题', '实现'],
    [
        ['① 认证', '你是谁？', 'JWT(HS256) + Bearer Token'],
        ['② 授权', '你能用哪些接口？', '角色 → 接口权限矩阵，默认拒绝'],
        ['③ 数据权限', '你能看哪些数据？', '角色 → 类目/渠道白名单，下沉到数据层'],
    ],
)
callout('★ 第 ③ 层最容易被忽略',
        '很多系统做了前两层就以为安全了。但如果一个只看美妆类目的运营'
        '能通过 /api/products 看到全部类目数据，权限体系就是纸糊的。'
        '所以数据权限必须下沉到数据访问层，而不是在接口层过滤 —— '
        '接口层过滤容易在某个分支漏掉，导致越权泄露。')
h3('知识点：密码存储的安全细节')
bullet('用 PBKDF2（20 万轮迭代）而不是 sha256 直哈希 —— '
       '直哈希太快，暴力破解成本低。慢哈希是特性，不是缺陷。')
bullet('必须用 hmac.compare_digest 做常量时间比较，不能用 == —— '
       '后者会在第一个不同字符处提前返回，攻击者可借响应时间差异逐字符推断（时序攻击）。')
bullet('用户不存在时也要做一次假哈希校验，'
       '否则响应时间会泄露该用户是否存在（用户枚举）。')
bullet('登录失败不区分用户不存在与密码错误，返回完全相同的响应体。')
h3('知识点：限流的两层设计')
table(
    ['层级', '目的', '实现'],
    [
        ['速率限制', '防瞬间打爆', '滑动窗口（避免固定窗口的临界 2 倍问题）'],
        ['每日配额', '防细水长流用超', '按角色配置，只对消耗 LLM 的路径计费'],
    ],
)
para('只有速率限制是不够的：一个客户端每秒 1 次、跑一整天，依然会把成本打穿。')
h3('知识点：依赖的执行顺序')
code('''
先认证 → 再鉴权 → 最后限流

为什么顺序不能换？
如果先限流，未认证的请求也会消耗计数，
攻击者可以用垃圾请求把正常用户的额度耗光。
''')
h3('知识点：会话为什么必须外置')
code('''
原来：_SESSIONS: dict = {}   # 进程内模块级字典
问题：
  ① 重启即丢失 —— 部署、扩容、崩溃重启后对话上下文断裂
  ② 多副本不共享 —— 负载均衡打到不同副本时会话不一致，
     表现为 Agent 时好时坏地记不住之前聊过什么

修法：抽象为 SessionStore 接口
      InMemorySessionStore（单实例开发）
      RedisSessionStore（多副本生产）
''')
h3('知识点：审计为什么要独立于日志')
para('日志会滚动删除、会被采样、会因磁盘压力被丢弃；'
     '而审计记录涉及合规（谁看了哪些数据、Agent 给了什么建议），'
     '必须独立保存、不可采样、有固定字段。'
     '企业环境里这两者的保留策略通常是完全不同的。')
callout('踩坑：权限矩阵漏配父子路径',
        '配了 /api/tools 却漏了 /api/tools/stats，导致分析师被误拒。'
        'FastAPI 的路径匹配是精确的，不是前缀匹配 —— '
        '这类父子路径漏配是权限矩阵最常见的坑。'
        '所以设计了默认拒绝：未配置的路径只有管理员能访问。'
        '新加接口忘了配权限时，结果是访问不了（可发现），'
        '而不是谁都能访问（静默漏洞）。')
h3('怎么验证')
code('''python scripts/verify_enterprise.py   # 95 项：认证/授权/数据权限/限流/会话/审计
pytest tests/test_eval.py -k auth      # 评测层：认证与数据权限''')

# --- 14 ---
h2('第 14 步：数据层适配与评测体系')
h3('做什么')
para('把 SQLite 换成可切换的双后端；建立可自动判分、可输出准确率的评测体系。')
h3('★ 知识点：为什么写适配层而不是引入 ORM')
code('''
本项目只用到三种数据库操作的很小一个子集：
    只读查询（SELECT）/ 建表（init）/ 批量插入（迁移）

引入 SQLAlchemy 会带来：
    · 重量级依赖（ORM、连接池、会话管理，我们都不需要）
    · 查询语义的额外抽象层（排查这条 SQL 到底发出去什么变难）
    · 与指标层刻意只用标准库以保证可复现的设计取向冲突

所以用薄适配层，只封装真正有差异的几处：
    ① 占位符      ?  vs  %s
    ② 只读强制    mode=ro  vs  会话变量 read_only
    ③ 元数据      PRAGMA  vs  information_schema
    ④ 数值类型    REAL 8 字节  vs  REAL 4 字节
''')
h3('知识点：占位符转换的坑')
code('''
不能无脑 replace("?", "%s") ——
字符串字面量里的问号会被误替换，
产生静默错误的查询（不报错，只是结果不对）。

正确做法：先屏蔽字符串字面量与注释，只替换代码区的问号。
这与 validate_sql 里先去注释再检测关键字是同一类问题：
对 SQL 做文本处理时，必须先区分代码与数据。
''')
h3('知识点：迁移脚本的设计要点')
table(
    ['要点', '做法', '为什么'],
    [
        ['幂等', '先 TRUNCATE 再写入', '重复执行会导致数据翻倍，是最常见的翻车方式'],
        ['分批提交', '500 行一批', '一次性插百万行会撑爆内存与事务日志'],
        ['列结构校验', '迁移前比对源/目标列名',
         'schema 不一致时宁可直接失败，也不要写入错位数据'],
        ['金额汇总校验', '比对 SUM(gmv) 而非只比行数',
         '★ 抓浮点精度问题：行数对了但金额差一点，只有汇总才暴露'],
    ],
)
info_box('没有 PostgreSQL 也能验证适配逻辑',
         '这是本步最重要的工程判断。CI 环境不会跑 PG，但不代表适配层没写错。'
         '所以把可离线验证的部分单独覆盖：方言转换、DDL 转换、配置解析与脱敏。'
         '而真的连上能不能跑，用一个 pg 标记的测试覆盖 —— '
         '有 DATABASE_URL 时才跑，否则 skip。'
         '这样 CI 全绿有意义，真实 PG 验证也不会被漏掉。')
h3('★ 知识点：评测为什么要分两层')
code('''
离线层（不需要 LLM，秒级、免费、100% 可复现）：
    路由准确率 / 工具准确率 / 数值准确率 / 质控准确率 / 并行策略 / 认证权限
    → 能进 CI 每次提交都跑
    → 覆盖数字可信度这个项目最核心的价值主张

在线层（需要 LLM，分钟级、花钱、有随机性）：
    端到端行为 / 陷阱抵御 / 数字保真度
    → 用 pytest marker 隔离，只在有 API Key 时跑

★ 关键设计：离线层的准确率本身就是有价值的指标 ——
  它衡量的是确定性骨架的健康度。改 prompt、换模型、
  重构指标层时，离线准确率下降就说明出了问题，
  而且能立刻定位到是哪一类能力退化。
''')
h3('知识点：加权准确率')
para('陷阱题的权重是普通题的 1.5~3 倍。为什么？'
     '因为会不会被归因陷阱带偏比能不能取对数更能区分系统能力。'
     '只看未加权准确率，会让取数全对但归因全错的系统看起来还不错。')
h3('怎么验证')
code('''python scripts/migrate_to_postgres.py --dry-run   # 迁移干跑
python scripts/migrate_to_postgres.py           # 正式迁移（含三层校验）
pytest tests/test_db_adapter.py -v              # 适配层验收
pytest tests/ --accuracy-report --min-accuracy 0.9   # 评测 + 准确率门槛''')

# ===========================================================================
# 第四章
# ===========================================================================
doc.add_page_break()
h1('第四章  踩坑复盘：16 个真实 Bug')

callout('为什么这一章最有价值',
        '这些 Bug 都是开发过程中被验收脚本或真实端到端调用抓出来的。'
        '它们的共同特点是：代码看起来完全正确，只有跑起来才知道错了。'
        '面试时讲这些，比背概念有说服力得多。')

h2('4.1 集成层问题（离线测试抓不到，必须真实调用）')
para('这一类问题最隐蔽：所有离线验收全绿，但真实调用必崩。', color=GRAY)

h3('Bug 1：所有结构化输出全部失效')
rich(('现象：', True), ('规划器、意图兜底、需求解析、周复盘全部失败，'
                     '但基础对话完全正常。', False))
rich(('根因：', True), ('LangChain 的 with_structured_output 默认发送 '
                     'response_format: json_schema，而 DeepSeek 不支持该类型，'
                     '直接返回 400。', False))
rich(('迷惑性：', True), ('基础对话能正常返回，所以第一反应不会怀疑到这个方向。'
                      '失败被各处 except 兜底吞掉，'
                      '表现为 Agent 什么都不做就说结束了。', False))
rich(('修法：', True), ('封装 structured_llm()，'
                     '显式指定 method="function_calling"，'
                     '并做三级回退 + 缓存探测结果。', False))
callout('教训', '第三方 API 的能力边界必须用真实请求探测。'
              '代码能构造对象不等于服务端会接受。'
              '本项目实际上一度只有确定性部分能用。')

h3('Bug 2：CLI 一跑就崩（回调签名冲突）')
rich(('现象：', True), ('TypeError: print_progress() missing 1 required '
                     'positional argument', False))
rich(('根因：', True), ('引擎里写 on_progress(msg)（1 个参数），'
                     '循环里写 on_progress(round_no, msg)（2 个参数），'
                     '两处约定不一致，而回调签名从来没有被定义成契约。', False))
rich(('修法：', True), ('抽出 safe_progress()，用 inspect 探测参数个数自适应两种签名；'
                     '并让回调异常不影响主流程。', False))
callout('教训', '可选参数的回调必须被测试覆盖 —— 否则它等同于没有。'
              '222 项验收全绿却漏掉了这个必然崩的 bug，'
              '就因为没有任何测试传过 on_progress。')

h3('Bug 3：工具因参数名报错，白跑一整轮')
rich(('现象：', True), ('scan_stockout() got an unexpected keyword argument '
                     'start_date', False))
rich(('根因：', True), ('Registry.execute 把 kwargs 直接透传给函数，'
                     '完全绕过了用函数签名自动生成的 Pydantic schema —— '
                     '等于校验层建了却没用。模型传 start_date 而签名是 start。', False))
rich(('修法：', True), ('执行前加 schema 校验 + 别名映射（静默纠正）；'
                     '真正非法的参数名给出正确参数名列表。', False))
para('效果：工具错误从 2 个降到 0 个，轮次从 3 降到 2。', bold=True, color=GREEN)

h3('Bug 4：pytest 钩子放错文件，命令行选项不生效')
rich(('现象：', True), ('pytest --accuracy-report 报 unrecognized arguments，'
                     '退出码 4（usage error）。', False))
rich(('根因：', True), ('把 pytest_addoption 写在测试模块 tests/test_eval.py 里。'
                     'pytest 在解析命令行参数时就要知道有哪些自定义选项，'
                     '而那时它还没导入测试模块。', False))
rich(('修法：', True), ('钩子必须放在 conftest.py —— 它是 pytest 提前加载的文件。', False))
callout('教训', '这类问题的特征是：代码看起来完全正确，但钩子从未被调用。'
              '因为它被放在了错误的生命周期阶段。')

h2('4.2 数据层问题（换库时最容易翻车）')

h3('Bug 5：SQL 掩码长度不一致导致 IndexError')
rich(('现象：', True), ('在含中文的 SQL 上，占位符转换直接抛 '
                     'IndexError: string index out of range。', False))
rich(('根因：', True), ('_strip_literals 用于把字面量屏蔽成等长空白，'
                     '但行注释循环把结尾的换行符也吞掉了，'
                     '然后又在换行处补了一个空格 —— 掩码比原文多一个字符。'
                     '调用方按下标逐字符比对，于是越界。', False))
rich(('修法：', True), ('注释体替换成空格，但换行符留给主循环处理；'
                     '并加显式断言：掩码长度必须与原文完全一致。', False))
info_box('为什么加断言而不是祈祷',
         '这类长度必须相等的不变量，用一个显式检查代替默默假设。'
         '一旦被破坏就立刻报出清晰的内部错误，'
         '而不是在下游某个位置抛出难以定位的越界异常。')

h3('Bug 6：完全编造的列名给不出修正建议')
rich(('现象：', True), ('模型写 SELECT bad_col 时报错，但 expected_columns 是空列表 —— '
                     '模型只知道列名不存在，不知道该用什么，只能继续瞎猜。', False))
rich(('根因：', True), ('高频错误（product_name/date/amount）有精确映射表，'
                     'difflib 模糊匹配也能覆盖拼写接近的情况。'
                     '但完全不相干的名字两者都给不出建议。', False))
rich(('修法：', True), ('兜底：给不出建议时列出全部可用列名（23 个）。'
                     '宁可多占几十个 token，'
                     '也不要让模型在不知道该用什么的状态下瞎猜。', False))

h3('Bug 7：PostgreSQL 的 REAL 是 4 字节（精度静默丢失）')
rich(('问题：', True), ('SQLite 的 REAL 是 8 字节 IEEE 浮点，'
                     '而 PostgreSQL 的 REAL 是 4 字节（约 7 位有效数字）。'
                     '直接复用同一份 DDL 会让金额精度静默丢失：'
                     '123012.88 可能变成 123012.875。', False))
rich(('危害：', True), ('行数对了、结构对了、不报错 —— 但所有金额指标都偏一点。'
                     '单条数据上只差几分钱毫不可疑，只有汇总求和时才暴露。', False))
rich(('修法：', True), ('schema.ddl_for() 把 REAL 转成 DOUBLE PRECISION；'
                     '迁移校验里必须比对 SUM(gmv) 这类金额汇总，'
                     '只比对行数是发现不了的。', False))

h2('4.3 编排与容错问题')

h3('Bug 8：工具连续失败 6 轮，降级从未触发')
rich(('现象：', True), ('工具连续失败 6 轮，degraded 始终为 False，'
                     '最后拿着成功的结果去汇总，而实际什么数据都没有。', False))
rich(('根因：', True), ('失败记录被塞进了 new_data，'
                     '导致 if not new_data 这个条件恒为假 —— '
                     '降级分支根本执行不到。', False))
rich(('修法：', True), ('成功数据与失败记录分开统计（new_data / failed_data）。', False))
callout('教训', '失败路径的分支必须和成功路径一样被测试覆盖。'
              '这个 bug 存活了很久，因为一直在测工具成功的场景。')

h3('Bug 9：压缩率 108%（越压越大）')
rich(('现象：', True), ('所谓的压缩之后，内容反而变长了。', False))
rich(('根因：', True), ('每轮无条件加上「第N轮｜工具：xxx」的头。'
                     '对本来就简短的结论，这个头比省掉的内容还长。', False))
rich(('修法：', True), ('加净收益检查；但轮次编号必须保留 —— '
                     '它是审计追溯的唯一线索，只有 4 个字符。', False))

h3('Bug 10：压缩把 35 格砍成 8 格')
rich(('现象：', True), ('模型报告工具只返回了 35 格中的前 8 项，'
                     '并据此说自己无法给出完整分析。', False))
rich(('根因：', True), ('压缩层固定 max_items=8。数据其实完整拿到了，'
                     '是被压缩层自己砍掉的 —— 却让模型以为工具能力不足。', False))
rich(('修法：', True), ('改为按规模自适应：≤12 项全量保留，'
                     '大列表保留 20 项并注明省略数量。', False))

h3('Bug 11：会话淘汰上限失效')
rich(('现象：', True), ('内存会话存储设置了 max_sessions=3，'
                     '但实际能存到 4 个 —— 上限从来没守住。', False))
rich(('根因：', True), ('_evict() 在插入之前调用：淘汰时是 3 个，'
                     '插入后就变 4 个。边界检查的时机不对。', False))
rich(('修法：', True), ('先插入/更新，再淘汰。'
                     '并把 stats() 里的副作用去掉（只读观测接口不该删数据）。', False))
callout('教训', '这类边界检查时机不对的 bug 很难靠读代码发现，'
              '因为它只在恰好达到上限时才暴露。')

h2('4.4 路由与规则问题')

h3('Bug 12：需求文档被判为数据分析')
rich(('现象：', True), ('「分析一下这个需求文档」被推去跑数据指标工具，'
                     '方向完全错了。', False))
rich(('根因：', True), ('为了实现含分析/归因必须走工具的硬约束，'
                     '把需求类判定放在了硬约束之后，'
                     '于是需求类请求被硬约束抢走。', False))
rich(('修法：', True), ('需求类判定前移；硬约束只做单向兜底'
                     '（只禁止 direct_generate，不抢其他意图的路由）。', False))
callout('教训', '硬约束要做成单向的。反向强制（含分析就必须是数据分析）'
              '会把其他意图错误地拉过来，而且更贵（白跑几轮工具循环）。')

h3('Bug 13：两套模式表宽严不一致，导致多做一次 LLM 调用')
rich(('现象：', True), ('「分析一下上周的经营情况」规则未命中，'
                     '落到 LLM 兜底判定。', False))
rich(('根因：', True), ('has_data_signal 能识别情况，'
                     '但 REPORT_PATTERNS 只有情况如何 —— 两套表覆盖不一致。'
                     '虽然硬约束兜住了（不会误判成免工具），'
                     '但白多了一次 LLM 调用。', False))
rich(('修法：', True), ('补齐 REPORT_PATTERNS；并加一条保守兜底：'
                     '含数据信号但未命中具体规则时，直接走分析报告。', False))
info_box('原则：同一个概念的表之间不应该有宽严差异',
         '这是 bug 的温床。两个函数判断同一件事（这句话要不要数据），'
         '却用不同的模式列表，必然会出现一个认、一个不认的缝隙。')

h2('4.5 安全与配置问题')

h3('Bug 14：.env.example 里写着真实 API Key')
rich(('现象：', True), ('模板文件 .env.example 中包含真实可用的 DeepSeek Key。', False))
rich(('危害：', True), ('.env.example 通常会被提交到版本库 —— 等于公开泄露密钥。'
                     '而且项目当时没有 .gitignore，'
                     '一旦 git init && git add . 就会把 .env 一起提交。', False))
rich(('修法：', True), ('模板清空并加警告注释；补齐 .gitignore / .dockerignore；'
                     '验收里加断言：模板文件不得含真实密钥。', False))
callout('教训', '安全审计要覆盖看起来不重要的文件。'
              '模板文件是最容易被忽略的泄露渠道 —— '
              '因为开发时为了方便，直接把能跑通的配置粘进去了。')

h3('Bug 15：登录审计里 user 字段为空')
rich(('现象：', True), ('审计记录显示 user 为空，无法追溯是谁登录失败的。', False))
rich(('根因：', True), ('登录接口是唯一在认证之前就要写审计的地方，'
                     '此时中间件还没有绑定用户上下文。', False))
rich(('修法：', True), ('登录时显式绑定请求上下文'
                     '（成功用真实身份，失败用尝试的用户名）。', False))

h3('Bug 16：pytest 报告跨模块丢失（静默无输出）')
rich(('现象：', True), ('准确率报告始终为空，但没有任何报错 —— '
                     '测试全绿，只是报告不打印。', False))
rich(('根因：', True), ('pytest 把 conftest.py 作为顶层模块 conftest 导入，'
                     '而测试模块用 from tests.conftest import ... 导入 —— '
                     '这是两个不同的模块对象，各自持有一份 REPORTS 列表。'
                     '测试注册的报告在汇总时完全看不到。', False))
rich(('修法：', True), ('不依赖同一个模块对象，改用进程级共享状态：'
                     '报告落盘到 JSON 文件，汇总时从磁盘读回。'
                     '顺带留下可归档的产物给 CI 用。', False))
callout('教训', '静默失败比报错更危险。'
              '报告为空但测试全绿，这种问题如果没人核对输出内容，会一直存在。'
              '所以关键产物必须有断言（例如报告非空）。')

h2('4.6 一个关于「修复过程本身」的教训')
callout('★ 不要用正则事后修复引号嵌套问题',
        '生成本学习文档的脚本里，中文正文天然会用到引号，'
        '而 Python 字符串也用引号定界 —— 于是出现语法错误。'
        '我先后试了 4 个版本的正则修复脚本，'
        '每次都把真正的字符串定界符也改了一部分，'
        '最后一轮误改 526 处，文件彻底损坏只能重写。'
        '根本原因：中文文本里的 ASCII 引号与 Python 定界符本质歧义，'
        '正则无法区分内容里的引号和代码里的引号。'
        '正确做法：不要靠事后修复，要靠编码约定从源头避免 —— '
        'Python 字符串一律用单引号，中文引用一律用「」。')

# ===========================================================================
# 第五章
# ===========================================================================
doc.add_page_break()
h1('第五章  知识点总结矩阵')

h2('5.1 Agent 架构类')
table(
    ['知识点', '在本项目中的体现', '关键理解'],
    [
        ['意图识别与条件路由', 'intent.py：规则优先 + LLM 兜底 + 出口拦截',
         '路由的本质是决定用不用工具，这是成本与准确率的分水岭'],
        ['动态 Tool Registry', 'registry.py：能力标签 + 按需装配',
         '工具不是越多越好：给 3~5 个相关工具比给 13 个，选错率低得多'],
        ['显式循环 vs 现成 Agent', 'loop.py 手写循环',
         '需要在工具结果和模型之间插入处理逻辑时，必须用显式循环'],
        ['上下文工程', 'context/：9 部分组装 + 压缩滚动',
         '硬约束要靠前，历史要近详远略'],
        ['结构化输出', 'schemas.py + structured_llm',
         '自由文本无法入库、无法对比、无法审计、无法判断对错'],
        ['并行执行', 'parallel.py：自适应策略',
         '并行不总是更快 —— 收益 = (N-1)×单耗时 − 线程池开销'],
    ],
)

h2('5.2 可靠性工程类')
table(
    ['知识点', '在本项目中的体现', '关键理解'],
    [
        ['错误分类与重试', 'registry：transport/data/permanent',
         '只重试真正瞬时的错误；确定性错误重试是纯浪费'],
        ['超时保护', '_call_with_timeout：线程 + join',
         'Windows 不能用 signal，必须用线程方案'],
        ['优雅降级', 'engine：明确声明未经数据验证',
         '带免责声明的答案 > 500 错误；但要禁止编造数字'],
        ['自适应容错', '自适应压缩、别名映射、签名探测、并行策略',
         '把让调用方适应改成让被调用方自适应，能消除一整类 bug'],
        ['纵深防御', 'SQL 五层护栏；路由三层拦截；默认拒绝',
         '入口可以宽松，出口必须严格'],
        ['可观测性', '结构化日志 + request_id + 审计',
         '出问题能定位到是哪一层、哪一轮、哪次调用'],
    ],
)

h2('5.3 数据与业务类')
table(
    ['知识点', '在本项目中的体现', '关键理解'],
    [
        ['指标口径字典', 'metrics/definitions.py',
         '口径不写成代码，Agent 每次算的数字都无法互相比对'],
        ['归因污染', 'quality/checks.py：6 类检测',
         '转化率下跌有 4 种病因，详情页问题必须是最后一项候选'],
        ['变点检测', 'price_change_scan',
         '枚举切分点比与全期中位数比稳健得多'],
        ['事实表 vs 维表', 'schema.py',
         '维表没有日期列；跨表 JOIN 必须先搞清粒度'],
        ['笛卡尔积陷阱', 'cross_breakdown 的重构方案',
         '粒度不同直接 JOIN 会放大数值，且放大后数字看起来合理'],
        ['相对时间解析', 'build_time_block',
         '只给今天是X不够，模型做日期心算和算数一样不可靠'],
        ['时间错位', 'detect_time_mismatch',
         '最危险的不是报错，是格式正常但对应错时间段的报告'],
    ],
)

h2('5.4 数据层与迁移类（v2.0 新增）')
table(
    ['知识点', '在本项目中的体现', '关键理解'],
    [
        ['方言适配层', 'db/adapter.py',
         '抽象收益小于复杂度时不要抽象；但方言差异必须封装，'
         '否则会长进业务代码'],
        ['只读的服务端强制', 'SQLite mode=ro / PG read_only 会话变量',
         '应用层校验可被绕过，服务端限制不会'],
        ['同名不同类型', 'REAL：SQLite 8 字节 vs PG 4 字节',
         '不报错、不崩，只让数据悄悄变形 —— 最危险的不兼容'],
        ['占位符转换', '_replace_outside_literals',
         '对 SQL 做文本处理必须先区分代码与数据（字面量、注释）'],
        ['迁移幂等', '先 TRUNCATE 再写入',
         '不幂等的迁移脚本第二次运行会让数据翻倍'],
        ['迁移校验三层', '行数 / 列结构 / 金额汇总',
         '只比行数发现不了精度问题'],
        ['无依赖验证', 'pg 标记测试 + skipif',
         '外部服务不可用不等于逻辑没法验证'],
    ],
)

h2('5.5 安全与合规类（v2.0 新增）')
table(
    ['知识点', '在本项目中的体现', '关键理解'],
    [
        ['三层权限模型', '认证 / 授权 / 数据权限',
         '数据权限必须下沉到数据层，接口层过滤容易漏分支'],
        ['默认拒绝', '未配置路径仅管理员可访问',
         '新加接口忘配权限时，结果是访问不了（可发现）'
         '而非谁都能访问（静默漏洞）'],
        ['慢哈希', 'PBKDF2 20 万轮',
         '慢是特性：直哈希太快，暴力破解成本低'],
        ['常量时间比较', 'hmac.compare_digest',
         '用 == 会因提前返回泄露信息（时序攻击）'],
        ['防用户枚举', '用户不存在也做假哈希校验',
         '响应时间会泄露该用户是否存在'],
        ['限流双层', '速率限制 + 每日配额',
         '只有速率限制防不住细水长流式的成本打穿'],
        ['审计独立于日志', '独立追加型记录',
         '日志会滚动删除，审计用于合规必须完整留存'],
        ['密钥脱敏', '_mask_url + 启动警告',
         '任何对外输出的配置描述都必须先脱敏'],
    ],
)

h2('5.6 评测与工程实践类（v2.0 新增）')
table(
    ['知识点', '在本项目中的体现', '关键理解'],
    [
        ['分层验收', '五套脚本 + pytest',
         '先证明确定层正确，再上模型 —— 否则错误会被模型放大'],
        ['假 LLM 测试', 'verify_workflow.py 离线模式',
         '流程 bug 和模型质量问题是两类，混在一起永远分不清'],
        ['评测两层', 'offline（免费）/ live（花钱）',
         '离线层能进 CI 每次跑，且它衡量的正是核心价值主张'],
        ['加权准确率', '陷阱题权重 1.5~3x',
         '只看未加权准确率，会让取数全对但归因全错的系统看起来还不错'],
        ['无依赖验证', 'test_db_adapter.py',
         '没有 PG 也能验证适配逻辑，有 PG 时自动跑真实集成测试'],
        ['CI 门槛', '--min-accuracy 0.9',
         '改 prompt 导致能力退化会在合并前被拦住，而不是上线后靠反馈发现'],
        ['陈旧状态污染', 'pytest_sessionstart 清理报告',
         '落盘的报告会累积，第二次运行会重复计数 —— 必须显式清理'],
        ['静默失败', '报告为空但测试全绿',
         '关键产物必须有断言，否则没输出会被当成没问题'],
        ['编码约定优于事后修复', 'generate_doc.py 的引号约定',
         '正则无法区分内容里的引号与代码里的引号 —— '
         '本质歧义必须靠约定避免'],
    ],
)

# ===========================================================================
# 第六章
# ===========================================================================
doc.add_page_break()
h1('第六章  面试题库（66 题）')
para('按主题分组。每题给出参考答案要点，建议先自己答一遍再看。', color=GRAY)

h2('6.1 架构设计类（14 题）')

qa('Q1. 请介绍一下这个项目的整体架构。',
   '按数据流讲：认证鉴权 → 意图识别（规则优先）→ 条件路由（是否用工具）→ '
   '数据预处理（算好同比/环比 + 质控，注入 Context）→ '
   '工具循环（选工具 → 自适应并行执行 → 预处理 → 子结论 → 滚动）→ '
   '汇总生成（带范文模板）→ 评审（确定性规则 + LLM 语义，不通过则回环）→ '
   '降级（工具全挂时声明未经数据验证）。\n'
   '核心设计原则一句话：把确定的交给代码，把不确定的交给模型 —— '
   '数字、时间、聚合由代码保证正确，模型只负责决定看什么和如何解读。')

qa('Q2. 为什么不让 LLM 自己算同比环比？',
   '两个原因：\n'
   '① 模型做算术会错，而且错得看不出来 —— '
   '它给出的报告格式仍然完全正确，用户无法察觉；\n'
   '② 同样的输入两次可能算出不同结果，不可复现。\n'
   '所以设计成：工具返回原始数据 → 代码算好环比 → 注入 Context，'
   '模型只做解读。有一层专门的 preprocess.py 负责这件事。')

qa('Q3. 意图识别为什么用规则而不是让模型判断？',
   '成本、延迟、稳定性三个维度：规则是微秒级零成本，模型是秒级有成本；'
   '规则结果稳定可复现，模型有随机性。\n'
   '实测规则命中率 100%，绝大多数请求根本不需要模型判意图。'
   '所以设计成规则优先、LLM 兜底，只有真正模糊的才交给模型。')

qa('Q4. 什么是单向硬约束？为什么要做成单向的？',
   '规则：含数据信号的请求（为什么/归因/分析/下滑/指标等）'
   '绝不允许判为 direct_generate；不含数据信号才允许。\n'
   '单向的原因是两个方向的错误代价不对称：'
   '误判为免工具 → 模型凭记忆编数字、输出看似专业但完全错误且无警示；'
   '误判为需工具 → 只是多查一次数据。所以宁可多查，不可编造。\n'
   '反例说明为什么不能反向强制：如果规定含分析就必须走数据分析，'
   '那「分析一下这个需求文档」会被推去跑指标工具，方向完全错了。')

qa('Q5. 硬约束为什么要在出口再兜一道？',
   '因为 direct_generate 的判定可能来自三个地方，每个都可能出错：\n'
   '① 规则表会随新写法不断漏网；\n'
   '② LLM 兜底会误判；\n'
   '③ 强信号优先级太高可能压过数据需求。\n'
   '与其在三处分别堵，不如在唯一出口统一兜底 —— 这是纵深防御。'
   '好处是以后无论新增哪条规则、换成哪个模型，这条底线都不会被突破。')

qa('Q6. 为什么用显式工具循环而不用 LangChain 的 create_agent？',
   '因为需要在工具执行和模型看到结果之间插入预处理步骤 —— '
   '算好同比/环比、补贡献占比、抽关键数字。\n'
   'create_agent 的内部循环插不进这一步：'
   '工具结果会以原始形态直接进入消息历史，模型还得自己算环比，'
   '等于放弃了数字可信度。\n'
   '判断标准：当流程本身是确定性的，图编排或现成 Agent 带来的抽象是负担；'
   '它们的价值在于让模型决定下一步走哪的场景。')

qa('Q7. 项目里有 13 个工具，怎么决定给模型哪几个？',
   '按意图动态装配，靠工具的能力标签（capabilities）筛选。\n'
   '比如需求解析只需要 schema + sql（看表结构、评估影响面），不需要 metric；'
   '归因则需要 schema + metric + drilldown + quality + sql。\n'
   '原因：给模型 13 个工具它容易选错；给 3~5 个相关工具，选对率显著提高。')

qa('Q8. 上下文里都放了什么？顺序有讲究吗？',
   '9 个部分：角色设定、业务规则、时间上下文、指标口径字典、业务范文模板、'
   '本次任务、数据预处理结果、质控提示、滚动前轮结论。\n'
   '顺序有讲究：硬约束（业务规则、口径字典）必须放在范文和数据之前 —— '
   '模型对靠前的内容服从度更高，把纪律放在最后容易被淹没在数据里。')

qa('Q9. 长对话上下文膨胀怎么解决？',
   '三层策略：\n'
   '① 近详远略压缩：最近 1 轮保留关键数字+结论全文（500 字），'
   '第 2~3 轮保留摘要（260 字），更早的只留要点（110 字）；\n'
   '② 数字锚点保留：压缩时可以砍描述，但关键数字必须留；\n'
   '③ 自适应截断：≤12 项的列表全量保留，大列表保留前 20 项并注明省略数量。\n'
   '实测压缩率 69%，且轮次编号始终保留（审计追溯需要）。')

qa('Q10. 工具调用失败了怎么处理？',
   '分三层：\n'
   '① 错误分类：transport（超时/网络/限流）可重试，指数退避；'
   'data（列名错/无数据）不重试，因为重试同样失败；'
   'permanent（权限/Key）不重试，直接降级。\n'
   '② 错误可自我修正：把裸报错翻译成带正确列名建议的提示。\n'
   '③ 降级：工具全挂时明确声明未经数据验证，禁止编造数字。')

qa('Q11. 怎么保证 Agent 输出的数字是对的？',
   '四层保证：\n'
   '① 计算归代码：所有指标由 metrics/compute.py 计算，模型不许自算；\n'
   '② 报告骨架由代码渲染：正文的表格和数字直接来自指标层；\n'
   '③ 证据强制：每条 Insight 必须带 evidence 字段，没有证据直接判不通过；\n'
   '④ 评测层验证：test_eval.py 里的数值准确率必须 100%。')

qa('Q12. 项目从原型到企业级，最大的缺口是什么？',
   '认证鉴权。原型版做了五层 SQL 护栏、单向硬约束、阻断纪律，'
   '但任何人 curl 一下就能拿到全部经营数据 —— '
   '前面所有护栏都失去意义，因为它们保护的是一扇没有门的房间。\n'
   '安全边界比功能完善度更重要：认证是唯一一个'
   '不做就等于前面全部白做的缺口。\n'
   '其余缺口（限流、会话外置、结构化日志、审计）虽然也重要，'
   '但都不会让已有能力失效。')

qa('Q13. 三层权限模型是什么？为什么数据权限要单独一层？',
   '① 认证：你是谁 —— JWT + Bearer Token\n'
   '② 授权：你能用哪些接口 —— 角色 → 接口权限矩阵\n'
   '③ 数据权限：你能看哪些数据 —— 角色 → 类目/渠道白名单\n'
   '为什么第 ③ 层单列：很多系统做了前两层就以为安全了。'
   '但如果一个只看美妆类目的运营能通过 /api/products 看到全部类目数据，'
   '权限体系就是纸糊的。\n'
   '而且数据权限必须下沉到数据访问层，不能在接口层过滤 —— '
   '接口层过滤容易在某个分支漏掉，导致越权泄露。')

qa('Q14. 如果让你重新设计这个项目，你会改什么？',
   '诚实回答三个方向：\n'
   '① 数据模型：一开始就该在事实表里放价格快照列和商品×渠道粒度，'
   '而不是后来补 —— 数据模型缺列无法用算法补偿；\n'
   '② 验收策略：应该更早引入真实端到端测试。'
   '我们有 222 项离线验收全绿，却漏掉了三个集成层致命 bug'
   '（结构化输出、回调签名、参数名），因为它们只有真实调用才暴露；\n'
   '③ 数据库选型：SQLite 适合原型但不适合生产（无并发写），'
   '应该在设计阶段就把数据访问做成可替换的 —— '
   '这次的适配层是补做的，如果一开始就分层会省很多事。')

h2('6.2 技术细节类（24 题）')

qa('Q15. 什么是归因污染？举例说明。',
   '指把 A 造成的现象错误归因为 B。电商场景的典型例子：\n'
   '转化率跌了 20% → 天真 Agent 说详情页不行，建议优化主图；\n'
   '真相可能是：爆款缺货 / 上周涨价 / 大促流量退了 / 付费流量占比升高。\n'
   '这四种情况的应对动作完全相反 —— 优化详情页对缺货毫无用处。\n'
   '所以质控层会在下结论前把这些干扰因素标出来，作为硬约束注入 prompt。')

qa('Q16. 缺货为什么会导致转化率下跌？',
   '用户照样点进来（流量不掉），但看到无货就走了，转化率断崖下跌。\n'
   '看数据的人会以为是页面或价格问题，因为流量正常、'
   '看起来人来都来了为什么不买。\n'
   '只有库存表能证明这一点，所以事实表必须有 stockout_flag，'
   '而且转化率排查的第一步就是调 scan_stockout。')

qa('Q17. 转化率下跌的排查顺序是什么？为什么是这个顺序？',
   '顺序：缺货 → 改价/比价 → 流量结构 → 大促 → 商品自身（详情页/评价）。\n'
   '为什么详情页排最后：前三项都是外部或结构性原因，'
   '先排除它们才能确定问题真的出在商品本身。\n'
   '从概率上，缺货和改价比详情页问题常见得多；从代价上，'
   '把缺货误判为详情页问题会让团队去优化没坏的东西。')

qa('Q18. 什么是变点检测？为什么不用平均值比较？',
   '变点检测是找出时间序列中发生结构性变化的那个点。'
   '本项目用它发现哪天改价了。\n'
   '做法是枚举所有切分点，分别算前段和后段的中位数，'
   '取相对差异最大的那个点。\n'
   '不用平均值是因为它不抗离群值（一次大促就打偏了）；'
   '不用与全期中位数比是因为当天数分布不均时，'
   '中位数会漂移到变化后的区间，导致算出来涨幅是 0 —— 这是实际踩过的坑。')

qa('Q19. 什么是上台阶形态？为什么线性斜率检测不出来？',
   '加预算的真实形态往往是：花费跳升一级后走平，ROI 掉一档后也走平。\n'
   '此时线性回归的斜率约等于 0，会被误判为没有恶化，'
   '但业务上这恰恰是最该报警的形态（换挡后停在低 ROI 平台）。\n'
   '所以判据要同时用水平位移（抓台阶）和线性斜率（抓渐进恶化）。'
   '教训：度量必须匹配形态。')

qa('Q20. 为什么 ROI 不能用商品表算？',
   '因为只有渠道流量表（fact_daily_traffic）有 ad_spend 字段。\n'
   '如果把商品事实表和流量表 JOIN 起来算 ROI，两张表粒度不同'
   '（日期×商品 vs 日期×渠道），会产生笛卡尔积，'
   'GMV 被放大 N 倍，ROI 虚高。\n'
   '所以把这条写进了指标口径字典，作为给模型看的硬规矩。')

qa('Q21. 事实表和维表的区别？为什么维表没有日期列？',
   '事实表记录发生了什么（可度量的事件），有时间和粒度；'
   '维表记录是什么（描述性属性），是相对静态的。\n'
   'dim_product 描述商品的固有属性，不随时间变化，所以没有日期列。\n'
   '要按时间筛选必须 JOIN 事实表 —— 这一点模型最容易搞错，'
   '所以在 schema 说明里显式写了不存在名为 date 的列。')

qa('Q22. 交叉维度为什么会产生笛卡尔积？你怎么处理的？',
   '两张事实表只能按 stat_date 关联，没有 product×channel 粒度的关联键。'
   '同一天每个商品 × 每个渠道组合都成立，于是产生笛卡尔积。\n'
   '现象很隐蔽：总量放大 N 倍，且同一类目在各渠道的数值完全相同。\n'
   '处理：不直接 SUM，而是用「类目结构 × 渠道结构」重构。'
   '这样各渠道合计等于真实渠道值，类目占比也真实，'
   '但单个交叉格是估算值。\n'
   '关键是在返回值里显式标注了方法是结构重构（非直接联表）'
   '和不可用于汇报精确数字 —— 宁可让工具显得弱一点，'
   '也不能给一个会被采信的错数字。')

qa('Q23. 什么是时间错位？为什么它特别危险？',
   '指用户问的时间段和系统实际回答的时间段不一致。\n'
   '例：今天 2026-10-09，用户问上周，但数据止于 2025-08-29，'
   '系统返回数据中最后一个完整周，相差 406 天。\n'
   '危险在于：报告标题会写成「上周转化率归因（2025-08-18~08-24）」，'
   '格式完全正常、内容详实，用户根本看不出这是一年前的行情。\n'
   '这比返回空结果危险得多 —— 空结果一眼能看出问题，'
   '错位报告会被直接采信。')

qa('Q24. 只给模型注入今天是X月X日够不够？',
   '不够。模型还需要做日期心算（今天 8/25，上周一是 8/18），'
   '而模型算日期和算数字一样不可靠。\n'
   '所以把上周/上上周/最近 7 天/本月全部预先解析成绝对日期，'
   '模型只需引用不需换算。同时附上数据库实际数据范围，'
   '让它能判断问的时间段有没有数据。\n'
   '这是计算归代码原则在时间维度的应用。')

qa('Q25. SQL 护栏你会怎么设计？',
   '五层纵深防御：\n'
   '① 语法白名单：只允许 SELECT / WITH 开头；\n'
   '② 关键字黑名单：拒绝 INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH；\n'
   '③ 表名白名单：从 FROM/JOIN 提取表名做校验；\n'
   '④ 强制 LIMIT：无 LIMIT 自动补 500；\n'
   '⑤ 只读连接：数据库层强制 —— 这是最关键的一层，'
   '因为它在驱动或服务端限制，即使前四层被绕过也无法写入。\n'
   '另外还要拒绝多语句（分号后还有内容），防止注释拼接绕过。')

qa('Q26. 工具报错了，怎么让模型自己改对？',
   '关键是错误信息要包含正确做法。'
   '裸报错 no such column: p.product_name '
   '只说错了、不说改成什么，模型只能瞎猜，会白跑好几轮。\n'
   '所以做了 friendly_sql_error()：识别错误模式 → '
   '用 difflib 模糊匹配给出最接近的真实列名 → 附上针对性提示。\n'
   '还有一个兜底：如果模型编了一个完全不相干的名字（difflib 也给不出建议），'
   '就列出全部可用列名（23 个）—— 宁可多占几十个 token，'
   '也不要让模型在不知道该用什么的状态下瞎猜。')

qa('Q27. 能力标签驱动的工具装配具体怎么实现？',
   '注册时打标签：@registry.register(capabilities=["metric"])；\n'
   '装配时按标签筛选：registry.select(capabilities=["metric", "quality"])；\n'
   '然后 build_langchain_tools() 把它们转成 LangChain 工具（含容错包装）。\n'
   'Function Schema 由函数签名自动生成（inspect + pydantic.create_model），'
   '所以新增工具只要写一个函数 + 一个装饰器，扩展成本接近于零。')

qa('Q28. Function Schema 自动生成踩过什么坑？',
   '两个：\n'
   '① 把 **kwargs 当成了一个名为 kw 的必填字符串字段，'
   '导致任何 def f(**kw) 形式的工具都报 Field required。'
   '修法是跳过 VAR_KEYWORD / VAR_POSITIONAL 可变参数。\n'
   '② 参数名没有引导，模型自然地把 start 写成 start_date。'
   '修法是在字段 description 里直接写明务必用 start，'
   '并在执行层加别名映射兜底。')

qa('Q29. 什么是阻断纪律？为什么需要它？',
   '当质控返回 has_blocking=True（如缺货）时，'
   '本期数据不可用于因果归因。阻断纪律要求输出必须是三段结构：\n'
   '① 事实陈述（只描述数据，不解释原因）；\n'
   '② 待核查清单（要核查什么 + 用什么数据 + 由谁确认）；\n'
   '③ 修复优先（先解决阻断问题，再谈经营策略）。\n'
   '同时禁止出现策略有效或无效类因果词汇。\n'
   '为什么需要：在不可信的数据上做因果推断，建议方向可能完全错误。'
   '此时不下结论才是正确的专业输出，不是能力不足。')

qa('Q30. 阻断纪律的评审为什么要检查结构而不只是禁用词？',
   '因为只查禁用词的话，一份空洞的、什么都没说的输出同样能通过 —— '
   '它对用户毫无价值。\n'
   '所以要做词汇 + 结构双重要求：既查有没有出现因果词，'
   '也查有没有不可用于因果归因的声明、有没有待核查清单、'
   '有没有指出应先修复阻断问题、有没有提及 error 级的质控问题。')

qa('Q31. 评审为什么分确定性和 LLM 两层？',
   '因为两类问题的性质不同：\n'
   '· 禁用词、字段是否为空、结构是否完整 —— 这些能用代码 100% 判定，'
   '零成本零漏判，绝不该交给 LLM；\n'
   '· 逻辑是否自洽、是否把相关性当因果、有没有幸存者偏差 —— '
   '这些需要语义理解，只能靠 LLM。\n'
   '顺序不能颠倒：能由代码判定的绝不交给模型。')

qa('Q32. 评审不通过怎么办？为什么不做惩罚？',
   '打回重做：把具体问题清单 + 上一轮结论一起回灌，'
   '让模型针对性修正而不是自由重来，最多重做 max_revisions 次。\n'
   '不做惩罚的原因是：惩罚不改变输出质量，回环会。'
   '实测缺货场景第 1 次被打回、第 2 次通过。\n'
   '另外评审不能过严 —— 踩过坑：把 warning 级问题也纳入强制提及，'
   '导致合格报告被判不通过，'
   '最后模型为了讨好评审而堆砌无关内容，反而降低质量。')

qa('Q33. 降级应该怎么设计？',
   '三条原则：\n'
   '① 不要报错，要给可用输出 —— '
   '一个带免责声明的答案比 500 错误有用得多；\n'
   '② 必须明确声明未经数据验证，并禁止编造任何具体数字 —— '
   '这是降级和编造的分界线；\n'
   '③ 要告诉用户缺什么：哪些信息缺失、'
   '需要补充什么数据才能给出确定结论。\n'
   '另外还有最底层兜底：连 LLM 都调不通时，'
   '输出纯确定性内容（已算好的指标）。')

qa('Q34. 工具调用的可观测性你会记录什么？',
   '每个工具记录：调用次数、失败次数、重试次数、平均耗时。\n'
   '每次 Agent 运行记录：意图与判定方式、装配的能力、路由目标、'
   '完整工具调用序列、轮次、工具错误详情（含错误类型）、是否降级、'
   '总耗时、上下文 token 数、评审结果、重做次数、警告列表、'
   '是否时间错位。\n'
   '价值：上线后排查为什么这次报告慢或差，全靠这些字段。')

h2('6.3 数据层与迁移类（10 题）')

qa('Q35. 切换数据库时，你会用 ORM 还是写适配层？',
   '看操作子集的大小。本项目只用到 SELECT / 建表 / 批量插入这一小撮操作，'
   '引入 SQLAlchemy 等于为很小的子集付出重量级依赖，'
   '而且多一层查询语义抽象会让排查变难。\n'
   '所以选择薄适配层，只封装真正有差异的地方。\n'
   '判断标准：当抽象能带来的收益小于它引入的复杂度时，不要抽象。'
   '但方言差异必须被封装 —— 否则它会长进业务代码里，'
   '那才是真正的技术债。')

qa('Q36. 双数据库需要封装哪些方言差异？',
   '四处，其中最后一处最危险：\n'
   '① 占位符：SQLite 是 ?，PostgreSQL 是 %s；\n'
   '② 只读强制：SQLite 是文件 mode=ro，'
   'PostgreSQL 是会话变量 default_transaction_read_only；\n'
   '③ 元数据查询：PRAGMA table_info vs information_schema.columns；\n'
   '④ 数值类型：SQLite 的 REAL 是 8 字节，'
   '而 PostgreSQL 的 REAL 是 4 字节，必须转 DOUBLE PRECISION。')

qa('Q37. 为什么同名不同类型比名字不同更危险？',
   '因为名字不同会立刻报错，你能马上发现；'
   '而同名不同类型不报错、不崩，只让数据悄悄变形。\n'
   '以 REAL 为例：PostgreSQL 的 4 字节浮点约 7 位有效数字，'
   '123012.88 可能变成 123012.875。'
   '单条数据上只差几分钱，毫不可疑 —— 只有汇总求和时才暴露。\n'
   '所以迁移校验必须比对 SUM(gmv) 这类金额汇总，'
   '只比对行数是发现不了的。')

qa('Q38. 占位符从 ? 换成 %s 有什么坑？',
   '不能无脑 replace。SQL 里的字符串字面量和注释也可能含问号，'
   '例如 LIKE 模式里带问号，或注释里写了个问号 —— '
   '无脑替换会产生静默错误的查询（不报错，只是结果不对）。\n'
   '正确做法是先屏蔽字符串字面量与注释，只替换代码区的问号。'
   '这与 validate_sql 里先去注释再检测关键字是同一类问题：'
   '对 SQL 做文本处理时，必须先区分代码与数据。')

qa('Q39. 迁移脚本的设计要点有哪些？',
   '四个：\n'
   '① 幂等：先 TRUNCATE 再写入。'
   '不幂等的脚本第二次运行会让数据翻倍，这是数据迁移最常见的翻车方式；\n'
   '② 分批提交：500 行一批。一次性插百万行会撑爆内存与事务日志；\n'
   '③ 列结构校验：迁移前比对源/目标的列名，'
   '不一致时宁可直接失败，也不要写入错位的数据；\n'
   '④ 金额汇总校验：比对 SUM(gmv) 而非只比行数 —— 抓浮点精度问题。')

qa('Q40. 没有 PostgreSQL 环境，你怎么验证适配层是对的？',
   '把可离线验证的部分单独覆盖：方言转换、DDL 转换、配置解析与脱敏。'
   '这些不依赖真实数据库，但覆盖了适配层最容易出错的地方。\n'
   '至于真的连上能不能跑，用一个 pg 标记的测试覆盖 —— '
   '有 DATABASE_URL 时才跑，否则 skip。\n'
   '然后 CI 里起一个真实 PostgreSQL 服务，跑迁移 + 一致性校验 + '
   '在 PG 后端上重跑全部验收。\n'
   '这样 CI 全绿有意义，真实 PG 验证也不会被漏掉。')

qa('Q41. 只读是怎么在两种数据库上强制实现的？',
   '都是服务端或驱动层强制，不靠应用层校验：\n'
   '· SQLite：用文件 URI 的 mode=ro 打开，写操作会被驱动直接拒绝；\n'
   '· PostgreSQL：设置会话变量 default_transaction_read_only = on，'
   '之后当前会话内的任何写操作都会被服务端拒绝。\n'
   '注意 PostgreSQL 要用 autocommit=True，否则这个设置落在事务里，'
   '事务结束就失效（连接池复用时会只读失效）。\n'
   '为什么不用应用层校验：应用层校验可能被绕过，服务端限制不会。')

qa('Q42. 连接串怎么处理才安全？',
   '三条：\n'
   '① 任何对外输出的配置描述都必须脱敏 —— '
   '日志、健康检查、审计都可能打印配置，'
   '一旦把带密码的连接串写进去就是凭据泄露。'
   '所以有一个 _mask_url() 专门做这件事；\n'
   '② 密钥只放环境变量，不放代码和模板文件。'
   '我们踩过 .env.example 里写着真实 Key 的坑 —— '
   '模板文件通常会被提交到版本库，等于公开泄露；\n'
   '③ 启动时做安全自检，把生产环境不该出现的配置显式警告出来'
   '（如未配 JWT 密钥、在使用默认账号）。'
   '很多事故的根因不是没做安全，而是以为做了。')

qa('Q43. 换库之后怎么确认业务逻辑没受影响？',
   '三个层次的验证：\n'
   '① 迁移一致性：行数、列结构、关键金额汇总三项比对；\n'
   '② 指标层一致性：在 PG 后端上跑指标层，与 SQLite 结果逐项比对'
   '（GMV、订单数、转化率、客单价），相对误差要求小于 1e-9；\n'
   '③ 全量验收：在 PG 后端上重跑全部验收脚本与 pytest。\n'
   '第 ② 层是关键 —— 它是换库不改业务代码的最终验证。'
   '如果这条通过，说明适配层成功吸收了所有方言差异。')

qa('Q44. 为什么内存会话存储会有多副本问题？',
   '原来会话存在进程内的模块级字典里，带来两个问题：\n'
   '① 重启即丢失 —— 部署、扩容、崩溃重启后对话上下文断裂；\n'
   '② 多副本不共享 —— 负载均衡把同一用户打到不同副本时，'
   '会话历史不一致，表现为 Agent 时好时坏地记不住之前聊过什么。\n'
   '修法是抽象为 SessionStore 接口，内存实现用于单实例，'
   'Redis 实现用于多副本。\n'
   '注意：Redis 不可用时必须明确报错，不能静默降级成内存存储 —— '
   '静默降级会让多副本部署出现难以排查的会话丢失。')

h2('6.4 性能与并行类（6 题）')

qa('Q45. 并行一定比串行快吗？',
   '不一定 —— 这是本项目一个反直觉的实测结论。\n'
   '实测：6 个本地 SQLite 工具，串行 20.3ms，并行 27.4ms（更慢），'
   '因为单个工具只要 12~20ms，而线程池创建和调度开销约 20~30ms。\n'
   '收益模型：并行墙钟 ≈ max(单个耗时) + 线程池开销，'
   '串行墙钟 ≈ sum(单个耗时)。\n'
   '所以只有单次工具耗时足够大（一般是几十到几百毫秒以上，'
   '例如走网络的外部 API、跨库大查询），并行才真正划算。\n'
   '如果照着并行一定更快的直觉直接上线，得到的是一个负优化。')

qa('Q46. 那你的并行是怎么做的？',
   '做成自适应的，不是无脑并行：\n'
   '· 用工具的真实历史耗时预估收益（不靠猜）；\n'
   '· 预估加速比 > 1.15 才启用并行，否则走串行（零额外开销）；\n'
   '· 冷启动（无历史统计）时保守走串行，避免首次运行反而变慢。\n'
   '实测：快工具（总 19ms）判定串行，慢工具（4×150ms）判定并行，'
   '实际 606ms → 154ms，加速 3.93x。\n'
   '这条策略的真正价值在未来：接上真实数仓后查询会升到百毫秒级，'
   '同一个策略会自动切换到并行，不需要改代码。')

qa('Q47. 并行化时怎么保证不改变原有语义？',
   '三件事必须守住：\n'
   '① 顺序：返回顺序与请求顺序严格一致（按原始下标回填，'
   '不依赖完成顺序），否则预处理与日志会错位；\n'
   '② 容错：超时、重试、错误分类、指数退避完全保留 —— '
   '走同一份 registry._run 实现，只有并发方式不同。'
   '并行化不允许改变任何容错行为；\n'
   '③ 不嵌套线程：registry.execute 内部本来就会开线程做超时控制，'
   'worker 里再开一层会让线程数翻倍，'
   '所以并行路径用 execute_inline'
   '（同一份容错逻辑，只是不做线程级超时）。')

qa('Q48. 并行路径的超时保护有什么妥协？',
   '要诚实说明：Python 无法强杀正在运行的线程，'
   '所以并行路径下单工具的 timeout_s 无法被硬性中断，'
   '超时统一由整批的 overall_timeout 和线程池调度兜底。\n'
   '对只读查询这是可接受的（不会产生副作用），'
   '但这一点必须写清楚，不能假装并行后超时保护和原来一模一样。\n'
   '串行路径（execute）仍然保留完整的硬超时保护。')

qa('Q49. 成本控制你做了哪些优化？',
   '① 规则优先：意图识别规则命中率 100%，省掉每次请求的意图判定调用；\n'
   '② 工具按需装配：给 3~5 个工具而非 13 个，减少无效调用轮次；\n'
   '③ 上下文压缩：压缩率 69%，降低输入 token；\n'
   '④ 早停：同类永久错误 2 次即停，不空跑到上限；\n'
   '⑤ 重复调用防护：元信息工具第二次起返回提示而非完整 schema；\n'
   '⑥ 交叉维度工具：一个工具替代多轮单维度工具拼凑'
   '（实测 5 轮降到 2 轮）；\n'
   '⑦ 分层限流：按角色配日配额，只对消耗 LLM 的路径计费；\n'
   '⑧ 评测分层：offline 层免费且进 CI，live 层只在需要时跑。')

qa('Q50. 减少 LLM 轮次和并行工具，哪个收益更大？',
   '减少 LLM 轮次大得多。\n'
   '一次归因 5~6 次工具调用约 20 秒，其中大部分是 LLM 往返；'
   '工具并行能省的只是工具那段（毫秒到百毫秒级）。\n'
   '真正的大收益来自减少轮次 —— 例如交叉维度工具把 5 轮压到 2 轮，'
   '直接省掉三轮 LLM 往返。\n'
   '所以优化顺序应该是：先减少轮次（改进工具设计），'
   '再优化单轮内耗时（并行）。')

h2('6.5 工程实践类（12 题）')

qa('Q51. 你怎么测试一个 LLM 应用？',
   '分五层，从确定到不确定：\n'
   '① 地基验收（43 项）：数据、指标、质控 —— 完全不依赖 LLM；\n'
   '② 编排骨架验收（34 项）：用假 LLM 验证回环、降级、受限路径；\n'
   '③ 架构验收（184 项）：Tool Registry、容错、意图、上下文、'
   '硬约束、时间、SQL、并行；\n'
   '④ 工作流验收（55 项）：离线假 LLM 模式 + --live 真实模式；\n'
   '⑤ 企业级验收（95 项）：认证、授权、数据权限、限流、会话、审计。\n'
   '另加 pytest 评测（44 项断言，产出准确率）。\n'
   '核心思路：用假 LLM 把流程 bug 和模型质量问题分开 —— '
   '混在一起测，你永远分不清是哪个。')

qa('Q52. 为什么离线测试全绿还是会出问题？',
   '因为实际踩到了 —— 222 项离线验收全绿，'
   '却漏掉了三个必然崩的 bug：\n'
   '① 结构化输出被 DeepSeek 拒绝（只有真发请求才知道服务端不支持）；\n'
   '② 回调签名冲突（没有任何测试传过 on_progress）；\n'
   '③ 工具参数名不匹配（只有真模型才会传 start_date）。\n'
   '共同点：都是集成层问题，只有真实调用才暴露。\n'
   '教训：离线全绿不等于能跑通。'
   '改了流程后必须跑一次 --live 或 CLI。')

qa('Q53. 测试里怎么处理 LLM 的不确定性？',
   '用假 LLM 替换模型调用，让编排行为完全可控可复现：\n'
   '· 假规划器：预先设定调什么工具、什么时候结束的序列；\n'
   '· 假子结论生成：返回固定文本；\n'
   '· 假 LLM 对象：捕获 prompt 用于断言，'
   '并支持 with_structured_output。\n'
   '这样轮次控制、降级触发、回环次数、上下文滚动全都能精确断言，'
   '而且秒级完成、零成本。真实模型只用于最后的 --live 端到端验收。')

qa('Q54. 项目里最难的 bug 是哪个？怎么定位的？',
   '推荐讲结构化输出全部失效，因为它最有迷惑性：\n'
   '· 现象：基础对话正常，但规划器、需求解析全部失败；\n'
   '· 迷惑点：基础调用正常，所以第一反应不会怀疑模型配置，'
   '而且失败被 except 兜底吞掉，'
   '表现为 Agent 什么都不做就说结束了；\n'
   '· 定位方法：逐步分层排查 —— 先测基础 invoke（成功）→ '
   '再测 with_structured_output（失败）→ '
   '拿到完整报错 response_format type is unavailable → '
   '确认 DeepSeek 不支持 json_schema 类型；\n'
   '· 修法：封装 structured_llm() 显式用 function_calling + 三级回退。\n'
   '这个 bug 的价值在于：它让整个项目一度只有确定性部分能用，'
   '而表面上一切正常。')

qa('Q55. 你怎么决定一个功能要不要交给 LLM？',
   '用一条判据：能不能用代码 100% 判定？能就不交给模型。\n'
   '具体分类：\n'
   '· 数值计算、日期换算、聚合统计 → 代码（模型会算错且看不出）；\n'
   '· 规则匹配（意图、禁用词、字段校验）→ 代码'
   '（稳定、零成本、零漏判）；\n'
   '· 语义理解（归因、生成、挑刺）→ 模型（代码做不到）。\n'
   '这条原则在项目里的体现：计算归代码、规则优先、双层评审、'
   '趋势检测用统计而非模型直觉。')

qa('Q56. 评测体系为什么分两层？',
   '离线层（不需要 LLM，秒级、免费、100% 可复现）：'
   '路由准确率 / 工具准确率 / 数值准确率 / 质控准确率 / '
   '并行策略 / 认证权限 —— 能进 CI 每次提交都跑。\n'
   '在线层（需要 LLM，分钟级、花钱、有随机性）：'
   '端到端行为 / 陷阱抵御 / 数字保真度 —— '
   '用 pytest marker 隔离，只在有 API Key 时跑。\n'
   '关键设计：离线层的准确率本身就是有价值的指标 —— '
   '它衡量的是确定性骨架的健康度。'
   '改 prompt、换模型、重构指标层时，'
   '离线准确率下降就说明出了问题，'
   '而且能立刻定位到是哪一类能力退化。')

qa('Q57. 什么是加权准确率？为什么要加权？',
   '给不同题目不同权重，本项目陷阱题的权重是普通题的 1.5~3 倍。\n'
   '原因：会不会被归因陷阱带偏比能不能取对数更能区分系统能力。'
   '只看未加权准确率，会让取数全对但归因全错的系统看起来还不错。\n'
   '实测：未加权 97.7% 时加权是 98.6% —— '
   '说明加权把陷阱题的表现更充分地反映出来了。')

qa('Q58. CI 里怎么防止 prompt 改动导致能力退化？',
   '把评测接进 CI，并设准确率门槛：\n'
   '· pytest 每次提交跑 offline 层，--min-accuracy 0.90；\n'
   '· 不达标 CI 失败，无法合并；\n'
   '· 准确率写入 GitHub Step Summary，PR 页面直接能看到表格；\n'
   '· 报告 JSON 作为 artifact 上传，保留 30 天，可以看趋势。\n'
   '这样改了 prompt 之后能力退化会在合并前被拦住，'
   '而不是上线后靠用户反馈发现。')

qa('Q59. 报告落盘会有什么问题？',
   '会累积。踩过的坑：报告是落盘的，如果不清空，'
   '第二次运行时汇总会把上一次的 layer 文件也读进来 —— '
   '表现为报告里出现重复的能力层，准确率被重复计算。\n'
   '这类陈旧状态污染的问题在 CI 里尤其隐蔽：'
   '因为 CI 每次都是干净环境，本地跑两次才会看到。\n'
   '修法是在 pytest_sessionstart 里显式清理，'
   '不能依赖环境是干净的这个假设。')

qa('Q60. 什么是静默失败？项目里遇到过吗？',
   '静默失败指代码没报错、测试全绿，但实际功能没生效。\n'
   '项目里遇到过一次：准确率报告始终为空，但没有任何报错。\n'
   '根因是 pytest 把 conftest.py 作为顶层模块 conftest 导入，'
   '而测试模块用 from tests.conftest import 导入 —— '
   '这是两个不同的模块对象，各自持有一份 REPORTS 列表，'
   '测试注册的报告在汇总时完全看不到。\n'
   '修法是不依赖同一个模块对象，改用进程级共享状态（落盘 JSON）。\n'
   '教训：关键产物必须有断言（例如报告非空），'
   '否则没输出会被当成没问题。')

qa('Q61. 权限矩阵最容易踩什么坑？',
   '父子路径漏配。我们配了 /api/tools 却漏了 /api/tools/stats，'
   '导致分析师被误拒 —— FastAPI 的路径匹配是精确的，不是前缀匹配。\n'
   '更根本的修法是设计成默认拒绝：'
   '未在权限表中配置的路径只有管理员能访问。'
   '这样新加接口忘了配权限时，结果是访问不了（可发现、可修复），'
   '而不是谁都能访问（静默漏洞）。\n'
   '这是安全设计的默认姿态问题 —— 白名单比黑名单安全。')

qa('Q62. 从原型到企业级，你的优先级排序是什么？',
   '按不做会怎样排序，不是按难度：\n'
   '① 认证鉴权 —— 不做就等于前面所有护栏失效（最高优先级）；\n'
   '② 限流与配额 —— 不做则成本可能被单个用户打穿；\n'
   '③ 会话外置 —— 不做则无法多副本部署；\n'
   '④ 结构化日志与审计 —— 不做则线上问题无法定位、无法合规；\n'
   '⑤ 容器化与 CI —— 不做则交付与回归都靠人工；\n'
   '⑥ 数据库换生产级 —— 不做则无法真正横向扩容。\n'
   '排序依据是失败代价而不是实现难度。')

h2('6.6 项目经历类（4 题）')

qa('Q63. 介绍一下你这个项目。',
   '结构：背景 → 职责 → 方案 → 结果 → 反思。\n'
   '背景：电商团队取数分析一轮要 1~2 小时，'
   '需求澄清和口径对齐成本高。\n'
   '职责：项目 owner，从 0 到 1 负责产品设计与落地。\n'
   '方案：做了意图识别条件路由、动态 Tool Registry、'
   '工具循环+数据预处理注入、上下文工程、双层评审五块；'
   '后来补齐企业级能力（认证鉴权、限流、会话外置、审计、'
   '双数据库后端、容器化与 CI）与评测体系。\n'
   '结果：单次分析报告从 1~2 小时缩短到 5~15 分钟；'
   '月均调用 1000+ 次；411 项验收 + 44 项评测断言全绿，准确率 100%。\n'
   '反思：最有价值的收获是计算归代码、解读归模型这条原则 —— '
   '它让数字可信度不再依赖模型表现。')

qa('Q64. 这个项目最大的技术难点是什么？',
   '不是写 Agent 循环，而是让结论可信。四个具体难点：\n'
   '① 归因污染：转化率下跌有 4 种病因，'
   '模型天然会选最直觉的那个，所以要建质控层做硬约束，'
   '并把详情页强制排到排查序列最后；\n'
   '② 数字可信度：模型算数会错且看不出来，'
   '所以把计算全部收到代码层；\n'
   '③ 集成层的坑：结构化输出、回调签名、参数名这三个问题'
   '离线测试全绿也发现不了，必须真实调用；\n'
   '④ 并行化的反直觉：并行不一定更快，'
   '必须用收益模型判断，否则做出的是负优化。')

qa('Q65. 这个项目有哪些不足？',
   '诚实回答：\n'
   '① 数据是模拟的，真实数仓的复杂度'
   '（脏数据、口径不一致、多数据源）还没覆盖；\n'
   '② PostgreSQL 后端已实现但本机无 PG 环境，'
   '只验证了适配逻辑，真实集成测试在 CI 里跑；\n'
   '③ 交叉维度受限于数据模型（缺商品×渠道粒度），'
   '目前是结构重构而非精确联表；\n'
   '④ 密钥管理仍是环境变量，生产应接入 KMS / Vault；\n'
   '⑤ 认证用的是自实现 HS256，生产建议用 RS256 + 成熟库；\n'
   '⑥ 缺少真实用户反馈闭环 —— 建议被采纳后的效果没有回流。')

qa('Q66. 用一句话总结你从这个项目学到的最重要的东西。',
   '不要让模型的强项去弥补它的弱项。\n'
   '模型的强项是理解与生成，弱项是精确计算与记忆细节。'
   '好的架构不是想办法让模型算对，而是根本不让它算 —— '
   '把数字、时间、聚合交给代码，把解读交给模型。\n'
   '同样地，也不要指望 prompt 能可靠地约束行为：'
   '能用代码强制的（硬约束、护栏、结构校验、评测门槛），'
   '就不要只写在 prompt 里。\n'
   '还有一条工程上的体会：不要靠事后修复，要靠约定从源头避免。'
   '我试图用正则修复引号嵌套，结果一次误改 526 处、文件彻底损坏 —— '
   '而正确的做法只是定一条编码约定。')

# ===========================================================================
# 附录
# ===========================================================================
doc.add_page_break()
h1('附录  常用命令速查')

h2('A.1 数据与验收')
code('''
python scripts/seed_data.py             # 生成模拟数据（含 4 类归因陷阱）
python scripts/verify_foundation.py     # 43 项：数据层 + 指标层 + 质控层
python scripts/verify_pipeline.py       # 34 项：编排骨架（假 LLM）
python scripts/verify_architecture.py   # 184 项：Registry/容错/意图/上下文/硬约束/时间/SQL/并行
python scripts/verify_workflow.py       # 55 项：工作流端到端（离线）
python scripts/verify_enterprise.py     # 95 项：认证/授权/数据权限/限流/会话/审计
python scripts/verify_workflow.py --live # 真实 LLM 端到端（需 API Key）
''')

h2('A.2 pytest 评测')
code('''
pytest tests/ -v                                  # 默认只跑 offline + pg
pytest tests/ -m offline                          # 只跑离线层（不花钱）
pytest tests/ -m live                             # 在线层（真实 LLM）
pytest tests/ --accuracy-report                   # 输出完整准确率报告
pytest tests/ --min-accuracy 0.90                 # 准确率门槛（不达标失败）
pytest tests/ --junitxml=reports/junit.xml        # 供 CI 解析
pytest tests/test_eval.py::test_routing_accuracy  # 单跑某一层
pytest tests/ -k auth                             # 按关键词筛选
''')

h2('A.3 数据库切换与迁移')
code('''
# --- 默认 SQLite ---
set EC_DB_BACKEND=sqlite
set EC_DB_PATH=data/ec.db

# --- 切换到 PostgreSQL ---
set EC_DB_BACKEND=postgres
set DATABASE_URL=postgresql://user:pwd@localhost:5432/ecmonica

# 迁移（幂等，可重复运行）
python scripts/migrate_to_postgres.py --dry-run    # 干跑：只检查
python scripts/migrate_to_postgres.py              # 正式迁移
python scripts/migrate_to_postgres.py --verify-only # 只做一致性校验

# 注意：迁移会先 TRUNCATE 目标表（保证幂等）
#       跳过清空用 --no-truncate（谨慎：可能主键冲突或重复数据）
''')

h2('A.4 命令行使用')
code('''
# 自动意图路由
python scripts/run_agent.py "上周的 GMV 是多少"
python scripts/run_agent.py "转化率为什么下滑了"
python scripts/run_agent.py "拆解这个需求：加一个加购转化率指标"

# 指定分析区间 / 需求解析
python scripts/run_agent.py "分析这段时间" --start 2025-07-01 --end 2025-07-07
python scripts/run_agent.py --requirement "订单状态要加一个部分退款状态"

# 查看注册工具与路由表
python scripts/run_agent.py --tools
python scripts/run_agent.py --routes

# 只跑确定性部分（不需要 API Key）
python scripts/run_agent.py --dry-run "上周GMV多少"
''')

h2('A.5 服务与容器')
code('''
# 本地运行
uvicorn app.main:app --reload

# 容器（含 Redis，多副本就绪）
docker compose up -d --build
docker compose logs -f api
docker compose down -v

# 登录并调用（所有业务接口都需要令牌）
curl -X POST http://127.0.0.1:8000/api/auth/login ^
  -H "Content-Type: application/json" ^
  -d "{\\"username\\":\\"admin\\",\\"password\\":\\"admin123\\"}"

curl http://127.0.0.1:8000/api/metrics ^
  -H "Authorization: Bearer <access_token>"

# 关键地址
#   交互文档：http://127.0.0.1:8000/docs
#   健康检查：http://127.0.0.1:8000/api/health   （免认证）
#   数据质控：http://127.0.0.1:8000/api/quality
#   工具清单：http://127.0.0.1:8000/api/tools
''')

h2('A.6 验收标准')
table(
    ['类别', '脚本 / 命令', '检查项', '当前状态'],
    [
        ['验收', 'verify_foundation.py', '43', '0 失败'],
        ['验收', 'verify_pipeline.py', '34', '0 失败'],
        ['验收', 'verify_architecture.py', '184', '0 失败'],
        ['验收', 'verify_workflow.py', '55', '0 失败'],
        ['验收', 'verify_enterprise.py', '95', '0 失败'],
        ['验收小计', '—', '411', '全绿'],
        ['评测', 'pytest tests/ -m offline', '44 项断言', '准确率 100%'],
        ['评测', 'pytest tests/ -m pg', '3（无 PG 时 skip）', 'CI 中真跑'],
    ],
)

ok_box('最后一句提醒',
       '改了流程后请务必跑一次真实端到端（--live 或 CLI）。'
       '本项目的三个最严重 bug 都是离线全绿、真实调用才暴露的 —— '
       '离线测试证明逻辑对，真实调用才能证明跑得通。')

# ===========================================================================
# 保存
# ===========================================================================
OUT_DIR.mkdir(parents=True, exist_ok=True)
doc.save(OUT_FILE)

_size_kb = OUT_FILE.stat().st_size / 1024
print(f'[完成] 文档已生成：{OUT_FILE}')
print(f'       大小：{_size_kb:.1f} KB')
print(f'       段落数：{len(doc.paragraphs)}')
print(f'       表格数：{len(doc.tables)}')
