"""M8 Web 展示层：给讲义知识库 RAG 一个浏览器问答界面。

职责
----
- GET  /         返回 web/index.html（单文件前端，内联 CSS/JS，离线可用）；
                 设置 DEMO_TOKEN 环境变量时改为密码闸门页（未带有效 cookie）
- POST /api/login  （DEMO_TOKEN 启用时）校验密码，成功则 Set-Cookie（HttpOnly）
- POST /api/ask  复用 src/qa.ask() 的完整问答链路（检索 → 双信号拒答 → LLM → 出处），
                 返回 {answer, abstained, citations, latency_ms, retrieval}
- GET  /healthz  健康检查（Render 用），恒 200 "ok"，不过闸门

部署相关
--------
- PORT 环境变量指定端口（Render 注入），默认 8000；HOST 恒 0.0.0.0
- DEMO_TOKEN：设置即启用访问控制（cookie 值为 token 的 sha256，不落明文）；
  未设置则完全不加闸（本地开发体验不变）
- /api/ask 按客户端 IP 内存滑动窗口限流（默认 10 次/分钟，超限 429），
  无论 DEMO_TOKEN 是否设置都启用；本地压测可用 RATE_LIMIT_PER_MIN 调大

启动
----
  py -3 -m src.serve
  # 浏览器打开 http://127.0.0.1:8000
"""

import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import qa
from .db import ROOT, DB_PATH
from .retrieve import Retriever

sys.stdout.reconfigure(encoding="utf-8")

HOST = "0.0.0.0"  # 监听所有网卡，局域网设备可通过本机 IP 访问
PORT = int(os.environ.get("PORT", 8000))  # Render 通过 $PORT 指定
INDEX_FILE = ROOT / "web" / "index.html"

DEMO_TOKEN = os.environ.get("DEMO_TOKEN") or None
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", 10))
COOKIE_NAME = "demo_auth"

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>访问验证 · 讲义知识库</title>
<style>
  :root{--ikb:#002FA7;--lemon:#FFDD00;--ink:#101010;--dot:#e4e4e4;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:#fff radial-gradient(var(--dot) 1px, transparent 1.3px);
       background-size:24px 24px;color:var(--ink);
       font-family:"Helvetica Neue",Helvetica,Arial,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       min-height:100vh;display:flex;align-items:center;justify-content:center;}
  body::before{content:"";position:fixed;top:0;left:0;right:0;height:6px;background:var(--ikb);}
  .card{width:min(420px,90vw);border:2px solid var(--ink);background:#fff;padding:40px 36px;}
  .card h1{font-size:20px;letter-spacing:.05em;margin-bottom:6px;}
  .card h1::before{content:"";display:block;width:44px;height:6px;background:var(--ikb);margin-bottom:14px;}
  .card p.sub{font-size:13px;color:#555;margin-bottom:26px;}
  input[type=password]{width:100%;border:2px solid var(--ink);padding:12px 14px;font-size:15px;
       outline:none;background:#fff;}
  input[type=password]:focus{box-shadow:4px 4px 0 var(--lemon);}
  button{width:100%;margin-top:16px;border:2px solid var(--ink);background:var(--ikb);color:#fff;
       font-size:15px;letter-spacing:.2em;padding:12px;cursor:pointer;}
  button:hover{background:#001f70;}
  .err{display:none;margin-top:14px;border:2px solid var(--ink);background:var(--lemon);
       padding:10px 14px;font-size:13px;}
</style>
</head>
<body>
<div class="card">
  <h1>讲义知识库 · RAG 问答</h1>
  <p class="sub">本演示站点已启用访问控制，请输入访问密码。</p>
  <form id="f">
    <input type="password" id="pw" placeholder="访问密码" autocomplete="current-password" autofocus>
    <button type="submit">进 入</button>
  </form>
  <div class="err" id="err">密码不正确，请重试。</div>
</div>
<script>
document.getElementById('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const r = await fetch('/api/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: document.getElementById('pw').value})
  });
  if (r.ok) { location.reload(); }
  else { document.getElementById('err').style.display = 'block';
         document.getElementById('pw').value = ''; }
});
</script>
</body>
</html>"""


def _cookie_value(token: str) -> str:
    """cookie 值 = token 的 sha256（不落明文，恒定时间比较在服务端做）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


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


class RateLimiter:
    """按客户端 IP 的内存滑动窗口限流（单进程演示部署足够）。"""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(ip, []) if now - t < 60.0]
            if len(hits) >= self.per_minute:
                self._hits[ip] = hits
                return False
            hits.append(now)
            self._hits[ip] = hits
            return True


class Handler(BaseHTTPRequestHandler):
    app: QAApp | None = None
    limiter: RateLimiter | None = None

    # ---- 访问控制 ----
    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _authed(self) -> bool:
        if DEMO_TOKEN is None:
            return True
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE_NAME:
                import hmac
                return hmac.compare_digest(v, _cookie_value(DEMO_TOKEN))
        return False

    # ---- 路由 ----
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(b"ok", "text/plain; charset=utf-8")
        elif path in ("/", "/index.html"):
            if self._authed():
                self._send_html()
            else:
                self._send(LOGIN_PAGE.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/login":
            self._handle_login()
            return
        if path != "/api/ask":
            self._send_json({"error": "not found"}, status=404)
            return
        if not self._authed():
            self._send_json({"error": "未授权：请先通过密码验证"}, status=401)
            return
        if not self.limiter.allow(self._client_ip()):
            self._send_json({"error": "请求太频繁，请稍后再试（每分钟限 "
                                      f"{RATE_LIMIT_PER_MIN} 次）"}, status=429)
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

    def _handle_login(self):
        if DEMO_TOKEN is None:
            self._send_json({"error": "未启用访问控制"}, status=404)
            return
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json({"error": "请求体不是合法 JSON"}, status=400)
            return
        import hmac
        if hmac.compare_digest(str(payload.get("password") or ""), DEMO_TOKEN):
            cookie = (f"{COOKIE_NAME}={_cookie_value(DEMO_TOKEN)}; "
                      "Path=/; HttpOnly; SameSite=Lax")
            self._send_json({"ok": True}, headers={"Set-Cookie": cookie})
        else:
            self._send_json({"error": "密码不正确"}, status=401)

    # ---- 响应 ----
    def _send(self, data: bytes, ctype: str, status: int = 200, headers: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self):
        try:
            data = INDEX_FILE.read_bytes()
        except FileNotFoundError:
            self._send("web/index.html 不存在".encode("utf-8"), "text/plain; charset=utf-8", 500)
            return
        self._send(data, "text/html; charset=utf-8")

    def _send_json(self, obj, status: int = 200, headers: dict | None = None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send(data, "application/json; charset=utf-8", status, headers)

    def log_message(self, fmt, *args):  # 精简访问日志，便于现场演示
        sys.stderr.write(f"[http] {self.command} {self.path} -> {fmt % args}\n")


def main() -> int:
    try:
        app = QAApp()
    except Exception as e:
        print(f"[serve] 启动失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    Handler.app = app
    Handler.limiter = RateLimiter(RATE_LIMIT_PER_MIN)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    gate = "启用密码闸门" if DEMO_TOKEN else "无闸门（本地模式）"
    print(f"[serve] 讲义知识库 · RAG 问答  http://{HOST}:{PORT}  "
          f"（{gate}；限流 {RATE_LIMIT_PER_MIN} 次/分/IP）(Ctrl+C 停止)")
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
