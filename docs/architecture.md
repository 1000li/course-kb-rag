# course-kb-rag 架构文档

版本：v0.3（M5 实测修订版） · 2026-10-07
v0.2 变更：落实外审 4 条 blocking（检索口径、embedding 版本化、中间产物落点、检索质量评估）+ 全部建议修改；新增"检索与回答口径""检索质量评估""可复现性"三节。
v0.3 变更：落实 M5 实测两处规格修订（owner 已裁定）：① 拒答阈值从"RRF top-1 单阈值"改为"双信号与门"（RRF 只含名次不含绝对相关性，两类题 RRF 区间完全重叠，实测不可行，标定过程见 docs/eval-report-m5.md）；② eval 契约从"只读"改为"只读源数据，结果追加落库"。

## 1. 项目定位

把一份教材体例的课程讲义（《人工智能主题课程讲义》，15 课）加工成一个**可溯源问答的知识库**，端到端演示"内容 → 高质量数据集 → RAG 检索问答"的完整管线。

项目的展示对象是出版/教育行业的"AI 产品 / 高质量数据集"岗位。每个阶段对应 JD 关键词：

| 阶段 | JD 关键词 |
|---|---|
| 归集台账 | 资源归集、可追溯性 |
| 解析 / 清洗 | 数据清洗与预处理 |
| 切块 + 元数据 | 数据集格式规范 |
| 标注 | 数据标注、标注规范 |
| 质检 | 自动化检测 + 人工抽检 |
| 检索评估 | 检索准确率、效果验证 |
| 检索 + 问答 | RAG、向量库、场景验证 |
| 版本演进 | 时效性、版本管理 |

**非目标**（明确不做）：OCR / 公式识别、图片内容理解、多人协同标注平台、Web 前端美化、部署上线、多用户与权限。

## 2. 语料与版本线

| 文件 | 版本 | 说明 |
|---|---|---|
| `data/raw/人工智能主题课程讲义v1.0.pdf` | v1.0 | 原始归档，71 页 14 课，第 5 课乱序。**只读，不可变** |
| `data/raw/第15课：AI与未来.docx` | — | 第 15 课原始 docx |
| `data/raw/人工智能主题课程讲义v1.1.pdf` | v1.1 | 修订版，73 页 15 课，乱序已修正，第 15 课并入 |
| `data/raw/人工智能主题课程讲义v1.2.pdf` | v1.2 | 事实修订版（2026-10-07）：外审发现的 28 项问题中 34 处手术修复，依据 `docs/讲义审读报告.md`，手术脚本 `src/fix_v12.py` 可重跑 |

**v1.2 说明**：在 v1.1 上做 PDF 级精准替换（PyMuPDF redact + 字体匹配回写），保留原版式。两处注意：① 新插入文本在内容流末尾，**文本抽取必须用 `sort=True`**（管线解析层遵守）；② 引号系统性问题与第 3 课低龄化段落属结构性问题，留待将来重排（v1.3）。

**"乱序"的精确定义**：v1.0 中第 5 课的**物理页序**错置——其页面（PDF 页 27–31）排在第 6 课（PDF 页 22–26）之后。课内内容与节顺序无问题。因此解析层以**课标题（"第N课/第五课"）为课号真源**，不依赖物理顺序；质检层的课序检查规则 = lessons 按 page_start 排序时 lesson_no 应单调递增。

**v1.1 的生成过程（多格式归集的实锤素材）**：pypdf 重排 v1.0 页序 + 第 15 课 docx 经 `tmp/lesson15.typ`（typst 排版）渲染为 PDF 后并入。该过程本身即"多源异构内容归一化为统一格式"的演示。

设计决策：**raw 层不可变**。v1.0 的乱序与缺课不修源文件，由管线发现、质检报告记录；修订版作为新版本归集。版本演进演示：

- v1.0 进管线 → 数据集 v0.1（质检抓出"第 5 课乱序、缺第 15 课"）
- v1.1 进管线 → 数据集 v0.2（结构问题消解，15 课完整）
- v1.2 进管线 → 数据集 v0.3（内容事实修正；与前版的差异即"内容修订如何传导到数据集"的演示）

默认 active 版本为 **v1.2**（最新修订版）。

## 3. 分层架构

离线管线（ingest → index）与运行时（qa）分离：

