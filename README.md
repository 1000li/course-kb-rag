# course-kb-rag

把一份教材体例的课程讲义（《人工智能主题课程讲义》，15 课，三个演进版本）加工成一个**可溯源问答的知识库**，端到端演示「内容 → 高质量数据集 → RAG 检索问答」完整管线。

权威规格：[docs/architecture.md](docs/architecture.md)（v0.3）；项目历史：[CHANGELOG.md](CHANGELOG.md)。

## 环境要求

- Windows + Git Bash（或任何类 Unix shell）
- Python ≥ 3.13（内置 SQLite ≥ 3.41，sqlite-vec 要求）
- 首次建索引需联网下载 embedding 模型（约 100MB，之后离线可用）

```bash
py -3 -m pip install -e .    # 或按 pyproject.toml 逐条安装固定版本
```

## 一键跑通（无需 API key）

```bash
py -3 -m src.pipeline all
```

按序执行 ingest → parse → clean → chunk → annotate → export → qc → index → eval，全链路幂等，预期输出摘要：

```
[入库] doc_001~doc_004（重跑时全部"跳过"）
[解析] v1.0: 14 课（乱序=True）；v1.1/v1.2: 15 课
[清洗] 命中 596/596/591 处（whitespace / page_number_line / pua_bullet）
[切块] 221/230/230 块，均长 ~200 字，超上限 0
[标注] knowledge_tags 课级下发，异常 0
[导出] data/export/dataset_v{1.0,1.1,1.2}.jsonl
[质检] v1.0 不通过（乱序 + 缺第 15 课）；v1.1/v1.2 通过
[索引] 640 块向量化（bge-small-zh-v1.5，dim=512）
[评估] recall@5=100%，MRR=0.917，应拒答 5/5，误拒率 5.6%
```

单阶段可单独跑：`py -3 -m src.pipeline chunk annotate`。删库重建验证：`db/course_kb.db`、`data/interim/`、`data/export/` 均为派生物，删掉重跑 `pipeline all` 即可完全重建。

## .env 配置（仅演示场景 1 需要）

```
DEEPSEEK_API_KEY=sk-...
```

`.env` 已在 `.gitignore` 中，绝不入库入仓。问答调用日志落 `data/logs/qa_calls.jsonl`。

## 演示动线（三场景）

### 场景 1：答得出带出处（需要 API key）

```bash
py -3 -m src.qa "机器学习有哪几种类型？"
```

回答由 DeepSeek 依据 top-5 检索块组织（系统提示词强制只依据块），结尾列出处，格式「第5课 · 一. 什么是机器学习？ · P23-24」（物理页码）。

### 场景 2：超纲拒答（不需要 API key）

```bash
py -3 -m src.qa "量子力学的基本原理是什么？"
```

双信号与门（top-5 内 maxVec < 0.1512 且 maxBM25 < 11.87，阈值由 eval 集标定）判定低置信 → 直接回答"未在讲义中找到依据"，**不调用 LLM**（日志中 model=None 可证）。

### 场景 3：版本对比（v1.0 需 API key 才能看到完整回答；拒答判定本身不需要）

```bash
py -3 -m src.qa "讲义中认为 AI 未来有哪些发展趋势？" --doc-version v1.0
py -3 -m src.qa "讲义中认为 AI 未来有哪些发展趋势？"            # 默认 v1.2
```

叙事要点：**旧版不是答不了，而是只能拼凑**——v1.0 没有第 15 课，只能从第 1/4/7 课的零散未来话题拼出浅层答案；v1.1+ 补入专门篇章后，同一问题精准命中第 15 课，答案有明确出处（「第15课 · 二、AI 的未来趋势与伦理挑战 · P72」）。

## 公网部署（Render 免费档）

仓库自带 `Dockerfile`（python:3.13-slim，构建期预热 embedding 模型避免冷启动下载超时）与 `db/course_kb.db`（事实库随镜像分发）。步骤要点：

1. Render → **New Web Service** → 连接本仓库 → Runtime 选 **Docker**
2. 环境变量：
   - `DEEPSEEK_API_KEY`（必填，场景 1 的 LLM 路径）
   - `DEMO_TOKEN`（可选但建议：设置后全站启用密码闸门）
   - `PYTHONUTF8=1`（保险；镜像内已默认）
3. 健康检查路径填 `/healthz`；Render 自动注入 `$PORT`，serve.py 已适配

安全说明：

- **DEMO_TOKEN 闸门**：GET / 出密码页，校验通过才发 HttpOnly Cookie（值为 token 的 sha256，不落明文）；无有效 Cookie 时 /api/ask 返回 401。不设 DEMO_TOKEN 则完全无闸（本地开发体验不变）
- **速率限制**：/api/ask 按客户端 IP 内存滑动窗口限流，默认 10 次/分钟（可用 `RATE_LIMIT_PER_MIN` 调整），超限返回 429——防止 DeepSeek 调用被打爆产生费用
- **key 绝不入库**：`.env` 在 `.gitignore`/`.dockerignore` 双重排除；DeepSeek key 只走 Render 环境变量
- 本地无 `.env` 也能跑：检索、拒答、版本对比全部可用，仅场景 1 的 LLM 组织回答路径报"缺 key"提示

## 数据浏览（Datasette）

```bash
bash scripts/serve_datasette.sh    # 只读模式，端口 8001
```

预置查询入口（首页 /course_kb 可见，或直接访问 `/course_kb/<查询名>`）：

| 查询 | 内容 |
|---|---|
| `chunks_by_version` | 各版本块数对比（按 content_type） |
| `qc_latest` | 质检报告：三版本最新一轮指标与结论 |
| `cleaning_by_rule` | 清洗规则命中聚合 |
| `eval_latest` | 检索评估最近一次运行逐题明细 |

## 目录结构

```
course-kb-rag/
├── data/raw/              # 源文件（只读，sha256 入台账）
├── data/interim/          # 解析/清洗中间文本（可删可重建）
├── data/export/           # dataset_v*.jsonl 导出
├── data/logs/             # qa 调用日志
├── db/course_kb.db        # 单文件事实库 + 向量索引（派生物，可重建）
├── eval/questions.jsonl   # 检索评测集（23 题）
├── src/                   # ingest/parse/clean/chunk/annotate/export/qc/index/eval_retrieval/retrieve/qa/pipeline
├── scripts/               # serve_datasette.sh + datasette-metadata.yaml
├── docs/                  # architecture.md、cleaning-rules.md、annotation-spec.md、qc 样本、eval 报告
├── tmp/                   # 一次性中间产物
└── pyproject.toml         # 依赖全部固定版本
```

## 可复现性

- 依赖全部固定版本（pyproject.toml）；raw 文件 sha256 在 documents 台账可校验
- 管线无随机环节（切块确定性、检索确定性）；qc 抽检样本固定 seed=42
- embedding 版本化：换模型会被 index_runs 校验拦下，强制重建索引
