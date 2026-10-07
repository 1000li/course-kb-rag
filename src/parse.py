"""M2a 解析层：PDF 文字层 → 课边界识别 → 课内原始文本。

- 文本抽取：PyMuPDF get_text(sort=True)（v1.2 手术插入文本在内容流末尾，必须 sort）
- 课号以课标题为真源，不依赖物理页序；page_start/page_end 记录真实物理页码区间
- docx（component）不解析，skip 记入 pipeline_runs.stats
- 幂等：同 doc 重跑先删 lessons 行再插入；每个 doc 写一条 pipeline_runs

用法：py -3 -m src.parse
"""

import json
import re
import sys
import uuid
from pathlib import Path
from datetime import datetime, timezone

import pymupdf

from .db import ROOT, init_db

sys.stdout.reconfigure(encoding="utf-8")

RAW_DIR = ROOT / "data" / "raw"
PARSED_DIR = ROOT / "data" / "interim" / "parsed"

# 课标题：兼容「第 5 课：」「第5课：」「第五课：」
TITLE_RE = re.compile(r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*课\s*[：:]\s*(.+?)\s*$")
# 兜底：第 3 课标题为艺术排版，sort=True 后语序仍乱（「课：…第3」同行逆序）
TITLE_FALLBACK_RE = re.compile(r"课\s*[：:]\s*(?P<title>.+?)\s*第\s*(?P<num>[0-9]+)\s*$")

# 知识点一览引导语，各课措辞略有差异：
#   本课涉及到的知识点一览：/ 本节课涉及到的知识点：/ 本节课涉及的知识点：/ 本课涉及的知识点一览：
KP_HEADER_RE = re.compile(r"本(?:节)?课涉及(?:到的|的)知识点(?:一览)?\s*[：:]")
KP_ITEM_RE = re.compile(r"^\s*(\d+)\s*[、.．]\s*(.+)")
SECTION_RE = re.compile(r"^[一二三四五六七八九十]+\s*[、.]")

CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}

# 艺术排版标题的人工补全（兜底正则丢失的部分），键为课号
TITLE_OVERRIDES = {3: "像游戏设计师一样思考数学的力量（AI 的数学基础）"}


def cn_to_int(s: str) -> int:
    """中文数字一至十五 → int。"""
    if s.isdigit():
        return int(s)
    if s in CN_DIGITS:
        return CN_DIGITS[s]
    if s == "十":
        return 10
    if s.startswith("十"):  # 十一..十五
        return 10 + CN_DIGITS[s[1]]
    if "十" in s:  # 一十.. 之类，防御
        a, b = s.split("十")
        return CN_DIGITS.get(a, 1) * 10 + (CN_DIGITS.get(b, 0) if b else 0)
    raise ValueError(f"无法识别的中文数字: {s}")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def find_titles(pages: list[str]) -> list[dict]:
    """按物理页序扫描课标题，返回 [{lesson_no, title, page}…]。"""
    hits = []
    for pno, text in enumerate(pages, start=1):
        for line in text.splitlines():
            m = TITLE_RE.search(line)
            if m:
                hits.append({"lesson_no": cn_to_int(m.group(1)),
                             "title": m.group(2).strip(), "page": pno})
                break
            fb = TITLE_FALLBACK_RE.search(line.strip())
            if fb:
                # 逆序排版：标题主体在「课：」与「第N」之间，括号副标题丢失，如实记录
                hits.append({"lesson_no": int(fb.group("num")),
                             "title": fb.group("title").strip(), "page": pno,
                             "title_recovered": True})
                break
    return hits


def extract_kp(first_page_text: str) -> list[str] | None:
    """从课首页文本抽取「知识点一览」编号列表；抽不到返回 None。

    条目可跨行（续行拼回上一条）；遇到空行后的非条目行或节标题即停止。
    """
    lines = first_page_text.splitlines()
    start = next((i for i, l in enumerate(lines) if KP_HEADER_RE.search(l)), None)
    if start is None:
        return None
    items: list[str] = []
    gap = False
    for raw in lines[start + 1:]:
        s = raw.strip()
        if not s:
            gap = True
            continue
        m = KP_ITEM_RE.match(s)
        if m and int(m.group(1)) == len(items) + 1:
            items.append(m.group(2).strip())
            gap = False
            continue
        if not items:
            continue  # 引导语与第一条之间的杂行
        if gap or SECTION_RE.match(s):
            break
        items[-1] += s  # 续行
        gap = False
    return items or None


