# CHANGELOG · course-kb-rag

记录项目每一步的决策与产出。格式：日期 + 事项 + 关键决策/产出。

## 2026-10-07 · 项目启动日

### 需求定义（调研驱动）

- 背景：目标岗位为出版社"AI 产品 / 高质量数据集"方向（人教社高层次人才招聘"高质量数据集项目负责人"，截止 2026-10-30）。
- 三路并行调研结论：政策线（十五五规划点名教育高质量数据集、国家数据局 2026-06 行动方案、教育部"人工智能+教育"行动计划 2026-04）；行情线（2026 年 8-9 月 A 股"AI 语料"主线爆发，中国科传/中国出版领涨，但属题材驱动、基本面承压）；机构线（人教×小猿课本学习智能体 2026-08、高教社"龙凤"大模型已备案 + 数据标注岗位在招）。
- **需求定型**：教材知识库 + 溯源问答 demo。一个 demo 同时命中 JD 两条线（数据加工平台 + 智能产品场景验证）。

### 语料决策

- 初案"初中教材 PDF + 课标"被推翻：数学（公式 OCR 难度不兑换面试分）→ 语文 → **最终定为作者自有的《人工智能主题课程讲义》**。
- 关键考量：版权 100% 干净（出版业刚签《人工智能高质量语料库建设公约》"先授权后使用"，拿未授权教材去出版社演示等于在"合规"题上先扣分）；作者对内容有权威判断力（质检有底气）；可公开分发。
- 讲义"过时"被重新定性为优势：裸模型（2026 认知）vs RAG 系统（讲义口径）的对比演示，正是"受控口径"价值的直接证据，与候选人 NL2SQL/cube 语义层经验同构。

### 讲义版本线建立

- **v1.0**：原始归档（71 页 14 课，第 5 课物理页序乱序，缺第 15 课）。决策：**raw 层不可变**，不修源文件，由管线发现、质检记录。
- **v1.1**：结构修订（73 页 15 课）。pypdf 重排页序 + 第 15 课 docx 经 typst 排版渲染并入。机器上 Word COM 自动化失败（疑似 WPS 残留注册），改用 typst 路线。
- **事实口径确认**：16 课时 = 15 课讲义 + 1 课时总复习/展示课（展示课无讲义）。已回写求职仓库 `01-candidate-profile.md`，同时把 RAG 表述降级为"写过 chunking 原型"。

### 架构设计与外审

- 架构文档 v0.1 → 外审 → **v0.2**。外审 4 条 blocking 全部成立并落实：检索口径（默认只查 active 版本未 supersede 的块）、embedding 版本化（index_runs 表）、中间产物落点（interim 文件 + cleaning_log 落库）、检索质量评估（eval 集 + recall@5/MRR + 阈值标定）。
- 兼容性风险当场消除：Python 3.13 + sqlite-vec 冒烟测试通过（KNN 正确，SQLite 3.45.3）。
- 已定决策：sqlite-vec（单文件叙事）、bge-small-zh-v1.5（fastembed 本地）、问答 LLM 用现有 DeepSeek key、标注走课级下发+栏目映射。

### 讲义审读与 v1.2

- 5 路并行审读 15 课，产出 `docs/讲义审读报告.md`：**28 项问题**（16 项事实硬伤 + 5 项示例代码错误 + 7 项前后不一致 + 系统性标点问题）。最严重：第 14 课算术错误（9700 万 − 8500 万 = 1200 万，误写"1.2 亿"）。
- **v1.2**：34 处 PDF 手术修复（PyMuPDF redact + 字体匹配回写，脚本 `src/fix_v12.py` 可重跑）。第 15 课无需改（问题在原 docx，typst 版本就干净）。引号系统性问题与第 3 课低龄化段落留待重排（假想 v1.3）。
- 注意点记录在架构文档：v1.2 插入文本在内容流末尾，**解析层抽取必须 `sort=True`**。

### M1 归集台账 ✅