```
离线管线：
data/raw/                    归集层：源文件只读归档
    │
    ▼  ingest.py             台账：sha256 登记，幂等（同哈希不重复入库）
db  ── documents 表
    │
    ▼  parse.py              解析层：PDF 文字层 / docx XML → 课边界识别 → 课内原始文本
db  ── lessons 表            中间文本落 data/interim/parsed/{doc_id}/L{nn}.txt
    │
    ▼  clean.py              清洗层：规则化去噪，命中逐条留痕
data/interim/cleaned/…       干净文本落文件；清洗日志落库（cleaning_log 表）
docs/cleaning-rules.md       规则清单自动生成
    │
    ▼  chunk.py              切块层：按节/栏目切块，超限按段递归切分
db  ── chunks 表             pipeline_version 标记；重跑 supersede 旧块，不删除
    │
    ▼  annotate.py           标注层：课级知识点自动下发 + 栏目→content_type 映射
docs/annotation-spec.md      标注规范文档
    │
    ▼  qc.py                 质检层：自动指标 + 人工抽检
db  ── qc_reports 表         空块率 / 重复率(simhash) / 长度分布 / 课序完整性
    │
    ▼  index.py              索引层：向量 + BM25，每次全量重建（派生物，删了可重建）
db  ── chunk_vectors + index_runs

运行时：
    qa.py                    混合检索 → LLM 组织回答 → 强制出处 / 低置信拒答
                             检索范围与口径见第 4 节

呈现：
datasette db/course_kb.db    数据浏览（documents / lessons / chunks / qc_reports）
data/export/dataset.jsonl    数据集交付格式导出
```

关键设计：

- **SQLite 单文件为事实库**。所有结构化数据落一个 `course_kb.db`；向量索引用 sqlite-vec 同库存储。演示叙事："整个数据集就是一个文件，索引是可重建的派生物。"（已验证：Python 3.13 + sqlite-vec 0.1.x 冒烟测试通过，内置 SQLite 3.45.3 ≥ 3.41）
- **块的元数据即出处**：每个 chunk 携带 课号 / 节标题 / 栏目类型 / 页码区间 / 文档版本，问答的"出处"直接从这里取。
- **幂等与版本**：所有脚本可重复运行；`pipeline_version` 变化只 supersede 受影响的块，历史块保留可查。
- **中间产物落点**：解析/清洗的课内文本落 `data/interim/`（按 doc_id/课号文件组织，可删可重建）；**清洗日志落库**（`cleaning_log` 表，按规则聚合查询"每条规则命中多少处、改了哪里"）。
- **拒绝回答是一等功能**：检索置信度低于阈值时明确回答"未在讲义中找到依据"，不调用 LLM。阈值标定依据见第 5 节。

## 4. 检索与回答口径

本节是运行时的完整口径，M5 实现前不得偏离：

1. **默认检索范围**：`superseded_by IS NULL` 且 `doc_id` 属于当前 active 文档版本（最新修订版，当前为 v1.2）的块。旧版本块虽在库中，**默认永不进入检索**。
2. **版本对比场景**：显式指定 `doc_version` 参数放开过滤（如 `qa.py --doc-version v1.0`），用于演示"同一问题在 v0.1 / v0.2 数据集上的回答差异"。
3. **混合检索融合**：向量召回（k=20）+ BM25 召回（k=20），**RRF（Reciprocal Rank Fusion）** 融合取 top-5 作为上下文。
4. **拒答判定（v0.3 修订）**：~~融合后 top-1 的 RRF 分数低于阈值 → 拒答~~ 实测不可行：RRF 只含名次不含绝对相关性，应拒答题与可答题的 top-1 RRF 区间完全重叠（详见 docs/eval-report-m5.md 标定节）。改为**双信号与门**：top-5 内最大向量相似度 < 0.1512 **且**最大 BM25 < 11.87 → 拒答（两个信号都弱才拒，任一强则答）。阈值**不拍脑袋**：由第 5 节评测集标定（使"应拒答问题全部拒答且正常问题误拒率 ≤10%"）；**换 embedding 模型或改评测集必须重新标定**。已知风险：最难拒的 q20（YOLO，与 OpenCV 课语义相近）距阈值余量仅 0.002。
5. **出处拼装**：chunk 元数据 → `第5课 · 二、机器学习的类型 · P22-26`（课号 + 节标题 + 页码区间）。
6. **LLM 角色**：仅将 top-5 块组织为通顺回答，系统提示词强制"只能依据给定块，块里没有的信息不得补充"。
7. **页码口径**（M2 实测后补记）：一律用**物理页码**（PDF 阅读器显示的页序）。原因：v1.0 本身印刷页码即乱序，v1.1/v1.2 经 pypdf 重排后页脚印刷页码随页搬走、跨课不单调（课内递增）。物理页码在所有版本中一致可用；印刷页码不作为定位依据。

