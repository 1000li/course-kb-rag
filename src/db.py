"""SQLite 连接助手：单一事实库 db/course_kb.db。

- get_conn()：返回启用 foreign_keys 的连接
- load_vec()：加载 sqlite-vec 扩展（向量索引阶段使用，M1 备用）
- init_db()：执行 src/schema.sql 建表（幂等，IF NOT EXISTS）
"""

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "course_kb.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def load_vec(conn: sqlite3.Connection) -> None:
    """加载 sqlite-vec 扩展（chunk_vectors 虚拟表由 index 阶段创建）。"""
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = get_conn(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return conn
