"""M8 Web 展示层：给讲义知识库 RAG 一个浏览器问答界面。

职责
----
- GET  /         返回 web/index.html（单文件前端，内联 CSS/JS，离线可用）
- POST /api/ask  复用 src/qa.ask() 的完整问答链路（检索 → 双信号拒答 → LLM → 出处），
                 返回 {answer, abstained, citations, latency_ms, retrieval}

复用说明（不重写逻辑）
---------------------
- 问答链路：直接调用 src.qa.ask()（双信号拒答判定、DeepSeek 调用、qa_calls 日志都在里面）
- 检索信号 max_vec/max_bm25 与结构化出处：复用 src.retrieve.Retriever.search()；
  serve 侧一次检索用于展示信号，qa.ask 内部再检索一次用于决策，两者确定性一致
- 出处字段口径与 src.retrieve.cite() 一致（chapter_path 的 "课 > 节" 拆分）
- 依赖仅标准库 http.server，无新增第三方依赖（不改 pyproject.toml）

启动
----
  py -3 -m src.serve
  # 浏览器打开 http://127.0.0.1:8000

接口示例
--------
  curl http://127.0.0.1:8000/
  curl -X POST http://127.0.0.1:8000/api/ask \
       -H "Content-Type: application/json" \
       -d '{"question":"机器学习有哪几种类型？","doc_version":"v1.2"}'
"""

import json
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import qa
from .db import ROOT, DB_PATH
from .retrieve import Retriever

HOST = "127.0.0.1"
PORT = 8000
INDEX_FILE = ROOT / "web" / "index.html"


def _open_conn() -> sqlite3.Connection:
    """打开连接（check_same_thread=False 供多线程 worker 使用，访问由锁串行化）。"""
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _structured_cite(c: dict) -> dict:
    """结构化出处（节标题口径与 retrieve.cite() 一致）。"""
    section = ""
    if " > " in c["chapter_path"]:
        section = c["chapter_path"].split(" > ", 1)[1].strip()
    return {
        "lesson_no": c["lesson_no"],
        "section_title": section,
        "page_range": f"{c['page_start']}-{c['page_end']}",
        "chunk_id": c["chunk_id"],
    }


class QAApp:
    """共享连接 + 检索器；所有访问经 self._lock 串行化（sqlite 连接线程安全由锁保证）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.conn = _open_conn()
        self.retriever = Retriever(self.conn)  # 校验索引、加载向量扩展与 embedding 模型

    def ask(self, question: str, doc_version: str | None) -> dict:
        with self._lock:
            doc_id = self._resolve_doc_id(doc_version)
            top = self.retriever.search(question, scope_doc_id=doc_id)["top_chunks"]
            max_vec = max(
                (c["vec_score"] for c in top if c["vec_score"] is not None), default=0.0)
            max_bm25 = max(
                (c["bm25_score"] for c in top if c["bm25_score"] is not None), default=0.0)

            t0 = time.monotonic()
            result = qa.ask(self.conn, self.retriever, question, doc_version=doc_version)
            latency_ms = round((time.monotonic() - t0) * 1000)

            citations = [] if result["abstained"] else [
                _structured_cite(c) for c in top[:3]]
            return {
                "answer": result["answer"],
                "abstained": result["abstained"],
                "citations": citations,
                "latency_ms": latency_ms,
                "retrieval": {
                    "max_vec": round(max_vec, 4),
                    "max_bm25": round(max_bm25, 2),
                },
            }

    def _resolve_doc_id(self, doc_version: str | None) -> str | None:
        if doc_version is None:
            return None
        row = self.conn.execute(
            "SELECT doc_id FROM documents WHERE version = ?", (doc_version,)).fetchone()
        if row is None:
            raise ValueError(f"未知文档版本：{doc_version}")
        return row["doc_id"]


class Handler(BaseHTTPRequestHandler):
    app: QAApp | None = None

    # ---- 路由 ----
    def do_GET(self):
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self._send_html()
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/ask":
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "请求体不是合法 JSON"}, status=400)
            return
        question = (payload.get("question") or "").strip()
        if not question:
            self._send_json({"error": "question 不能为空"}, status=400)
            return
        doc_version = payload.get("doc_version") or None
        try:
            body = self.app.ask(question, doc_version)
        except ValueError as e:
            self._send_json({"error": str(e)}, status=400)
        except Exception as e:  # LLM / 索引 / 拒答阈值缺失等
            self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
        else:
            self._send_json(body)

    # ---- 响应 ----
    def _send(self, data: bytes, ctype: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self):
        try:
            data = INDEX_FILE.read_bytes()
        except FileNotFoundError:
            self._send("web/index.html 不存在".encode("utf-8"), "text/plain; charset=utf-8", 500)
            return
        self._send(data, "text/html; charset=utf-8")

    def _send_json(self, obj, status: int = 200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(data, "application/json; charset=utf-8", status)

    def log_message(self, fmt, *args):  # 精简访问日志，便于现场演示
        sys.stderr.write(f"[http] {self.command} {self.path} -> {fmt % args}\n")


def main() -> int:
    try:
        app = QAApp()
    except Exception as e:
        print(f"[serve] 启动失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    Handler.app = app
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[serve] 讲义知识库 · RAG 问答  http://{HOST}:{PORT}  (Ctrl+C 停止)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] 已停止")
    finally:
        server.server_close()
        app.conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