## 5. 检索质量评估

数据质检（qc.py）之外，独立的**问答质量评估**：

- `eval/questions.jsonl`：15–25 条人工设计的问题，字段：
  ```json
  {"qid": "q01", "question": "机器学习有哪几种类型？", "expected_lesson_no": 5, "expected_chunk_ids": ["doc_002_L05_004"], "should_abstain": false}
  {"qid": "q20", "question": "量子力学的基本原理是什么？", "expected_lesson_no": null, "expected_chunk_ids": [], "should_abstain": true}
  ```
  覆盖三类：常规问题（应命中指定课/块）、跨课综合问题、超纲问题（应拒答，约 1/4）。
- `src/eval_retrieval.py`：对评测集跑检索，计算 **recall@5 / MRR / 拒答准确率**，输出报告并作为第 4 节拒答阈值的标定依据。
- 评测集随数据集版本演进：v0.1 的评测结果本身就是"第 15 课缺失"的证据（相关问题全部召回失败）。

## 6. 数据库 Schema

```sql
-- 归集台账
CREATE TABLE documents (
  doc_id        TEXT PRIMARY KEY,      -- doc_001
  title         TEXT NOT NULL,
  version       TEXT NOT NULL,         -- v1.0 / v1.1
  format        TEXT NOT NULL,         -- pdf / docx
  path          TEXT NOT NULL,
  sha256        TEXT NOT NULL UNIQUE,  -- 幂等键
  pages         INTEGER,
  lessons_count INTEGER,               -- 派生字段：解析后回填，展示用，非权威（真源 = lessons 表）
  status        TEXT NOT NULL,         -- active / superseded / component（源组件，如并入某版本的 docx）
  ingested_at   TEXT NOT NULL,
  notes         TEXT
);

-- 课级结构（解析层产物）
CREATE TABLE lessons (
  lesson_id     TEXT PRIMARY KEY,      -- doc_001_L05
  doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
  lesson_no     INTEGER NOT NULL,
  title         TEXT NOT NULL,
  page_start    INTEGER,
  page_end      INTEGER,
  kp_declared   TEXT                   -- 讲义自带"本课知识点一览"，JSON 数组
);

-- 块（数据集的一行）
CREATE TABLE chunks (
  chunk_id      TEXT PRIMARY KEY,      -- doc_002_L05_003
  lesson_id     TEXT NOT NULL REFERENCES lessons(lesson_id),
  doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
  lesson_no     INTEGER NOT NULL,
  section_title TEXT,                  -- 一、二、三…节标题
  content_type  TEXT NOT NULL,         -- 正文/知识点一览/课堂活动/思考题/代码段/课后任务
  chapter_path  TEXT NOT NULL,         -- "第5课 > 二、机器学习的类型"
  text          TEXT NOT NULL,
  char_count    INTEGER NOT NULL,
  page_start    INTEGER,
  page_end      INTEGER,
  knowledge_tags TEXT,                 -- JSON 数组，由 kp_declared 下发
                                       -- 注：标签筛选走全表 LIKE，数据量小可接受，不建关联表
  pipeline_version TEXT NOT NULL,      -- 0.1 / 0.2 …
  superseded_by TEXT,                  -- 被替换时指向新 chunk_id，否则 NULL
  created_at    TEXT NOT NULL
);

-- 清洗日志（逐条留痕，可按规则聚合）
CREATE TABLE cleaning_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id        TEXT NOT NULL,
  lesson_no     INTEGER NOT NULL,
  rule_id       TEXT NOT NULL,         -- page_number_line / header / image_placeholder / ...
  line_content  TEXT,                  -- 被处理行的原文（截断）
  action        TEXT NOT NULL,         -- removed / replaced
  created_at    TEXT NOT NULL
);

-- 质检报告（核心指标拆为顶层列，便于 Datasette 排序筛选）
CREATE TABLE qc_reports (
  run_id        TEXT PRIMARY KEY,
  doc_version   TEXT NOT NULL,
  pipeline_version TEXT NOT NULL,
  ran_at        TEXT NOT NULL,
  chunk_count   INTEGER,
  empty_rate    REAL,
  dup_rate      REAL,
  lesson_order_ok INTEGER,             -- 0/1：课序完整性检查
  metrics       TEXT,                  -- JSON：长度分布等次要指标
  human_sample  TEXT,                  -- 人工抽检记录（20 条：含标注下发准确率验证）
  conclusion    TEXT
);

-- 管线运行日志
CREATE TABLE pipeline_runs (
  run_id        TEXT PRIMARY KEY,
  stage         TEXT NOT NULL,         -- ingest/parse/clean/chunk/annotate/qc/index/export/eval
  doc_version   TEXT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  stats         TEXT,                  -- JSON
  status        TEXT NOT NULL          -- success / failed
);

-- 索引元数据（embedding 版本化：换模型可检测、强制重建）
CREATE TABLE index_runs (
  run_id        TEXT PRIMARY KEY,
  model_name    TEXT NOT NULL,         -- 如 bge-small-zh-v1.5
  dim           INTEGER NOT NULL,
  normalize     INTEGER NOT NULL,      -- 0/1
  chunk_count   INTEGER NOT NULL,
  built_at      TEXT NOT NULL
);
-- qa.py 启动时校验：当前配置的 model_name/dim 必须与最新 index_runs 一致，否则报错要求重建索引

-- 检索评估运行（M5）
CREATE TABLE eval_runs (
  run_id        TEXT PRIMARY KEY,
  pipeline_version TEXT NOT NULL,
  model_name    TEXT NOT NULL,       -- 与 index_runs.model_name 对应
  ran_at        TEXT NOT NULL,
  question_count INTEGER NOT NULL,
  recall_at_5   REAL,                -- 可答题课级 recall@5
  mrr           REAL,                -- 可答题 MRR（课级首个命中名次）
  abstain_rate  REAL,                -- 应拒答题中实际拒答比例
  false_abstain_rate REAL,           -- 可答题误拒率
  threshold     TEXT,                -- 标定的拒答规则 JSON：{"vec_max_top5": tv, "bm25_max_top5": tb}
                                     -- 拒答 = top-5 内最大向量相似度 < tv 且最大 BM25 < tb（§4.4）
  notes         TEXT
);

-- 检索评估逐题明细
CREATE TABLE eval_results (
  run_id        TEXT NOT NULL REFERENCES eval_runs(run_id),
  qid           TEXT NOT NULL,
  scope_doc_id  TEXT NOT NULL,
  should_abstain INTEGER NOT NULL,   -- 0/1
  hit_at_5      INTEGER,             -- 可答题：top-5 是否命中期望课；应拒答题 NULL
  first_hit_rank INTEGER,            -- 首个命中名次（MRR 用），未命中 NULL
  top1_score    REAL,                -- 融合 top-1 的 RRF 分数
  abstained     INTEGER NOT NULL,    -- 标定规则下是否拒答
  top_chunks    TEXT,                -- JSON：top-5 [{chunk_id, lesson_no, rrf, vec, bm25}]
  PRIMARY KEY (run_id, qid)
);

-- 向量索引（sqlite-vec 虚拟表，每次全量重建，可删可重建）
-- CREATE VIRTUAL TABLE chunk_vectors USING vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[512])
```

