"""M2b 清洗层：课内原始文本 → 规则化去噪，命中逐条留痕。

- 输入：data/interim/parsed/{doc_id}/L{nn}.txt（lessons 表为课清单真源）
- 输出：data/interim/cleaned/{doc_id}/L{nn}.txt + cleaning_log 落库
- 规则清单与三版本合计命中数自动生成 docs/cleaning-rules.md
- 幂等：重跑先清该 doc 的 cleaning_log 行；每个 doc 写一条 pipeline_runs

用法：py -3 -m src.clean
"""

import json
import re
import sys
import uuid
from datetime import datetime, timezone

from .db import ROOT, init_db

sys.stdout.reconfigure(encoding="utf-8")

PARSED_DIR = ROOT / "data" / "interim" / "parsed"
CLEANED_DIR = ROOT / "data" / "interim" / "cleaned"
RULES_MD = ROOT / "docs" / "cleaning-rules.md"

# CJK 及 CJK 标点（用于「CJK 之间的半角空格删除」）
CJK = r"\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef\u2018\u2019\u201c\u201d\u2014\u2026"
CJK_SPACE_RE = re.compile(f"(?<=[{CJK}]) +(?=[{CJK}])")
DIGIT_LINE_RE = re.compile(r"\d{1,3}")
PUA_RE = re.compile("[\ue000-\uf8ff]")
PAGE_MARKER_RE = re.compile(r"⟦P\d+⟧")

RULES = {
    "page_number_line": "行首孤立页码行：整行仅 1-3 位数字且课内页码序列递增，判定为页脚页码，整行删除",
    "image_caption_dup": "连续重复的图片占位/图注行：相邻两行strip后相同且长度 ≤40，保留首行删除后续重复行",
    "whitespace": "空白规整：行首尾空白剔除；CJK（含CJK标点）之间的半角空格删除；连续空行压缩为一行",
    "pua_bullet": "PUA 私用区字符（Wingdings/Symbol 字体项目符号，如 U+F06C/U+F0B7）替换为标准项目符号「•」",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_lesson(text: str) -> tuple[str, list[tuple[str, str, str]]]:
    """返回 (干净文本, [(rule_id, line_content, action)])。

    顺序：页码行 → PUA 符号 → 行内空白 → 连续重复行 → 空行压缩。
    """
    logs: list[tuple[str, str, str]] = []
    out: list[str] = []
    last_page_no = -1  # 课内页码序列（页脚页码课内递增）

    for raw in text.splitlines():
        line = raw.strip()
        # 0. 页标记行 ⟦Pn⟧ 原样通过，不动也不留痕（chunk 阶段消费）
        if PAGE_MARKER_RE.fullmatch(line):
            out.append(line)
            continue
        # 1. 孤立页码行（课内递增序列）
        if DIGIT_LINE_RE.fullmatch(line) and int(line) > last_page_no:
            last_page_no = int(line)
            logs.append(("page_number_line", line[:200], "removed"))
            continue
        # 2. PUA 项目符号
        if PUA_RE.search(line):
            new = PUA_RE.sub("•", line)
            logs.append(("pua_bullet", line[:200], "replaced"))
            line = new
        # 3. 行内空白：CJK 间半角空格
        new = CJK_SPACE_RE.sub("", line)
        if new != line:
            logs.append(("whitespace", line[:200], "replaced"))
            line = new
        # 4. 连续重复行（图注/占位行）
        if line and len(line) <= 40 and out and out[-1] == line:
            logs.append(("image_caption_dup", line[:200], "removed"))
            continue
        out.append(line)

    # 5. 空行压缩（连续空行 → 一行）
    final: list[str] = []
    blank_run = 0
    for line in out:
        if line == "":
            blank_run += 1
            if blank_run == 1:
                final.append(line)
            else:
                logs.append(("whitespace", f"(连续空行第{blank_run}行)", "removed"))
        else:
            blank_run = 0
            final.append(line)
    # 首尾空行剔除
    while final and final[0] == "":
        final.pop(0)
    while final and final[-1] == "":
        final.pop()

    return "\n".join(final) + "\n", logs


def write_rules_md(conn) -> None:
    counts = dict(conn.execute(
        "SELECT rule_id, COUNT(*) FROM cleaning_log GROUP BY rule_id").fetchall())
    lines = [
        "# 清洗规则清单",
        "",
        "<!-- 本文件由 src/clean.py 自动生成，请勿手改；重跑 clean 即更新命中统计 -->",
        "",
        f"生成时间：{utcnow()}（三版本合计命中数来自 cleaning_log 全表聚合）",
        "",
        "| rule_id | 说明 | 命中数 |",
        "|---|---|---|",
    ]
    for rule_id, desc in RULES.items():
        lines.append(f"| `{rule_id}` | {desc} | {counts.get(rule_id, 0)} |")
    extra = set(counts) - set(RULES)
    for rule_id in sorted(extra):
        lines.append(f"| `{rule_id}` | （未在 RULES 中登记说明） | {counts[rule_id]} |")
    lines += [
        "",
        "聚合查询示例：`SELECT rule_id, action, COUNT(*) FROM cleaning_log GROUP BY rule_id, action`",
        "",
    ]
    RULES_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    conn = init_db()
    docs = conn.execute(
        """SELECT d.doc_id, d.version FROM documents d
           WHERE d.format = 'pdf' ORDER BY d.doc_id"""
    ).fetchall()
    for d in docs:
        doc_id, version = d["doc_id"], d["version"]
        started = utcnow()
        run_id = f"clean_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            conn.execute("DELETE FROM cleaning_log WHERE doc_id = ?", (doc_id,))
            lessons = conn.execute(
                "SELECT lesson_no FROM lessons WHERE doc_id = ? ORDER BY lesson_no",
                (doc_id,)).fetchall()
            out_dir = CLEANED_DIR / doc_id
            out_dir.mkdir(parents=True, exist_ok=True)
            hits: dict[str, int] = {}
            now = utcnow()
            for les in lessons:
                no = les["lesson_no"]
                src = PARSED_DIR / doc_id / f"L{no:02d}.txt"
                cleaned, logs = clean_lesson(src.read_text(encoding="utf-8"))
                (out_dir / f"L{no:02d}.txt").write_text(cleaned, encoding="utf-8")
                conn.executemany(
                    """INSERT INTO cleaning_log
                       (doc_id, lesson_no, rule_id, line_content, action, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [(doc_id, no, r, c, a, now) for r, c, a in logs])
                for r, _, _ in logs:
                    hits[r] = hits.get(r, 0) + 1
            stats = {"lessons": len(lessons), "hits_by_rule": hits,
                     "hits_total": sum(hits.values())}
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'clean', ?, ?, ?, ?, 'success')""",
                (run_id, version, started, utcnow(),
                 json.dumps(stats, ensure_ascii=False)))
            conn.commit()
            print(f"[清洗] {doc_id} ({version}): {len(lessons)} 课，命中 {stats['hits_total']} 处 {hits}")
        except Exception as e:
            conn.rollback()
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'clean', ?, ?, ?, ?, 'failed')""",
                (run_id, version, started, utcnow(),
                 json.dumps({"error": str(e)}, ensure_ascii=False)))
            conn.commit()
            raise
    write_rules_md(conn)
    conn.close()
    print(f"\n规则清单已生成：{RULES_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