- 交付：`pyproject.toml`（依赖固定）、`.gitignore`、`src/schema.sql`（7 张表 DDL）、`src/db.py`、`src/ingest.py`。
- 验收：documents 4 行（v1.0/v1.1/v1.2 + docx component）；sha256 幂等（重跑 0 新增）；删库可重建；pipeline_runs 留痕。
- 规格补记：documents.status 增加 `component` 取值（源组件，如并入 v1.1+ 的 docx）；清理了 raw 目录里 Word COM 失败时写出的残留 PDF（移入 tmp/）。

---

### M2 解析 + 清洗 ✅

- 交付：`src/parse.py`（PyMuPDF sort=True 抽取、课标题正则含中文数字与艺术排版兜底、kp_declared 抽取、课内原始文本落盘）、`src/clean.py`（4 条规则逐条留痕 cleaning_log、`docs/cleaning-rules.md` 自动生成）。
- 验收：课数 v1.0=14 / v1.1=15 / v1.2=15；v1.0 乱序如实入表（L06 p22-26 在 L05 p27-31 前）；kp 抽取 14/14、14/15、14/15（L15 无知识点一览节，NULL）；清洗三版本合计命中 1783 处（page_number_line 210 / whitespace 1534 / pua_bullet 39 / image_caption_dup 0）；重跑幂等。
- 数据新发现：**v1.1/v1.2 的印刷页码跨课不递增**——pypdf 重排把页脚随页搬走，物理 p22-26（第 5 课）印的是 26-30，p27-31（第 6 课）印的是 21-25。课内递增、跨课断档。M3 切块"页码区间"与 M6 出处"P12-13"需先定口径（物理页 or 印刷页）。
- 新发现噪声：PUA 私用区项目符号（Wingdings U+F06C / Symbol U+F0B7），新增 `pua_bullet` 规则替换为「•」。
- 第 3 课标题为艺术排版（sort=True 仍乱序成「课：…第3」），兜底正则按课号归位，标题正文取回但括号副标题"（AI 的数学基础）"丢失——title 字段非权威展示字段，课号才是真源。

### M3 切块 + 标注 ✅

- 前置：parse.py 落 parsed 文本时每页开头插页标记 `⟦Pn⟧`（clean 原样通过不留痕），chunk 消费标记还原块级 page_start/page_end（物理页码）。重跑 parse+clean 课数与清洗命中数不变。
- 交付：`src/chunk.py`（节切块 + 栏目启发式 + 大小控制）、`src/annotate.py`（kp_declared 课级下发 + content_type 校验 + 自动生成 `docs/annotation-spec.md`）、`src/export.py`（JSONL 导出）。
- 产出：块数 v1.0=286 / v1.1=299 / v1.2=298，均长 ~155 字，全部 ≤800、无空块、外键/页码区间零异常；knowledge_tags 课级下发，L15 空数组属预期；`data/export/dataset_v{1.0,1.1,1.2}.jsonl` 行数与 chunks 表一致。
- 栏目判定两次校准：① 栏目块"收到内容后遇空行即收束"，防吞正文；② 数字小节（1. xxx）作为块边界归正文。残留误判（短冒号行误为栏目头、多段活动截断）记入 annotation-spec 已知局限，待 M4 人工抽检量化。
- pipeline_runs.stage 增加 `export` 取值（schema 注释原为 ingest/parse/clean/chunk/annotate/qc/index）。

### M4 质检 ✅（含 M3 遗留修正）