## 7. 各阶段输入 / 输出契约

| 阶段 | 输入 | 输出 | 幂等性 |
|---|---|---|---|
| ingest | `data/raw/*` | documents 行 | sha256 唯一约束 |
| parse | documents(active) | lessons 行 + `data/interim/parsed/` | 同 doc 重跑覆盖 |
| clean | parsed 文本 | `data/interim/cleaned/` + cleaning_log 行 | 规则版本计入 pipeline_version |
| chunk | cleaned 文本 | chunks 行 | 同输入重跑 supersede 旧块 |
| annotate | chunks + kp_declared | knowledge_tags / content_type 更新 | 可重跑 |
| qc | chunks | qc_reports 行 | 每次运行一行 |
| eval | 评测集 + 索引 | 检索质量报告（recall@5/MRR/拒答率） | 只读源数据，结果追加落库（eval_runs/eval_results） |
| index | chunks(active) | chunk_vectors + index_runs | 每次全量重建 |
| qa | 用户问题 | 回答 + 出处 / 拒答 | 只读 |

## 8. 技术选型

- Python 3.13；pypdf（页操作/文本抽取）+ PyMuPDF（渲染验证）；docx 用 zipfile+XML 解析（已验证可行，不引依赖）
- SQLite（内置 3.45.3）+ **sqlite-vec**（向量，已通过 3.13 冒烟测试）；jieba + rank_bm25（BM25；SQLite FTS5 默认分词器不支持中文，排除）
- Embedding：**fastembed + bge-small-zh-v1.5**（约 100MB，CPU 可跑，离线可演示）；效果不足再升 bge-m3（换模型走 index_runs 校验强制重建）
- 问答 LLM：**用候选人手头已有的 API key**（DeepSeek / 智谱 / Moonshot 皆可），仅组织回答，拒答时不调用
  - key 管理：`.env` 存放 + `.gitignore` 排除，绝不入库入仓
  - 语料为本人自有讲义，无第三方版权外传风险；仍记录问答调用日志（`data/logs/qa_calls.jsonl`）便于复盘
