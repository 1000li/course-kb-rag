"""M3b 标注层：课级知识点下发 + content_type 校验。

- knowledge_tags：lessons.kp_declared（讲义自带「知识点一览」，JSON 数组）
  原样下发到该课所有块；kp_declared 为 NULL 的课（L15，typst 源无此节）下发空数组 []
- content_type 在切块阶段（chunk.py）判定，本阶段做校验：分布统计 + 空 tags 异常检查
- 生成 docs/annotation-spec.md（标注规范，含实时分布统计）
- 幂等可重跑；每 doc 一条 pipeline_runs（stage=annotate）

用法：py -3 -m src.annotate
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from .db import ROOT, init_db

sys.stdout.reconfigure(encoding="utf-8")

SPEC_MD = ROOT / "docs" / "annotation-spec.md"

CONTENT_TYPES = ["课标题", "正文", "知识点一览", "课堂活动", "思考题", "代码段", "课后任务"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def annotate_doc(conn, doc_id: str, version: str) -> dict:
    lessons = conn.execute(
        "SELECT lesson_id, lesson_no, kp_declared FROM lessons WHERE doc_id = ?",
        (doc_id,)).fetchall()
    tagged, empty_tag_lessons = 0, []
    for les in lessons:
        tags = json.loads(les["kp_declared"]) if les["kp_declared"] else []
        if not tags:
            empty_tag_lessons.append(les["lesson_no"])
        n = conn.execute(
            "UPDATE chunks SET knowledge_tags = ? WHERE lesson_id = ?",
            (json.dumps(tags, ensure_ascii=False), les["lesson_id"])).rowcount
        tagged += n
    dist = dict(conn.execute(
        "SELECT content_type, COUNT(*) FROM chunks WHERE doc_id = ? GROUP BY content_type",
        (doc_id,)).fetchall())
    # 异常检查：除 kp 为 NULL 的课（当前仅 L15）外，不应有空 tags 的块
    abnormal = conn.execute(
        """SELECT COUNT(*) FROM chunks c JOIN lessons l ON c.lesson_id = l.lesson_id
           WHERE c.doc_id = ? AND l.kp_declared IS NOT NULL
             AND (c.knowledge_tags IS NULL OR c.knowledge_tags = '[]')""",
        (doc_id,)).fetchone()[0]
    missing_ct = [ct for ct in CONTENT_TYPES if ct not in dist]
    return {
        "chunks_tagged": tagged,
        "empty_tag_lessons": empty_tag_lessons,
        "abnormal_empty_tags": abnormal,
        "content_type_dist": dist,
        "content_type_missing": missing_ct,
    }


def write_spec_md(conn, all_stats: dict) -> None:
    lines = [
        "# 标注规范（annotation spec）",
        "",
        "<!-- 本文档由 src/annotate.py 自动生成并注入实时统计，规则描述部分改动请改 annotate.py -->",
        "",
        f"生成时间：{utcnow()}",
        "",
        "## 1. content_type 取值与判定规则",
        "",
        "content_type 在切块阶段（src/chunk.py）判定，优先级从上到下：",
        "",
                "| 取值 | 判定规则 |",
        "|---|---|",
        "| 课标题 | 每课首块且内容仅为课标题（`第N课：…`，≤60 字）；保留入库，index 阶段排除。注：第 3 课标题为艺术排版乱序行，不匹配此规则，仍归正文 |",
        "| 知识点一览 | 课首「本课/本节课涉及（到）的知识点（一览）：」引导语 + 后续编号列表区，整块一个 chunk |",
        "| 课后任务 | 行首匹配「课后任务 / 课后探索地图」，或节标题含「课后任务」（如「六、课后任务与思考」）；从该行到课尾 |",
        "| 代码段 | 以代码起始行（import/from/def/class/if/while/for/print(/input(/#注释）开启，续行去注释后无 CJK 且含 `=(){}[],:.` 等代码字符（或 ≤2 字换行残片）；遇节标题/课后任务/正文句结束 |",
        "| 思考题 | 短前缀（≤12 字）+ 冒号的栏目头，前缀含「思考」（思考题/思考/情境思考/批判思考等） |",
        "| 课堂活动 | 同上栏目头，前缀含 实验/练习/练一练/活动/游戏/显微镜/任务/挑战/小组/动手/观察/讨论/扮演/制作/设计/实践/测试/调试/探索/卡片 |",
        "| 正文 | 默认。节标题（一、二、…）切大块，数字小节（1. xxx）归正文；按段贪心装包 |",
        "",
        "栏目块收束规则：栏目头之后收到至少一段内容，遇空行即收束（栏目内容多为单段），",
        "防止栏目块吞掉后续正文。",
        "",
        "## 2. knowledge_tags 来源与下发逻辑",
        "",
        "- 来源：讲义每课课首自带的「知识点一览」编号列表，解析层抽为 `lessons.kp_declared`（JSON 数组）。",
        "- 下发：该课所有块（不分 content_type）的 knowledge_tags = 本课 kp_declared 原样数组。",
        "- 粒度为**课级**：不做逐块精标（架构文档 §12 决策 4）。",
        "",
        "## 3. 已知局限",
        "",
        "- 栏目判定是关键词启发式： prose 行若短且以冒号结尾并含活动关键词会被误判为栏目头；",
        "  多段落活动块会在首个空行处截断，余段归入正文。准确率由 M4 人工抽检量化。",
        "- L15（第15课，docx→typst 源）无「知识点一览」节，knowledge_tags 为空数组 `[]`，属预期而非异常。",
        "- 知识标签为课级粒度：块与标签是「相关」而非「精确对应」关系。",
        "- 标签筛选走全表 LIKE（架构文档 §6 注），数据量小可接受。",
        "",
        "## 4. 实时统计（本次运行）",
        "",
    ]
    for doc_key, stats in all_stats.items():
        lines.append(f"### {doc_key}")
        lines.append("")
        lines.append("| content_type | 块数 |")
        lines.append("|---|---|")
        for ct in CONTENT_TYPES:
            lines.append(f"| {ct} | {stats['content_type_dist'].get(ct, 0)} |")
        lines.append("")
        lines.append(f"- 下发块数：{stats['chunks_tagged']}；空 tags 课：{stats['empty_tag_lessons']}"
                     f"；异常空 tags 块（kp 非空课内）：{stats['abnormal_empty_tags']}")
        lines.append("")
    SPEC_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    conn = init_db()
    docs = conn.execute(
        "SELECT doc_id, version FROM documents WHERE format = 'pdf' ORDER BY doc_id"
    ).fetchall()
    all_stats = {}
    for d in docs:
        doc_id, version = d["doc_id"], d["version"]
        started = utcnow()
        run_id = f"annotate_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            stats = annotate_doc(conn, doc_id, version)
            status = "success" if stats["abnormal_empty_tags"] == 0 else "failed"
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'annotate', ?, ?, ?, ?, ?)""",
                (run_id, version, started, utcnow(),
                 json.dumps(stats, ensure_ascii=False), status))
            conn.commit()
            all_stats[f"{doc_id} ({version})"] = stats
            print(f"[标注] {doc_id} ({version}): 下发 {stats['chunks_tagged']} 块，"
                  f"空tags课 {stats['empty_tag_lessons']}，"
                  f"异常 {stats['abnormal_empty_tags']} → {status}")
        except Exception as e:
            conn.rollback()
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'annotate', ?, ?, ?, ?, 'failed')""",
                (run_id, version, started, utcnow(),
                 json.dumps({"error": str(e)}, ensure_ascii=False)))
            conn.commit()
            raise
    write_spec_md(conn, all_stats)
    conn.close()
    print(f"\n标注规范已生成：{SPEC_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