- 课标题块：每课首块仅含课标题者 content_type 改记 `课标题`（13/14/14——v1.0 第 3 课标题乱序行不匹配，归正文，已写入 annotation-spec 备注）；保留入库，index 阶段排除。
- 切块修正二则（qc 首轮抓出空块率 2.3% 超标）：① 数字小节规则不再收束「课后任务」块（原把任务编号列表拆碎）；② 残余短块兜底合并——标题/标签样短块前挪并入后块，其余并入前块（课标题块除外）。修正后块数 221/230/230，空块率归零。**教训：qc 指标真能反哺切块规则。**
- 交付 `src/qc.py`：simhash（字符 3-gram + blake2b 64 位，stdlib）近重复检测 hamming≤3，排除课标题块、只算 text；lesson_order_ok 课序检查；metrics JSON 含缺课清单/长度分布/content_type 分布；人工抽检样本（seed=42，20 块三项判断 + 10 块标注下发自动核对）落 `docs/qc-sample-{version}.md`，附可直接执行的 human_sample 回填 SQL。
- 结果：v1.0 不通过（乱序 + 缺第 15 课，预期内），v1.1/v1.2 通过；dup_rate 全 0%；标注下发一致率 10/10。

### M5 向量索引 + 混合检索 + 检索评估 ✅

- 依赖启用并固定：fastembed==0.8.1 / jieba==0.42.1 / rank_bm25==0.2.2（onnxruntime 1.30.0 随 fastembed 进入，Python 3.13 兼容）。
- 交付：`src/index.py`（bge-small-zh-v1.5 dim=512 L2 归一化，640 块全量重建 chunk_vectors，排除课标题块；index_runs 版本化 + check_index 校验）、`src/retrieve.py`（向量 k=20 + jieba/BM25 k=20，RRF(k=60) 融合 top-5；默认 active 口径，scope_doc_id 放开版本过滤；BM25 按文档分语料——idf 依赖语料，scope 必须先过滤再打分）、`src/eval_retrieval.py`（23 题评测 + 阈值标定 + 落库 eval_runs/eval_results + `docs/eval-report-m5.md`）。
- 指标：recall@5 = 100%（18/18），MRR = 0.917，应拒答 5/5 全拒，误拒率 5.6%（仅 q04）。
- **规格偏离（已记录待裁决）**：§4.4「RRF top-1 阈值拒答」实测不可行——RRF 只含名次无绝对相关性，q22（红烧肉）RRF 0.0328 为全场最高之一，两类题 RRF 区间完全重叠。改用双信号与门：top-5 内 maxVec < 0.1512 且 maxBM25 < 11.87 → 拒答（阈值由评测集标定，标定过程在报告）。q20（YOLO，与 OpenCV 课语义相近 maxVec=0.150）是最难拒的题；q04（线性组合，双弱信号）是唯一误拒代价。
- schema 补 eval_runs / eval_results 两表（§6 原本没有，按既有风格补并同步 src/schema.sql）；pipeline_runs.stage 实际取值再增 `eval`。

### 架构文档 v0.3（owner 裁定 M5 两处规格修订）

- §4.4 拒答口径：RRF top-1 阈值 → 双信号与门（top-5 maxVec < 0.1512 且 maxBM25 < 11.87），阈值 eval 集标定，换模型/改评测集须重新标定；q20 余量 0.002 为已知风险。
- §7 eval 契约「只读」→「只读源数据，结果追加落库」；§6 pipeline_runs.stage 注释补 eval。

### M6 问答 ✅（LLM 场景待 key 验收）

- 交付 `src/qa.py`：检索（复用 retrieve.py，--doc-version 放开版本过滤）→ 双信号与门（规则读最新 eval_runs.threshold）→ 命中才调 DeepSeek（openai SDK 兼容模式，key 走 .env 的 DEEPSEEK_API_KEY，缺失时明确报错不硬编码）；系统提示词强制只依据检索块；出处用 cite() 物理页码；调用日志落 `data/logs/qa_calls.jsonl`（query/命中块/拒答/模型/token/耗时）。
- 已验证（无 LLM 路径）：超纲题（量子力学）拒答未调 LLM ✓；key 缺失时命中题给出明确指引 ✓；eval 集 q23 措辞在 v1.0 上拒答 ✓（版本对比机制成立）。
- **发现（待裁决）**：§10 场景 3 的措辞「AI 的未来趋势有哪些？」在 v1.0 上**不拒答**——vec=0.206 命中 L7「未来展望：让AI向善而行」段。v1.0 确有未来相关内容，判定"有依据"并非全错；eval 集 q23 措辞（"在第几课？讲了什么？"）则正确拒答。该题不在评测集，无法靠重标定分离（会误伤可答题）。选项：a) 演示改用 q23 措辞；b) 接受 v1.0 从 L7 作答（语义上站得住）；c) 把该措辞补进评测集重新标定（预计不可行）。

