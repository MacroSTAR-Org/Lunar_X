"""长期记忆 SQLite 存储（user_facts）。

隐私分层的一半：这张库**只在 memory.enabled 显式开启后才会被写入**，
默认配置下它根本不会被创建。模式完全照抄 plugins/tools/store.py
（WAL / busy_timeout / RLock / 0600 / 关闭钩子），保持项目内一致。

存什么：从对话里异步抽取的"用户事实"——一句独立、第三人称的短句。
不存什么：原始聊天记录（那是 tools 插件消息库的事，且默认关闭）。
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time

# 数据库路径：Lunar_X/data/ai_chat/memory.db
# 与 bot.plugin_data_dir("ai_chat") 指向同一处；测试时可整体替换模块常量
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))  # plugins/ai_chat
_FRAMEWORK_ROOT = os.path.dirname(os.path.dirname(_PLUGIN_DIR))  # Lunar_X
DATA_DIR = os.path.join(_FRAMEWORK_ROOT, "data", "ai_chat")
DB_PATH = os.path.join(DATA_DIR, "memory.db")

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()

# 检索参数
_RECALL_CANDIDATES = 50
_RECALL_TOP = 8
_RECALL_MAX_CHARS = 400

# 惰性日清：不建定时器，每天第一次对话顺手清一次过期记忆
_last_purge_day: str = ""


def get_conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            os.makedirs(DATA_DIR, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=5000")
            _conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    group_id INTEGER,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    hits INTEGER DEFAULT 0
                )
                """
            )
            _conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_user ON user_facts(user_id, last_seen)"
            )
            _conn.commit()
            with contextlib.suppress(OSError):
                os.chmod(DB_PATH, 0o600)
        return _conn


def close_db() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn = None


def add_fact(user_id: int, kind: str, content: str, group_id: int | None = None) -> str:
    """写入一条事实；同一用户内容重复时视为"再次提及"，刷新近因计数。

    返回 "new" 或 "refreshed"。
    """
    content = content.strip()[:120]
    now = int(time.time())
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT id FROM user_facts WHERE user_id = ? AND content = ?",
            (user_id, content),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE user_facts SET last_seen = ?, hits = hits + 1, kind = ? WHERE id = ?",
                (now, kind, row[0]),
            )
            conn.commit()
            return "refreshed"
        conn.execute(
            "INSERT INTO user_facts (user_id, group_id, kind, content, ts, last_seen) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, group_id, kind, content, now, now),
        )
        conn.commit()
        return "new"


def _keywords(question: str) -> list[str]:
    """粗糙的中文关键词切分：按标点断句 + CJK 二字滑窗。

    不引分词库。中文没有空格，整句没法当关键词去子串匹配，
    所以对超过 2 字的 CJK 片段额外产出全部二字窗口——
    "研究生生活如何"能切出"研究生"去命中事实里的"在读研究生"。
    代价是常用字组（"怎么"）可能凑巧命中，但候选本来就少（每用户≤100），
    噪音可接受。
    """
    import re

    tokens = re.split(r"[\s，。,.!！?？:：;；、\n\r]+", question or "")
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        token = token.strip().lower()
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(token) > 2 and re.search(r"[\u4e00-\u9fff]", token):
            out.extend(
                token[i : i + 2] for i in range(len(token) - 1)
                if token[i : i + 2] not in seen and not seen.add(token[i : i + 2])
            )
    return out


def recall_facts(user_id: int, question: str) -> list[str]:
    """检索与当前问题相关的记忆：关键词命中 ×2 + 近因加成，取 top。

    没有向量库——检索质量用"候选少 + 打分简单"换实现简单，
    事实条目本身就不多（每用户上限 100），够用。
    """
    tokens = _keywords(question)
    with _lock:
        rows = get_conn().execute(
            "SELECT content, last_seen FROM user_facts WHERE user_id = ? "
            "ORDER BY last_seen DESC LIMIT ?",
            (user_id, _RECALL_CANDIDATES),
        ).fetchall()

    scored: list[tuple[float, str]] = []
    for index, (content, _last_seen) in enumerate(rows):
        score = 0.0
        lowered = content.lower()
        for token in tokens:
            if token in lowered:
                score += 2.0
        score += max(0.0, 1.0 - index * 0.05)  # 近因加成：越新分越高
        if score > 1.0:  # 纯近因（无关键词命中）不入选
            scored.append((score, content))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    out: list[str] = []
    used = 0
    for _, content in scored:
        if used + len(content) > _RECALL_MAX_CHARS:
            break
        out.append(content)
        used += len(content)
        if len(out) >= _RECALL_TOP:
            break
    return out


def enforce_max_per_user(user_id: int, max_per_user: int) -> int:
    """单用户记忆条数上限，超限删最旧（按 last_seen）。"""
    with _lock:
        conn = get_conn()
        ids = conn.execute(
            "SELECT id FROM user_facts WHERE user_id = ? ORDER BY last_seen DESC",
            (user_id,),
        ).fetchall()
        excess = ids[max(1, max_per_user) :]
        for (fact_id,) in excess:
            conn.execute("DELETE FROM user_facts WHERE id = ?", (fact_id,))
        if excess:
            conn.commit()
        return len(excess)


def purge_old_facts(retention_days: int) -> int:
    """删除 N 天没被提及的记忆；retention_days <= 0 表示永久保留。"""
    if retention_days <= 0:
        return 0
    cutoff = int(time.time()) - retention_days * 86400
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM user_facts WHERE last_seen < ?", (cutoff,))
        conn.commit()
        return cursor.rowcount or 0


def lazy_daily_purge(retention_days: int) -> int:
    """惰性日清：每天首次调用时执行一次，其余调用直接跳过。"""
    global _last_purge_day
    today = time.strftime("%Y-%m-%d")
    if _last_purge_day == today:
        return 0
    _last_purge_day = today
    try:
        return purge_old_facts(retention_days)
    except Exception:
        return 0


def forget_user(user_id: int) -> int:
    """删除某用户的全部记忆（$ai 忘了我）。"""
    with _lock:
        conn = get_conn()
        cursor = conn.execute("DELETE FROM user_facts WHERE user_id = ?", (user_id,))
        conn.commit()
        return cursor.rowcount or 0


def count_facts() -> int:
    with _lock:
        row = get_conn().execute("SELECT COUNT(*) FROM user_facts").fetchone()
    return int(row[0]) if row else 0
