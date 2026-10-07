"""M4 质检层：chunks + lessons → qc_reports + 人工抽检样本。

自动指标（每版本一行 qc_reports，每次运行新增不覆盖）：
- chunk_count / empty_rate（text 空白或 <10 字占比）
- dup_rate：字符 3-gram simhash（stdlib blake2b，64 位），hamming ≤3 判近重复；
  只算 text（knowledge_tags 是元数据不参与）；排除 content_type='课标题'
  （课标题块互相天然相似）
- lesson_order_ok：lessons 按 page_start 排序时 lesson_no 单调递增 → 1 否则 0
- metrics JSON：lessons_count / 缺课清单 / 长度分布 / content_type 分布 / 超长块数
- conclusion：lesson_order_ok=1 且 empty_rate<1% 且 dup_rate<5% → 通过

人工抽检：seed=42 随机抽 20 块 → docs/qc-sample-{version}.md（三项待人工判断）；
另抽 10 块自动核对 knowledge_tags 与 kp_declared 一致性（写入同文件）。

用法：py -3 -m src.qc
"""

import hashlib
import json
import random
import statistics
import sys
import uuid
from datetime import datetime, timezone

from .db import ROOT, init_db
from .chunk import PIPELINE_VERSION

sys.stdout.reconfigure(encoding="utf-8")

DOCS_DIR = ROOT / "docs"
SAMPLE_SIZE = 20
TAGS_CHECK_SIZE = 10
SEED = 42
HAMMING_THRESHOLD = 3
EMPTY_MIN_CHARS = 10
DUP_EXCLUDE_TYPES = ("课标题",)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def simhash64(text: str) -> int:
    """字符 3-gram simhash（64 位，stdlib 实现）。"""
    grams = [text[i:i + 3] for i in range(max(len(text) - 2, 1))]
    v = [0] * 64
    for g in grams:
        h = int.from_bytes(hashlib.blake2b(g.encode("utf-8"), digest_size=8).digest(),
                           "big")
        for i in range(64):
            v[i] += 1 if h >> i & 1 else -1
    fp = 0
    for i in range(64):
        if v[i] > 0:
            fp |= 1 << i
    return fp


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def dup_rate(chunks: list[dict]) -> tuple[float, int]:
    """近重复率：有至少一个 hamming≤3 邻居的块占比（排除课标题块）。"""
    pool = [c for c in chunks if c["content_type"] not in DUP_EXCLUDE_TYPES]
    fps = [(c["chunk_id"], simhash64(c["text"])) for c in pool]
    dup_ids = set()
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            if hamming(fps[i][1], fps[j][1]) <= HAMMING_THRESHOLD:
                dup_ids.add(fps[i][0])
                dup_ids.add(fps[j][0])
    rate = len(dup_ids) / len(fps) if fps else 0.0
    return rate, len(dup_ids)