### M7 呈现 + 可复现 ✅

- 出处修复：retrieve.cite() 节标题为空时省略节段（116 个无节块原会显示「第10课 · · P50」，现为「第10课 · P50-50」）。
- 交付 `src/pipeline.py`：ingest→parse→clean→chunk→annotate→export→qc→index→eval 全链路（§11），各阶段可单独跑；eval 只检索不调 LLM，all 全程无需 API key。**干净副本重建验证通过**：挪走 db+interim+export 重跑 all，9 阶段输出与既有状态逐项一致（4 文档 / 14+15+15 课 / 221+230+230 块 / 640 向量 / recall@5=100% MRR=0.917），验证后恢复原 db（保留 pipeline_runs 历史）并重跑 qc 使样本文件 run_id 重新对齐。
- Datasette：`scripts/serve_datasette.sh`（只读 -i，端口 8001）+ `scripts/datasette-metadata.yaml`（4 个预置查询：各版本块数对比 / qc 最新一轮 / 清洗规则聚合 / eval 最近一次逐题明细）。datasette==0.65.5 固定。冒烟：首页 + documents/chunks/chunk_vectors + 4 个 canned query 全部 200，验证后关进程。**Windows 两坑**：metadata 文件被按 GBK 读 → PYTHONUTF8=1；--load-extension 按「路径：入口」解析被 C:\ 盘符冒号误切 → vec0.dll 复制到仓库相对路径加载（scripts/vec0.dll 已入 .gitignore）。
- 架构文档 §6 补齐 eval_runs/eval_results DDL（与 src/schema.sql 一致），§11 一键跑通命令补 export/eval 阶段。
- README.md：环境要求 / 一键跑通预期输出 / .env 说明 / 三场景演示动线 / Datasette 入口 / 目录结构 / 可复现性。场景 3 用裁定措辞 q18「讲义中认为 AI 未来有哪些发展趋势？」——v1.0 从第 1/4/7 课拼凑浅层答案，v1.2 精准命中第 15 课；叙事为「旧版不是答不了，而是只能拼凑」。
- 注意：本次验证期间 owner 有过一次 commit，把临时目录 tmp/m7_backup/ 提交了进去；该目录已在工作区删除（git status 显示 D），下次 commit 自然清掉。

### 公网部署准备（v0.4，Render 免费档）

- **密码闸门**（src/serve.py）：`DEMO_TOKEN` 环境变量设置即启用——GET / 出瑞士风密码页（独立内联小页面，沿用白底点阵 + 克莱因蓝 + 硬边框），POST /api/login 校验通过后发 HttpOnly Cookie（值为 token 的 sha256 + hmac 恒定时间比较，不落明文）；无有效 Cookie /api/ask 返回 401。未设置则完全无闸，本地体验不变。
- **速率限制**：/api/ask 按客户端 IP（尊重 X-Forwarded-For）内存滑动窗口 10 次/分（`RATE_LIMIT_PER_MIN` 可调），超限 429；无论是否设闸门都启用——护住 DeepSeek 调用费用。
- 部署适配：PORT 读环境变量（Render 注入 $PORT），新增 GET /healthz（恒 200，过闸门前）。
- **Dockerfile**（python:3.13-slim）：pip install . → 构建期预热 bge-small-zh 模型进镜像层（防冷启动下载超时）→ COPY web/eval/db/course_kb.db → CMD python -m src.serve。`.dockerignore` 排除 .env/.git/tmp/data 各层/docs。
- **数据库入库准备**：.gitignore 放行 `!db/course_kb.db`（事实库随仓库分发）；误提交的空库 `data/kb.sqlite`（0 字节早期残留）加入忽略，待 owner git rm --cached。
- README 加「公网部署」节：Render 步骤、三个环境变量、闸门+限流的安全叙事、无 .env 可跑的现状。
- 验收（本地全过）：无闸门模式 / + /api/ask 正常；DEMO_TOKEN=test123 模式下密码页 / 401 / 错误密码提示 / 正确密码 cookie 通行；连打 11 次第 11 次 429；/healthz 200。本机无 docker，镜像构建未实测（Dockerfile 已就绪）。

