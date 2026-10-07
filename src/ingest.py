"""M1 归集台账：扫描 data/raw/，登记 documents 表。

- 幂等键为 sha256：已入库的文件跳过，重跑不重复
- 只登记 RAW_REGISTRY 中声明的规格文件（架构文档 §2）；
  data/raw/ 中出现未登记文件时跳过并告警（不擅自归集）
- 每次运行写 pipeline_runs（stage=ingest，stats 记录新增/跳过/未知数）

用法：py -3 -m src.ingest
"""

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pypdf import PdfReader

from .db import ROOT, init_db

sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台 GBK 防乱码

RAW_DIR = ROOT / "data" / "raw"

# 规格文件的元数据映射（架构文档 §2 语料与版本线）。
# 顺序即 doc_id 分配顺序，保持稳定以保证可复现。
RAW_REGISTRY = [
    {
        "filename": "人工智能主题课程讲义v1.0.pdf",
        "doc_id": "doc_001",
        "title": "人工智能主题课程讲义",
        "version": "v1.0",
        "status": "superseded",
        "notes": "原始归档，71 页 14 课；第 5 课物理页序乱序（PDF 页 27–31 排在第 6 课之后），缺第 15 课。raw 层只读不可变，乱序与缺课不修源文件，由质检报告记录。",
    },
    {
        "filename": "第15课：AI与未来.docx",
        "doc_id": "doc_002",
        "title": "第15课：AI与未来",
        "version": "-",  # 源组件无版本号；schema 要求 version NOT NULL，故用 "-"
        "status": "component",
        "notes": "第15课源文件，已并入 v1.1+",
    },
    {
        "filename": "人工智能主题课程讲义v1.1.pdf",
        "doc_id": "doc_003",
        "title": "人工智能主题课程讲义",
        "version": "v1.1",
        "status": "superseded",
        "notes": "结构修订版，73 页 15 课：v1.0 乱序已修正（pypdf 重排页序），第 15 课由 docx 经 typst 排版渲染并入。",
    },
    {
        "filename": "人工智能主题课程讲义v1.2.pdf",
        "doc_id": "doc_004",
        "title": "人工智能主题课程讲义",
        "version": "v1.2",
        "status": "active",
        "notes": "事实修订版（2026-10-07）：外审发现的 28 项问题中 34 处手术修复（PyMuPDF redact + 字体匹配回写），依据 docs/讲义审读报告.md，手术脚本 src/fix_v12.py 可重跑。默认 active 版本。",
    },
]


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def page_count(path: Path, fmt: str) -> int | None:
    """PDF 用 pypdf 读页数；docx 无页概念，留 NULL。"""
    if fmt == "pdf":
        return len(PdfReader(path).pages)
    return None


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> int:
    run_id = f"ingest_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    started_at = utcnow()
    conn = init_db()

    added, skipped, missing, unknown = 0, 0, [], []
    try:
        registered_names = {m["filename"] for m in RAW_REGISTRY}
        for m in RAW_REGISTRY:
            path = RAW_DIR / m["filename"]
            if not path.exists():
                missing.append(m["filename"])
                print(f"[缺失] {m['filename']}", file=sys.stderr)
                continue
            digest = sha256_of(path)
            if conn.execute(
                "SELECT 1 FROM documents WHERE sha256 = ?", (digest,)
            ).fetchone():
                skipped += 1
                print(f"[跳过] {m['filename']}（sha256 已入库）")
                continue
            fmt = path.suffix.lstrip(".").lower()
            conn.execute(
                """INSERT INTO documents
                   (doc_id, title, version, format, path, sha256, pages,
                    status, ingested_at, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    m["doc_id"],
                    m["title"],
                    m["version"],
                    fmt,
                    str(path.relative_to(ROOT)),
                    digest,
                    page_count(path, fmt),
                    m["status"],
                    started_at,
                    m["notes"],
                ),
            )
            added += 1
            print(f"[入库] {m['doc_id']} {m['filename']}")

        # data/raw/ 中未在规格内登记的文件：不擅自归集，告警留痕
        for f in sorted(RAW_DIR.iterdir()):
            if f.is_file() and not f.name.startswith(".") and f.name not in registered_names:
                unknown.append(f.name)
                print(f"[未知] {f.name}（不在架构文档 §2 语料清单内，跳过）", file=sys.stderr)

        stats = {
            "added": added,
            "skipped": skipped,
            "missing": missing,
            "unknown": unknown,
        }
        status = "success" if not missing else "failed"
        conn.execute(
            """INSERT INTO pipeline_runs
               (run_id, stage, doc_version, started_at, finished_at, stats, status)
               VALUES (?, 'ingest', NULL, ?, ?, ?, ?)""",
            (run_id, started_at, utcnow(), json.dumps(stats, ensure_ascii=False), status),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.execute(
            """INSERT INTO pipeline_runs
               (run_id, stage, doc_version, started_at, finished_at, stats, status)
               VALUES (?, 'ingest', NULL, ?, ?, NULL, 'failed')""",
            (run_id, started_at, utcnow()),
        )
        conn.commit()
        raise
    finally:
        conn.close()

    print(f"\n本次运行 {run_id}：新增 {added}，跳过 {skipped}，"
          f"缺失 {len(missing)}，未知 {len(unknown)}")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
