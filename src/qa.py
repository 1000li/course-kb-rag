"""M6 运行时问答：检索 → 双信号与门拒答判定 → LLM 组织回答（命中才调）。

- 检索复用 src/retrieve.py：默认 active 口径；--doc-version 放开版本过滤（版本对比场景，§4.2）
- 拒答规则读最新 eval_runs.threshold（eval 集标定的双信号与门，§4.4 v0.3）；
  拒答时直接返回话术，不调用 LLM（§4.4 / §8：拒答不调用）
- LLM：DeepSeek（openai SDK 兼容模式，base_url=https://api.deepseek.com），
  key 从环境变量或 .env 的 DEEPSEEK_API_KEY 读；缺失时明确报错，不硬编码
- 出处拼装用 retrieve.cite()（§4.5/§4.7 物理页码）
- 调用日志：data/logs/qa_calls.jsonl（§8）

用法：
  py -3 -m src.qa "机器学习有哪几种类型？"
  py -3 -m src.qa "AI 的未来趋势有哪些？" --doc-version v1.0
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import ROOT, init_db
from .retrieve import Retriever, cite

sys.stdout.reconfigure(encoding="utf-8")

LLM_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-chat"
LOG_FILE = ROOT / "data" / "logs" / "qa_calls.jsonl"
ABSTAIN_REPLY = "未在讲义中找到依据。该问题超出本讲义范围，恕不作答。"

SYSTEM_PROMPT = (
    "你是一位课程讲义的问答助手。你只能依据下面给出的讲义检索块回答问题，"
    "块里没有的信息不得补充、不得编造。回答用简洁的中文，"
    "结尾不需要重复出处（出处由系统单独展示）。"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_api_key() -> str | None:
    """环境变量优先，其次 .env；找不到返回 None。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DEEPSEEK_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'") or None
    return None


def load_abstain_rule(conn) -> dict:
    """读最新 eval_runs 标定的双信号与门规则。"""
    row = conn.execute(
        "SELECT threshold FROM eval_runs ORDER BY ran_at DESC LIMIT 1").fetchone()
    if row is None or not row["threshold"]:
        raise RuntimeError("未找到标定的拒答规则：请先运行 py -3 -m src.eval_retrieval")
    return json.loads(row["threshold"])


def should_abstain(top_chunks: list[dict], rule: dict,
                   lesson_filter: int | None = None) -> bool:
    # 课号 scope（查询理解命中"第N课"）：双信号阈值是按全库检索标定的，与课内
    # 小语料的绝对分值不可比（BM25 语料收缩 + 1-L2 分值压缩），不走与门——
    # 仅当 scope 为空（课号不存在）时拒答；有块则交给 LLM 依据块作答
    # （系统提示词已强制只依据块，课内没有的内容 LLM 会说没有）
    if lesson_filter is not None:
        return not top_chunks
    vs = [c["vec_score"] for c in top_chunks if c["vec_score"] is not None]
    bs = [c["bm25_score"] for c in top_chunks if c["bm25_score"] is not None]
    max_vec = max(vs) if vs else float("-inf")
    max_bm = max(bs) if bs else float("-inf")
    return max_vec < rule["vec_max_top5"] and max_bm < rule["bm25_max_top5"]


def log_call(record: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def ask(conn, retriever: Retriever, question: str,
        doc_version: str | None = None) -> dict:
    scope_doc_id = None
    if doc_version:
        row = conn.execute(
            "SELECT doc_id FROM documents WHERE version = ?", (doc_version,)
        ).fetchone()
        if row is None:
            raise ValueError(f"未知文档版本：{doc_version}")
        scope_doc_id = row["doc_id"]

    rule = load_abstain_rule(conn)
    t0 = time.monotonic()
    res = retriever.search(question, scope_doc_id=scope_doc_id)
    top5 = res["top_chunks"]
    abstained = should_abstain(top5, rule, lesson_filter=res["lesson_filter"])

    record = {
        "ts": utcnow(), "query": question, "scope_doc_id": res["scope_doc_id"],
        "doc_version": doc_version, "chunk_ids": [c["chunk_id"] for c in top5],
        "abstained": abstained, "model": None,
        "usage": None, "latency_ms": None, "error": None,
    }

    if abstained:
        record["latency_ms"] = round((time.monotonic() - t0) * 1000)
        log_call(record)
        return {"answer": ABSTAIN_REPLY, "abstained": True,
                "citations": [], "scope_doc_id": res["scope_doc_id"]}

    api_key = load_api_key()
    if not api_key:
        record["error"] = "missing_api_key"
        record["latency_ms"] = round((time.monotonic() - t0) * 1000)
        log_call(record)
        raise RuntimeError(
            "检索已命中，但找不到 DEEPSEEK_API_KEY：请在项目根目录 .env 中写入 "
            "DEEPSEEK_API_KEY=sk-... 后重试（.env 已在 .gitignore 中）")

    context = "\n\n".join(
        f"【检索块 {i}｜{cite(c)}】\n{c['text']}" for i, c in enumerate(top5, 1))
    from openai import OpenAI
    client = OpenAI(api_key=api_key, base_url=LLM_BASE_URL)
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"讲义检索块：\n{context}\n\n问题：{question}"},
            ],
            temperature=0.2,
        )
        answer = resp.choices[0].message.content
        record["model"] = LLM_MODEL
        record["usage"] = (resp.usage.model_dump() if resp.usage else None)
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}"
        record["latency_ms"] = round((time.monotonic() - t0) * 1000)
        log_call(record)
        raise
    record["latency_ms"] = round((time.monotonic() - t0) * 1000)
    log_call(record)
    return {"answer": answer, "abstained": False,
            "citations": [cite(c) for c in top5[:3]],
            "scope_doc_id": res["scope_doc_id"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="课程讲义溯源问答")
    ap.add_argument("question")
    ap.add_argument("--doc-version", default=None,
                    help="放开版本过滤（如 v1.0），默认仅 active 版本")
    args = ap.parse_args()

    conn = init_db()
    retriever = Retriever(conn)
    try:
        result = ask(conn, retriever, args.question, doc_version=args.doc_version)
    finally:
        conn.close()

    print(f"\nQ：{args.question}（scope={result['scope_doc_id']}）")
    print(f"\nA：{result['answer']}")
    if result["abstained"]:
        print("\n[已拒答：双信号与门判定低于阈值，未调用 LLM]")
    else:
        print("\n出处：")
        for c in result["citations"]:
            print(f"  · {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