## 2026-10-08 · tokenizer [UNK] 缺陷修复（真实使用驱动的闭环）

- **真实使用 → 发现**：部署前用用户真实提问实测 CLI，「AI是什么意思？」被双信号与门误拒；深挖发现 bge-small-zh-v1.5 tokenizer 把全大写拉丁缩写（AI/AIGC/CNN/GPT/RGB/HSV/NLP…）整体映射为 `[UNK]`，"AI" 与 "AIGC" embedding cos=1.0000（语义全毁），小写正常。
- **定位**：嵌入（embed_texts）与 BM25（tokenize）两条路径均未做大小写归一化，索引/查询两侧对称中招。
- **修复**：`src/index.py` 增 `normalize_text`（`str.lower()`，CJK 不受影响）嵌入侧生效；`src/retrieve.py::tokenize` 同步小写；索引全量重建。修复后 "AI" vs "AIGC" cos=0.7960，语义区分恢复。
- **顺手修复标定缺陷**：eval 标定的 tv/tb 中点此前取整落库（round 4/2 位），tv 中点 0.17415 被截为 0.1741，恰低于边界应拒答题 q23 的 maxVec=0.17413 致其漏拒；阈值改存全精度浮点。
- **防回归**：eval 集追加 q24-q28 拉丁缩写词义题（RNN 为应拒答）；重跑 28 题：recall@5=100%（22/22）、MRR=0.955、应拒答 6/6 全拒、误拒 9.1%（q25 AIGC / q27 CNN——双信号被 q23 两维占优，单调与门无兼得操作点，如实记录未改评测题）。阈值 0.1512/11.87 → 0.174150/9.3972。
- CLI 实测：「AI是什么意思？」✓ 带出处（第1课）；「transformer是什么意思？」✓（第7课）；「RNN是什么意思」✓ 正确拒答；「AIGC是什么意思？」✗ 被与门误拒（遗留代价，备选方案见 eval-report-m5.md 附录节）。
- 文档同步：架构文档升 **v0.4**（§4.4 阈值更新 + tokenizer [UNK] 风险登记；与昨日部署准备条目共用 v0.4 版本号）；cleaning-rules.md 登记 `latin_lower` 归一化规则（已并入 clean.py 生成器，重跑不丢）。

### 同日后续：q23 移出阈值标定集（owner 裁定，方案①）

- **裁定**：q23（归档版本 doc_001 scope 的版本对比陷阱题）双信号两维占优 q25/q27 致其被与门误拒。owner 裁定将 q23 移出标定集——生产/演示默认只查 active 版本，且场景 3 叙事早已改为「q18 两版都能答、对比质量差异」，不依赖 q23 拒答。
- **实施**：q23 保留在 eval/questions.jsonl 并加 `"calibration": false`（note 写明原因）；`eval_retrieval.py` 的 `calibrate()` 跳过 calibration=false 题，拒答率/误拒率分母同步只算标定集；报告仍展示 q23 实际表现（当前阈值下未拒答，标定外信息项，生产路径不可达）。
- **重新标定**：tv 0.174150 → **0.091300**，tb 9.3972 → **9.2569**（全精度落库）。28 题终态：recall@5=100%（22/22）、MRR=0.955、应拒答（标定集 5 题）5/5 全拒、误拒率 4.5%（仅 q27）。
- **验收实测**：CLI「AI/AIGC/transformer 是什么意思？」全部带出处作答（第 1/13/7 课）；**演示场景 3 修复确认**——q18 在 v1.2 精准命中第 15 课、v1.0（--doc-version v1.0）从第 1/7/13 课拼凑浅层答案，两版均正常作答（前端 agent 报告的"新阈值下两版都拒答"已修回）；q19/q20/q21/q22/q28 全拒。
- **已知残余**：q27「CNN是什么意思？」仍误拒（maxVec=0.021 确实太弱，讲义仅 L8 提及非专讲），owner 裁定接受，未硬降阈值。

