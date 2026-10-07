"""M3 导出：chunks 表 → data/export/dataset_{version}.jsonl（数据集交付格式）。

每行一个块：chunk_id / lesson_no / chapter_path / content_type / text /
knowledge_tags / page_start / page_end / doc_version。
幂等：覆盖写同名文件；每 doc 一条 pipeline_runs（stage=export）。

用法：py -3 -m src.export
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from .db import ROOT, init_db

sys.stdout.reconfigure(encoding="utf-8")

EXPORT_DIR = ROOT / "data" / "export"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    conn = init_db()
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    docs = conn.execute(
        "SELECT doc_id, version FROM documents WHERE format = 'pdf' ORDER BY doc_id"
    ).fetchall()
    for d in docs:
        doc_id, version = d["doc_id"], d["version"]
        started = utcnow()
        run_id = f"export_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            rows = conn.execute(
                """SELECT chunk_id, lesson_no, chapter_path, content_type, text,
                          knowledge_tags, page_start, page_end
                   FROM chunks WHERE doc_id = ?
                   ORDER BY lesson_no, chunk_id""", (doc_id,)).fetchall()
            out = EXPORT_DIR / f"dataset_{version}.jsonl"
            with open(out, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps({
                        "chunk_id": r["chunk_id"],
                        "lesson_no": r["lesson_no"],
                        "chapter_path": r["chapter_path"],
                        "content_type": r["content_type"],
                        "text": r["text"],
                        "knowledge_tags": json.loads(r["knowledge_tags"])
                                          if r["knowledge_tags"] else [],
                        "page_start": r["page_start"],
                        "page_end": r["page_end"],
                        "doc_version": version,
                    }, ensure_ascii=False) + "\n")
            stats = {"chunks": len(rows), "file": str(out.relative_to(ROOT))}
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'export', ?, ?, ?, ?, 'success')""",
                (run_id, version, started, utcnow(),
                 json.dumps(stats, ensure_ascii=False)))
            conn.commit()
            print(f"[导出] {out.relative_to(ROOT)}: {len(rows)} 行")
        except Exception as e:
            conn.rollback()
            conn.execute(
                """INSERT INTO pipeline_runs
                   (run_id, stage, doc_version, started_at, finished_at, stats, status)
                   VALUES (?, 'export', ?, ?, ?, ?, 'failed')""",
                (run_id, version, started, utcnow(),
                 json.dumps({"error": str(e)}, ensure_ascii=False)))
            conn.commit()
            raise
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
