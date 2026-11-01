"""工具箱插件：SQLite 持久化（消息 + 定时任务）"""

import contextlib
import os
import sqlite3
import threading
import time
from datetime import datetime

# 数据库路径：Lunar_X/data/tools/tools.db
# 与 bot.plugin_data_dir('tools') 指向同一处；用具名中间变量而不是
# 嵌三层 dirname，插件目录层级变了也一眼看得出该改哪里
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))  # plugins/tools
_FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(_PLUGIN_DIR))  # Lunar_X
DATA_DIR = os.path.join(_FRAMEWORK_ROOT, "data", "tools")
DB_PATH = os.path.join(DATA_DIR, "tools.db")
_LEGACY_DB = os.path.join(_FRAMEWORK_ROOT, "data", "tools.db")  # 旧位置

_conn = None
_lock = threading.RLock()


def get_conn():
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            # 一次性迁移：把旧的 data/tools.db 搬进 data/tools/
            if not os.path.exists(DB_PATH) and os.path.exists(_LEGACY_DB):
                import shutil

                shutil.move(_LEGACY_DB, DB_PATH)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    scene TEXT NOT NULL,
                    group_id INTEGER,
                    user_id INTEGER NOT NULL,
                    text TEXT
                )""")
            _conn.execute("""
                CREATE TABLE IF NOT EXISTS scheduled (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    due_ts INTEGER NOT NULL,
                    done INTEGER DEFAULT 0
                )""")
            _conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_group_ts ON messages(group_id, ts)"
            )
            _conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scheduled_pending "
                "ON scheduled(done, group_id, due_ts)"
            )
            _conn.commit()
            with contextlib.suppress(OSError):
                os.chmod(DB_PATH, 0o600)
        return _conn


def close_db():
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn = None


def save_message(event):
    """保存一条消息（群聊/私聊）"""
    try:
        text = event.get_text()
        if not text:
            return
        with _lock:
            conn = get_conn()
            conn.execute(
                "INSERT INTO messages (ts, scene, group_id, user_id, text) VALUES (?, ?, ?, ?, ?)",
                (
                    int(time.time()),
                    "group" if getattr(event, "group_id", None) else "private",
                    getattr(event, "group_id", None),
                    event.user_id,
                    text[:500],
                ),
            )
            conn.commit()
    except Exception:
        pass


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def purge_old_messages(retention_days: int) -> int:
    """删除 N 天以前的消息，返回删除条数。

    数据保留策略：群消息是个人数据，不该无限期堆在磁盘上。
    retention_days <= 0 表示永久保留（不删除）。
    """
    if retention_days <= 0:
        return 0
    cutoff = int(time.time()) - retention_days * 86400
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount or 0


def count_stored_messages() -> int:
    """当前库里一共存了多少条消息（用于 WebUI 展示与清理）。"""
    with _lock:
        conn = get_conn()
        row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
    return int(row[0]) if row else 0


def purge_all_messages() -> int:
    """清空全部消息记录，返回删除条数。"""
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM messages")
        conn.commit()
        return cur.rowcount or 0


def search_messages(keyword, group_id=None, limit=20):
    """按关键词搜索消息；必须限定群，避免私聊/跨群泄露。"""
    if not group_id:
        return []
    pattern = f"%{_escape_like(keyword)}%"
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT ts, user_id, text FROM messages "
            "WHERE group_id = ? AND text LIKE ? ESCAPE '\\' "
            "ORDER BY id DESC LIMIT ?",
            (group_id, pattern, limit),
        ).fetchall()
    return [{"ts": r[0], "user_id": r[1], "text": r[2]} for r in rows]


def count_messages(days=7, group_id=None):
    """近 N 天消息统计（按天分组）；必须限定群。"""
    if not group_id:
        return []
    since = int(time.time()) - days * 86400
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(*) FROM messages "
            "WHERE ts >= ? AND group_id = ? GROUP BY d ORDER BY d DESC",
            (since, group_id),
        ).fetchall()
    return rows


def count_pending_scheduled(group_id):
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT COUNT(*) FROM scheduled WHERE done = 0 AND group_id = ?",
            (group_id,),
        ).fetchone()
    return int(row[0]) if row else 0


# ---------- 定时任务持久化 ----------


def add_scheduled(group_id, user_id, content, due_ts):
    with _lock:
        conn = get_conn()
        cur = conn.execute(
            "INSERT INTO scheduled (group_id, user_id, content, due_ts) VALUES (?, ?, ?, ?)",
            (group_id, user_id, content, int(due_ts)),
        )
        conn.commit()
        return cur.lastrowid


def load_pending_scheduled():
    """加载所有未到期的定时任务"""
    now = int(time.time())
    with _lock:
        conn = get_conn()
        rows = conn.execute(
            "SELECT id, group_id, user_id, content, due_ts FROM scheduled "
            "WHERE done = 0 AND due_ts > ?",
            (now,),
        ).fetchall()
    return [
        {"id": r[0], "group_id": r[1], "user_id": r[2], "content": r[3], "due_ts": r[4]}
        for r in rows
    ]


def list_scheduled(group_id=None):
    sql = "SELECT id, group_id, user_id, content, due_ts FROM scheduled WHERE done = 0"
    params = []
    if group_id:
        sql += " AND group_id = ?"
        params.append(group_id)
    sql += " ORDER BY due_ts ASC LIMIT 20"
    with _lock:
        conn = get_conn()
        rows = conn.execute(sql, params).fetchall()
    return [
        {"id": r[0], "group_id": r[1], "user_id": r[2], "content": r[3], "due_ts": r[4]}
        for r in rows
    ]


def cancel_scheduled(task_id, group_id=None):
    with _lock:
        conn = get_conn()
        if group_id:
            conn.execute(
                "UPDATE scheduled SET done = 1 WHERE id = ? AND group_id = ?",
                (task_id, group_id),
            )
        else:
            conn.execute("UPDATE scheduled SET done = 1 WHERE id = ?", (task_id,))
        conn.commit()


def mark_scheduled_done(task_id):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE scheduled SET done = 1 WHERE id = ?", (task_id,))
        conn.commit()


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
