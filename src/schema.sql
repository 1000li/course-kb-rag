-- course-kb-rag 数据库 schema（架构文档 docs/architecture.md §6，v0.2）
-- 单一事实库：db/course_kb.db；由 src/db.py 的 init_db() 执行本文件建表。

-- 归集台账
CREATE TABLE IF NOT EXISTS documents (
  doc_id        TEXT PRIMARY KEY,      -- doc_001
  title         TEXT NOT NULL,
  version       TEXT NOT NULL,         -- v1.0 / v1.1
  format        TEXT NOT NULL,         -- pdf / docx
  path          TEXT NOT NULL,
  sha256        TEXT NOT NULL UNIQUE,  -- 幂等键
  pages         INTEGER,
  lessons_count INTEGER,               -- 派生字段：解析后回填，展示用，非权威（真源 = lessons 表）
  status        TEXT NOT NULL,         -- active / superseded
  ingested_at   TEXT NOT NULL,
  notes         TEXT
);

-- 课级结构（解析层产物）
CREATE TABLE IF NOT EXISTS lessons (
  lesson_id     TEXT PRIMARY KEY,      -- doc_001_L05
  doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
  lesson_no     INTEGER NOT NULL,
  title         TEXT NOT NULL,
  page_start    INTEGER,
  page_end      INTEGER,
  kp_declared   TEXT                   -- 讲义自带"本课知识点一览"，JSON 数组
);

-- 块（数据集的一行）
CREATE TABLE IF NOT EXISTS chunks (
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
CREATE TABLE IF NOT EXISTS cleaning_log (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id        TEXT NOT NULL,
  lesson_no     INTEGER NOT NULL,
  rule_id       TEXT NOT NULL,         -- page_number_line / header / image_placeholder / ...
  line_content  TEXT,                  -- 被处理行的原文（截断）
  action        TEXT NOT NULL,         -- removed / replaced
  created_at    TEXT NOT NULL
);

-- 质检报告（核心指标拆为顶层列，便于 Datasette 排序筛选）
CREATE TABLE IF NOT EXISTS qc_reports (
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
CREATE TABLE IF NOT EXISTS pipeline_runs (
  run_id        TEXT PRIMARY KEY,
  stage         TEXT NOT NULL,         -- ingest/parse/clean/chunk/annotate/qc/index
  doc_version   TEXT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  stats         TEXT,                  -- JSON
  status        TEXT NOT NULL          -- success / failed
);

-- 索引元数据（embedding 版本化：换模型可检测、强制重建）
CREATE TABLE IF NOT EXISTS index_runs (
  run_id        TEXT PRIMARY KEY,
  model_name    TEXT NOT NULL,         -- 如 bge-small-zh-v1.5
  dim           INTEGER NOT NULL,
  normalize     INTEGER NOT NULL,      -- 0/1
  chunk_count   INTEGER NOT NULL,
  built_at      TEXT NOT NULL
);
-- qa.py 启动时校验：当前配置的 model_name/dim 必须与最新 index_runs 一致，否则报错要求重建索引

-- 检索评估运行（M5；架构文档 §6 未列此二表，按 §5 评估口径补，风格对齐既有表）
CREATE TABLE IF NOT EXISTS eval_runs (
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
                                     -- 拒答 = top-5 内最大向量相似度 < tv 且最大 BM25 < tb
                                     -- （RRF 单阈值经实测不可分，见 docs/eval-report-m5.md 标定节）
  notes         TEXT
);

-- 检索评估逐题明细
CREATE TABLE IF NOT EXISTS eval_results (
  run_id        TEXT NOT NULL REFERENCES eval_runs(run_id),
  qid           TEXT NOT NULL,
  scope_doc_id  TEXT NOT NULL,
  should_abstain INTEGER NOT NULL,   -- 0/1
  hit_at_5      INTEGER,             -- 可答题：top-5 是否命中期望课；应拒答题 NULL
  first_hit_rank INTEGER,            -- 首个命中名次（MRR 用），未命中 NULL
  top1_score    REAL,                -- 融合 top-1 的 RRF 分数
  abstained     INTEGER NOT NULL,    -- 标定阈值下是否拒答
  top_chunks    TEXT,                -- JSON：top-5 [{chunk_id, lesson_no, rrf, vec, bm25}]
  PRIMARY KEY (run_id, qid)
);

-- 向量索引（sqlite-vec 虚拟表，每次全量重建，可删可重建）
-- 由 index 阶段（M5，src/index.py）创建，M1 不建：
-- CREATE VIRTUAL TABLE chunk_vectors USING vec0(chunk_id TEXT PRIMARY KEY, embedding FLOAT[512])