def parse_doc(conn, doc_id: str, path, version: str) -> dict:
    path = Path(path)
    pdf = pymupdf.open(path)
    pages = [p.get_text(sort=True) for p in pdf]
    n_pages = len(pdf)
    pdf.close()

    hits = find_titles(pages)
    # 课区间：标题页 → 下一个标题页前一页（物理顺序；末课到文档尾）
    lessons = []
    for i, h in enumerate(hits):
        end = (hits[i + 1]["page"] - 1) if i + 1 < len(hits) else n_pages
        lessons.append({**h, "page_start": h["page"], "page_end": end})

    # 幂等：同 doc 重跑覆盖
    conn.execute("DELETE FROM lessons WHERE doc_id = ?", (doc_id,))
    out_dir = PARSED_DIR / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)

    kp_ok, kp_fail, recovered = 0, [], []
    for les in lessons:
        les["title"] = TITLE_OVERRIDES.get(les["lesson_no"], les["title"])
        kp = extract_kp(pages[les["page_start"] - 1])
        if kp:
            kp_ok += 1
        else:
            kp_fail.append(les["lesson_no"])
        if les.get("title_recovered"):
            recovered.append(les["lesson_no"])
        lesson_id = f"{doc_id}_L{les['lesson_no']:02d}"
        conn.execute(
            """INSERT INTO lessons
               (lesson_id, doc_id, lesson_no, title, page_start, page_end, kp_declared)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (lesson_id, doc_id, les["lesson_no"], les["title"],
             les["page_start"], les["page_end"],
             json.dumps(kp, ensure_ascii=False) if kp else None),
        )
        # 每页开头插页标记 ⟦P{物理页码}⟧，供 chunk 阶段还原块级页码区间
        text = "\n".join(
            f"⟦P{pno}⟧\n{pages[pno - 1]}"
            for pno in range(les["page_start"], les["page_end"] + 1)
        )
        (out_dir / f"L{les['lesson_no']:02d}.txt").write_text(text, encoding="utf-8")

    # 更新派生字段 lessons_count（非权威，真源 = lessons 表）
    conn.execute("UPDATE documents SET lessons_count = ? WHERE doc_id = ?",
                 (len(lessons), doc_id))

    order = [l["lesson_no"] for l in sorted(lessons, key=lambda l: l["page_start"])]
    return {
        "lessons": len(lessons), "pages": n_pages, "kp_ok": kp_ok,
        "kp_fail": kp_fail, "title_recovered": recovered,
        "lesson_no_by_page": order,
        "lesson_order_monotonic": order == sorted(order),
    }


def main() -> int:
    conn = init_db()
    docs = conn.execute(
        "SELECT doc_id, version, path, format FROM documents ORDER BY doc_id"
    ).fetchall()
    for d in docs:
        started = utcnow()
        run_id = f"parse_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        if d["format"] != "pdf":
            stats = {"skipped": True,
                     "reason": f"{d['doc_id']} 为 docx 源组件，内容已并入 v1.1+，不单独解析"}
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'parse', ?, ?, ?, ?, 'success')""",
                (run_id, d["version"], started, utcnow(),
                 json.dumps(stats, ensure_ascii=False)))
            conn.commit()
            print(f"[跳过] {d['doc_id']} ({d['version']}): {stats['reason']}")
            continue
        try:
            stats = parse_doc(conn, d["doc_id"], str(ROOT / d["path"]), d["version"])
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'parse', ?, ?, ?, ?, 'success')""",
                (run_id, d["version"], started, utcnow(),
                 json.dumps(stats, ensure_ascii=False)))
            conn.commit()
            print(f"[解析] {d['doc_id']} ({d['version']}): {stats['lessons']} 课 / "
                  f"{stats['pages']} 页 / kp 成功 {stats['kp_ok']}，"
                  f"失败课号 {stats['kp_fail']} / 乱序={not stats['lesson_order_monotonic']}")
        except Exception as e:
            conn.rollback()
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'parse', ?, ?, ?, ?, 'failed')""",
                (run_id, d["version"], started, utcnow(),
                 json.dumps({"error": str(e)}, ensure_ascii=False)))
            conn.commit()
            raise
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
