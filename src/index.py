"""M5 索引层：chunks → chunk_vectors（sqlite-vec）+ index_runs。

- Embedding：fastembed / BAAI-bge-small-zh-v1.5，dim=512，L2 归一化
- 覆盖全部文档版本的块（含 superseded），检索时按 scope 过滤；
  排除 content_type='课标题' 的块（课标题互相天然相似，无检索价值）
- 幂等：每次全量重建（DROP + CREATE 虚拟表 + 重插）
- index_runs 记录 model_name/dim/normalize/chunk_count/built_at；
  check_index() 供检索/问答启动时校验模型配置与索引一致

用法：py -3 -m src.index
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from .db import init_db, load_vec

sys.stdout.reconfigure(encoding="utf-8")

MODEL_NAME = "BAAI/bge-small-zh-v1.5"
DIM = 512
NORMALIZE = 1  # L2 归一化，余弦等价点积


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_embedder():
    from fastembed import TextEmbedding
    return TextEmbedding(model_name=MODEL_NAME)


def normalize_text(text: str) -> str:
    """拉丁字母小写归一化：bge-small-zh-v1.5 的 tokenizer 会把全大写缩写
    (AI/AIGC/CNN/GPT...) 整体映射为 [UNK]，大小写语义完全丢失；
    小写化后分词正常。作用于索引与查询两侧（均经 embed_texts/tokenize），
    CJK 字符不受 lower() 影响。"""
    return text.lower()


def embed_texts(model, texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in model.embed([normalize_text(t) for t in texts])]


def build_index(conn) -> dict:
    """全量重建 chunk_vectors；返回 stats。"""
    chunks = conn.execute(
        """SELECT chunk_id, text FROM chunks
           WHERE content_type != '课标题' ORDER BY chunk_id""").fetchall()
    model = get_embedder()
    texts = [c["text"] for c in chunks]
    vectors = embed_texts(model, texts)

    load_vec(conn)
    conn.execute("DROP TABLE IF EXISTS chunk_vectors")
    conn.execute(
        f"CREATE VIRTUAL TABLE chunk_vectors USING vec0("
        f"chunk_id TEXT PRIMARY KEY, embedding FLOAT[{DIM}])")
    conn.executemany(
        "INSERT INTO chunk_vectors (chunk_id, embedding) VALUES (?, ?)",
        [(c["chunk_id"], json.dumps(v)) for c, v in zip(chunks, vectors)])

    run_id = f"index_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    conn.execute(
        """INSERT INTO index_runs (run_id, model_name, dim, normalize, chunk_count, built_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (run_id, MODEL_NAME, DIM, NORMALIZE, len(chunks), utcnow()))
    return {"run_id": run_id, "chunk_count": len(chunks)}


def check_index(conn, model_name: str = MODEL_NAME, dim: int = DIM) -> dict:
    """校验当前模型配置与最新 index_runs 一致，不一致报错要求重建。"""
    row = conn.execute(
        "SELECT * FROM index_runs ORDER BY built_at DESC LIMIT 1").fetchone()
    if row is None:
        raise RuntimeError("索引不存在：请先运行 py -3 -m src.index 重建索引")
    if row["model_name"] != model_name or row["dim"] != dim:
        raise RuntimeError(
            f"索引与模型配置不一致（索引: {row['model_name']}/{row['dim']}，"
            f"配置: {model_name}/{dim}）：请运行 py -3 -m src.index 重建索引")
    return dict(row)


def main() -> int:
    conn = init_db()
    started = utcnow()
    run_id = f"index_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    try:
        stats = build_index(conn)
        pv = conn.execute(
            "SELECT DISTINCT pipeline_version FROM chunks").fetchall()
        conn.execute(
            """INSERT INTO pipeline_runs
               (run_id, stage, doc_version, started_at, finished_at, stats, status)
               VALUES (?, 'index', NULL, ?, ?, ?, 'success')""",
            (run_id, started, utcnow(), json.dumps(
                {**stats, "model": MODEL_NAME, "dim": DIM,
                 "pipeline_version": [r[0] for r in pv]}, ensure_ascii=False)))
        conn.commit()
        print(f"[索引] {stats['chunk_count']} 块向量化，model={MODEL_NAME} dim={DIM} "
              f"→ index_runs {stats['run_id']}")
    except Exception as e:
        conn.rollback()
        conn.execute(
            """INSERT INTO pipeline_runs
               (run_id, stage, doc_version, started_at, finished_at, stats, status)
               VALUES (?, 'index', NULL, ?, ?, ?, 'failed')""",
            (run_id, started, utcnow(),
             json.dumps({"error": str(e)}, ensure_ascii=False)))
        conn.commit()
        raise
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