def qc_doc(conn, doc_id: str, version: str) -> dict:
    chunks = [dict(r) for r in conn.execute(
        """SELECT chunk_id, lesson_no, content_type, chapter_path, text,
                  char_count, knowledge_tags
           FROM chunks WHERE doc_id = ? ORDER BY chunk_id""", (doc_id,)).fetchall()]
    lessons = [dict(r) for r in conn.execute(
        "SELECT lesson_no, page_start, kp_declared FROM lessons WHERE doc_id = ?",
        (doc_id,)).fetchall()]

    chunk_count = len(chunks)
    empty_n = sum(1 for c in chunks if len(c["text"].strip()) < EMPTY_MIN_CHARS)
    empty_rate = empty_n / chunk_count if chunk_count else 0.0
    drate, dup_n = dup_rate(chunks)

    order = [l["lesson_no"] for l in sorted(lessons, key=lambda l: l["page_start"])]
    lesson_order_ok = int(order == sorted(order))
    missing = [n for n in range(1, 16) if n not in {l["lesson_no"] for l in lessons}]

    lens = sorted(c["char_count"] for c in chunks)
    metrics = {
        "lessons_count": len(lessons),
        "lessons_missing": missing,
        "len_min": lens[0] if lens else 0,
        "len_p50": int(statistics.median(lens)) if lens else 0,
        "len_p95": lens[min(int(len(lens) * 0.95), len(lens) - 1)] if lens else 0,
        "len_max": lens[-1] if lens else 0,
        "over_hard_max": sum(1 for c in chunks if c["char_count"] > 800),
        "content_type_dist": {r[0]: r[1] for r in conn.execute(
            "SELECT content_type, COUNT(*) FROM chunks WHERE doc_id = ? "
            "GROUP BY content_type", (doc_id,)).fetchall()},
        "dup_count": dup_n,
        "empty_count": empty_n,
        "lesson_no_by_page": order,
    }
    ok = (lesson_order_ok == 1 and empty_rate < 0.01 and drate < 0.05)
    conclusion = "通过" if ok else "不通过"
    reasons = []
    if lesson_order_ok == 0:
        reasons.append(f"课序乱序（按 page_start 排序 lesson_no={order}）")
    if missing:
        reasons.append(f"缺课：{missing}")
    if empty_rate >= 0.01:
        reasons.append(f"空块率 {empty_rate:.2%} ≥1%")
    if drate >= 0.05:
        reasons.append(f"近重复率 {drate:.2%} ≥5%")
    if reasons:
        conclusion += "：" + "；".join(reasons)

    run_id = f"qc_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    sample_path = write_sample_md(conn, doc_id, version, chunks, lessons, run_id)
    conn.execute(
        """INSERT INTO qc_reports
           (run_id, doc_version, pipeline_version, ran_at, chunk_count,
            empty_rate, dup_rate, lesson_order_ok, metrics, human_sample, conclusion)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, version, PIPELINE_VERSION, utcnow(), chunk_count,
         round(empty_rate, 4), round(drate, 4), lesson_order_ok,
         json.dumps(metrics, ensure_ascii=False),
         f"待人工抽检（样本见 docs/{sample_path.name}）", conclusion))
    return {"run_id": run_id, "chunk_count": chunk_count, "empty_rate": empty_rate,
            "dup_rate": drate, "lesson_order_ok": lesson_order_ok,
            "conclusion": conclusion, "sample": sample_path.name}


def write_sample_md(conn, doc_id: str, version: str,
                    chunks: list[dict], lessons: list[dict], run_id: str) -> "Path":
    rng = random.Random(SEED)
    sample = rng.sample(chunks, min(SAMPLE_SIZE, len(chunks)))
    sample.sort(key=lambda c: c["chunk_id"])

    kp_map = {l["lesson_no"]: (json.loads(l["kp_declared"]) if l["kp_declared"] else [])
              for l in lessons}
    tag_sample = rng.sample(chunks, min(TAGS_CHECK_SIZE, len(chunks)))
    tag_sample.sort(key=lambda c: c["chunk_id"])

    lines = [
        f"# 人工抽检样本 · {version}（{doc_id}）",
        "",
        f"生成：{utcnow()} · seed={SEED} · 抽 {len(sample)}/{len(chunks)} 块",
        "",
        "## 使用说明",
        "",
        "逐块判断三项（切块合理？/ content_type 正确？/ 有无噪声残留？），",
        "在 ☐ 上打勾或写批注；审完后把结论回填 qc_reports.human_sample：",
        "",
        "```sql",
        f"UPDATE qc_reports SET human_sample = '抽检 20 块：切块合理 x/20，content_type 正确 x/20，"
        f"无噪声残留 x/20；批注：…' ",
        f"WHERE run_id = '{run_id}';",
        "```",
        "",
        "## 一、切块与栏目抽检（20 块）",
        "",
    ]
    for c in sample:
        preview = c["text"][:150].replace("\n", " / ")
        lines += [
            f"### {c['chunk_id']}",
            "",
            f"- chapter_path：{c['chapter_path']}",
            f"- content_type：{c['content_type']}",
            f"- 长度：{c['char_count']} 字",
            f"- 前 150 字：{preview}",
            "- 判断：切块合理 ☐ ｜ content_type 正确 ☐ ｜ 无噪声残留 ☐",
            "",
        ]
    lines += [
        "## 二、标注下发一致性抽查（10 块，自动核对）",
        "",
        "规则：块的 knowledge_tags 应与所属课的 kp_declared 完全一致（课级下发）。",
        "",
        "| chunk_id | 课 | tags 数 | 与 kp_declared 一致 |",
        "|---|---|---|---|",
    ]
    agree = 0
    for c in tag_sample:
        tags = json.loads(c["knowledge_tags"]) if c["knowledge_tags"] else []
        expected = kp_map.get(c["lesson_no"], [])
        ok = tags == expected
        agree += ok
        lines.append(f"| {c['chunk_id']} | 第{c['lesson_no']}课 | {len(tags)} | "
                     f"{'✓' if ok else '✗'} |")
    lines += ["", f"一致率：{agree}/{len(tag_sample)}", ""]
    path = DOCS_DIR / f"qc-sample-{version}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    conn = init_db()
    docs = conn.execute(
        "SELECT doc_id, version FROM documents WHERE format = 'pdf' ORDER BY doc_id"
    ).fetchall()
    for d in docs:
        started = utcnow()
        run_id = f"qc_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            result = qc_doc(conn, d["doc_id"], d["version"])
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'qc', ?, ?, ?, ?, 'success')""",
                (run_id, d["version"], started, utcnow(),
                 json.dumps({k: v for k, v in result.items()}, ensure_ascii=False,
                            default=str)))
            conn.commit()
            print(f"[质检] {d['doc_id']} ({d['version']}): "
                  f"{result['chunk_count']} 块，empty={result['empty_rate']:.2%}，"
                  f"dup={result['dup_rate']:.2%}，课序ok={result['lesson_order_ok']} "
                  f"→ {result['conclusion']}")
        except Exception as e:
            conn.rollback()
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'qc', ?, ?, ?, ?, 'failed')""",
                (run_id, d["version"], started, utcnow(),
                 json.dumps({"error": str(e)}, ensure_ascii=False)))
            conn.commit()
            raise
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
