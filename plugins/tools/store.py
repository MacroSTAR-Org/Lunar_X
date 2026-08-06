# -*- coding: utf-8 -*-
"""工具箱插件：SQLite 持久化（消息 + 定时任务）"""
import os
import sqlite3
import time
from datetime import datetime

# 数据库路径：Lunar_X/data/tools.db
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data')
DB_PATH = os.path.join(DATA_DIR, 'tools.db')

_conn = None


def get_conn():
    global _conn
    if _conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        _conn = sqlite3.connect(DB_PATH)
        _conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                scene TEXT NOT NULL,
                group_id INTEGER,
                user_id INTEGER NOT NULL,
                text TEXT
            )''')
        _conn.execute('''
            CREATE TABLE IF NOT EXISTS scheduled (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                due_ts INTEGER NOT NULL,
                done INTEGER DEFAULT 0
            )''')
        _conn.commit()
    return _conn


def save_message(event):
    """保存一条消息（群聊/私聊）"""
    try:
        text = event.get_text()
        if not text:
            return
        conn = get_conn()
        conn.execute(
            'INSERT INTO messages (ts, scene, group_id, user_id, text) VALUES (?, ?, ?, ?, ?)',
            (int(time.time()),
             'group' if getattr(event, 'group_id', None) else 'private',
             getattr(event, 'group_id', None),
             event.user_id,
             text[:500]))
        conn.commit()
    except Exception:
        pass


def search_messages(keyword, group_id=None, limit=20):
    """按关键词搜索消息（可限定群）"""
    conn = get_conn()
    sql = 'SELECT ts, user_id, text FROM messages WHERE text LIKE ?'
    params = [f'%{keyword}%']
    if group_id:
        sql += ' AND group_id = ?'
        params.append(group_id)
    sql += ' ORDER BY id DESC LIMIT ?'
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [{'ts': r[0], 'user_id': r[1], 'text': r[2]} for r in rows]


def count_messages(days=7, group_id=None):
    """近 N 天消息统计（按天分组）"""
    conn = get_conn()
    since = int(time.time()) - days * 86400
    sql = "SELECT date(ts, 'unixepoch', 'localtime') AS d, COUNT(*) FROM messages WHERE ts >= ?"
    params = [since]
    if group_id:
        sql += ' AND group_id = ?'
        params.append(group_id)
    sql += ' GROUP BY d ORDER BY d DESC'
    return conn.execute(sql, params).fetchall()


# ---------- 定时任务持久化 ----------

def add_scheduled(group_id, user_id, content, due_ts):
    conn = get_conn()
    cur = conn.execute(
        'INSERT INTO scheduled (group_id, user_id, content, due_ts) VALUES (?, ?, ?, ?)',
        (group_id, user_id, content, int(due_ts)))
    conn.commit()
    return cur.lastrowid


def load_pending_scheduled():
    """加载所有未到期的定时任务"""
    conn = get_conn()
    now = int(time.time())
    rows = conn.execute(
        'SELECT id, group_id, user_id, content, due_ts FROM scheduled WHERE done = 0 AND due_ts > ?',
        (now,)).fetchall()
    return [{'id': r[0], 'group_id': r[1], 'user_id': r[2], 'content': r[3], 'due_ts': r[4]} for r in rows]


def list_scheduled(group_id=None):
    conn = get_conn()
    sql = 'SELECT id, group_id, user_id, content, due_ts FROM scheduled WHERE done = 0'
    params = []
    if group_id:
        sql += ' AND group_id = ?'
        params.append(group_id)
    sql += ' ORDER BY due_ts ASC LIMIT 20'
    rows = conn.execute(sql, params).fetchall()
    return [{'id': r[0], 'group_id': r[1], 'user_id': r[2], 'content': r[3], 'due_ts': r[4]} for r in rows]


def cancel_scheduled(task_id, group_id=None):
    conn = get_conn()
    if group_id:
        conn.execute('UPDATE scheduled SET done = 1 WHERE id = ? AND group_id = ?', (task_id, group_id))
    else:
        conn.execute('UPDATE scheduled SET done = 1 WHERE id = ?', (task_id,))
    conn.commit()


def mark_scheduled_done(task_id):
    conn = get_conn()
    conn.execute('UPDATE scheduled SET done = 1 WHERE id = ?', (task_id,))
    conn.commit()


def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime('%H:%M:%S')