### 同日晚间：查询理解层（第二次真实提问驱动，架构升 v0.5）

- **真实使用 → 发现**：「我想知道第一课有什么作业」被与门误拒（maxVec=0.0064）；「第1课的思考题」top-1 张冠李戴到第 3 课（正文含"第1课"交叉引用）。根因：课号是元数据不在正文里 + 讲义从不用口语词"作业"（用思考题/课后任务）。
- **实现（src/retrieve.py）**：`parse_lesson_no`（中文/阿拉伯数字课号，召回阶段 scope 到 lesson_no，BM25 语料同步收缩保 idf 口径）+ `SYNONYM_EXPANSIONS` 保守同义词扩展（仅作业/讨论/练习题三组，嵌入与 BM25 共用扩展后文本）；课号不存在 → 空 scope → 拒答。
- **拒答口径配套修订**：课内小语料绝对分值与全库标定的与门阈值不可比（实测课内命中 maxVec≈0.006 必误拒；若入标定集则 calibrate 不可行），课号 scope 题不走与门——仅空 scope 拒答，有块交 LLM 依据块作答。qa.should_abstain 与 eval 同口径。
- **防回归**：eval 追加 q29-q31（calibration=false，note 写明原因）。31 题：recall@5=100%（25/25）、MRR=0.960、应拒答 5/5、误拒 4.5%（仅 q27）；**阈值未变**（0.091300/9.2569），不带课号题目行为不变（抽查 q24 ✓ 作答、q19 量子计算 ✓ 拒答）。
- CLI 实测三题全过且出处课号正确：第一课作业 → L1 课后任务块；第1课思考题 → 第1课；第五课讨论问题 → L5 思考题×3+延伸挑战。
- 文档：架构升 **v0.5**（§4 新增第 8 条查询理解；§4.4 补与门例外口径）；eval-report-m5.md 附二记录。

## 经验总结（随项目持续补充）

1. **raw 不可变 + 版本归集**是数据治理的基本功：源文件的问题（乱序/缺课/事实错误）不在源头修，而是归集新版本——这让"版本管理"从演示台词变成真实操作。
2. **外审的价值在 blocking 项**：4 条 blocking 里有 2 条（检索口径、检索评估）是"想得对但没落到文字"的口径缺失——架构文档最容易漏的不是模块，是口径。
3. **兼容性冒烟要先行**：sqlite-vec + Python 3.13 花了 5 分钟验证，避免 M5 才发现装不上。
4. **PDF 手术修订可行但有章法**：redact + 字体匹配回写适合点状事实修改；斜体字符 bbox 比字宽大 40%（局部 redact 会误删相邻字，须整行重写）；插入文本进内容流末尾，文本抽取须 sort=True；保存前 subset_fonts() 防体积膨胀。结构性问题（引号体系、段落风格）不做手术，留待重排。
5. **环境问题当素材不当障碍**：Word COM 失败 → typst 路线反而成了"多格式归一化"的演示素材；乱序/缺课 → 质检的真实案例。数据项目里"脏数据"是特性不是 bug。
6. **并行审读方法**：15 课拆 5 路并行通读，要求"宁缺毋滥 + 排除抽取伪影"，事实核查需联网验证（YOLOv4 帧率、Strava 年份等均为联网核实）。
7. **Windows 环境坑**：Git Bash 控制台 GBK，Python 脚本需 `sys.stdout.reconfigure(encoding="utf-8")`；pypdf 不接受 Git Bash 的 /tmp 路径。