- Datasette（数据浏览呈现）；导出 JSONL（数据集交付格式）
- 依赖管理：`pyproject.toml` 固定版本（尤其 sqlite-vec / fastembed）

## 9. 目录结构

```
course-kb-rag/
├── data/raw/              # 源文件（只读）
├── data/interim/          # 解析/清洗中间文本（可删可重建）
├── data/export/           # dataset.jsonl 等导出物
├── data/logs/             # qa 调用日志
├── db/course_kb.db        # 单文件事实库 + 向量索引
├── eval/questions.jsonl   # 检索评测集
├── src/                   # ingest/parse/clean/chunk/annotate/qc/eval_retrieval/index/qa
├── docs/                  # 本文档、cleaning-rules.md、annotation-spec.md、qc 报告
├── tmp/                   # 一次性中间产物（lesson15.typ 等）
├── pyproject.toml
├── .env / .gitignore
└── README.md              # 演示动线 + 复现说明
```

## 10. 里程碑与验收

| 里程碑 | 内容 | 验收 |
|---|---|---|
| M1 归集 | ingest + documents 表 | v1.0/v1.1/docx 三行台账，重跑不重复 |
| M2 解析清洗 | parse + clean | v1.1 解析出 15 课、节结构完整；清洗规则清单 + cleaning_log 可按规则聚合查询 |
| M3 切块标注 | chunk + annotate | chunks 落库、JSONL 导出；块均带课号/栏目/知识点 |
| M4 质检 | qc | v1.0 报告抓出乱序（lesson_order_ok=0）+ 缺课；v1.1 通过；人工抽检 20 条（含标注下发准确率）写入报告 |
| M5 检索评估 | eval 集 + eval_retrieval | recall@5 / MRR / 拒答率报告；拒答阈值完成标定 |
| M6 问答 | qa | 三个场景全通（见下） |
| M7 呈现 | datasette + README | 动线可复现：台账→块表→质检报告→问答 |

M6 三个演示场景（具体 Q&A 对）：

1. **答得出带出处**：问"机器学习有哪几种类型？" → 回答正确组织讲义内容，出处显示"第 5 课 · 二、机器学习的类型 · P26-27"
2. **超纲拒答**：问"量子力学的基本原理是什么？" → "未在讲义中找到依据"，不调用 LLM
3. **版本对比**：问"AI 的未来趋势有哪些？" → v1.0 数据集上拒答（第 15 课缺失），v1.1 数据集上正常作答带出处

## 11. 可复现性

- `pyproject.toml` 固定全部依赖版本；README 写明 Python ≥3.13、内置 SQLite ≥3.41（sqlite-vec 要求）
- 数据资产：raw 文件为本人讲义，随仓库分发；sha256 记录在台账，任何人重跑可校验一致性
- 一键跑通：`python -m src.pipeline all`（按序执行 ingest→parse→clean→chunk→annotate→export→qc→index→eval；eval 只做检索不调 LLM，全程无需 API key），README 附预期输出
- 随机性来源：本管线无随机环节（切块确定性、检索确定性）；若后续引入采样，固定 seed 并写入 pipeline_runs.stats

## 12. 已定决策记录（v0.2 拍板）

1. 向量方案：**sqlite-vec**（单文件叙事，兼容性已冒烟验证）
2. Embedding：**bge-small-zh-v1.5 起步**（fastembed 本地，离线可演示）
3. 问答 LLM：用现有 API key（DeepSeek/智谱/Moonshot 任选），key 走 .env
4. 标注深度：**课级知识点下发 + 栏目映射**，逐块精标不做；M4 人工抽检时验证下发准确率
