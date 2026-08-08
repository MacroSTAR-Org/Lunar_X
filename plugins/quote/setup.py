# -*- coding: utf-8 -*-
"""群聊语录插件

指令：
  $收录 [分类] <内容>      收录一句话（分类可选，如：$收录 沙雕 今天又帅了）
                          也可以引用（回复）一条消息后直接发 $收录
  $语录 [分类]            随机播放一条（该分类）
  $语录列表 [分类]        查看最近语录（带编号）
  $语录搜索 <关键词>       搜索语录
  $删除语录 <编号>         删除（仅管理员）

配置在 WebUI 的「插件管理 → quote → 配置」里改，字段说明见 README.md。
数据库在 data/quote/quotes.db。
"""
TRIGGHT_KEYWORD = 'Any'
PLT_ST = 3
HELP_MESSAGE = '群聊语录：$收录 / $语录 / $语录列表 / $语录搜索 / $删除语录'

import os
import random
import shutil
import sqlite3
import time
from datetime import datetime

_conn = None
_last_id = None  # 避免随机连续重复


def _cfg(bot):
    return bot.plugin_config(__name__)


def get_conn(bot):
    """懒初始化数据库连接。

    数据目录统一走 bot.plugin_data_dir()，插件挪目录也不会断——
    以前是 `dirname(dirname(__file__))/data` 硬算层数，
    这个插件从 plugins/quote.py 搬进 plugins/quote/ 之后层数就变了。
    """
    global _conn
    if _conn is not None:
        return _conn

    data_dir = bot.plugin_data_dir(__name__)
    db_path = os.path.join(data_dir, 'quotes.db')

    # 一次性迁移：把旧位置 data/quotes.db 的数据搬过来，已有语录不丢
    legacy = os.path.join(os.path.dirname(data_dir), 'quotes.db')
    if not os.path.exists(db_path) and os.path.exists(legacy):
        shutil.move(legacy, db_path)
        bot.plugin_logger.info(f'[quote] 已迁移旧数据库 {legacy} → {db_path}')

    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.execute('''
        CREATE TABLE IF NOT EXISTS quotes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            category TEXT DEFAULT '',
            content TEXT NOT NULL,
            ts INTEGER NOT NULL
        )''')
    _conn.execute('CREATE INDEX IF NOT EXISTS idx_quotes_group ON quotes(group_id)')
    _conn.commit()
    return _conn


def add_quote(bot, group_id, user_id, category, content):
    conn = get_conn(bot)
    cur = conn.execute(
        'INSERT INTO quotes (group_id, user_id, category, content, ts) VALUES (?, ?, ?, ?, ?)',
        (group_id, user_id, category, content, int(time.time())))
    conn.commit()
    return cur.lastrowid


def random_quote(bot, group_id, category='', pool=500):
    global _last_id
    conn = get_conn(bot)
    if category:
        rows = conn.execute(
            'SELECT id, category, content, user_id, ts FROM quotes WHERE group_id = ? AND category = ? ORDER BY id DESC LIMIT ?',
            (group_id, category, pool)).fetchall()
    else:
        rows = conn.execute(
            'SELECT id, category, content, user_id, ts FROM quotes WHERE group_id = ? ORDER BY id DESC LIMIT ?',
            (group_id, pool)).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        candidates = [r for r in rows if r[0] != _last_id] or rows
        row = random.choice(candidates)
    else:
        row = rows[0]
    _last_id = row[0]
    return {'id': row[0], 'category': row[1], 'content': row[2], 'user_id': row[3], 'ts': row[4]}


def list_quotes(bot, group_id, category='', limit=20):
    conn = get_conn(bot)
    if category:
        rows = conn.execute(
            'SELECT id, category, content, user_id, ts FROM quotes WHERE group_id = ? AND category = ? ORDER BY id DESC LIMIT ?',
            (group_id, category, limit)).fetchall()
    else:
        rows = conn.execute(
            'SELECT id, category, content, user_id, ts FROM quotes WHERE group_id = ? ORDER BY id DESC LIMIT ?',
            (group_id, limit)).fetchall()
    return [{'id': r[0], 'category': r[1], 'content': r[2], 'user_id': r[3], 'ts': r[4]} for r in rows]


def search_quotes(bot, group_id, keyword, limit=20):
    conn = get_conn(bot)
    rows = conn.execute(
        'SELECT id, category, content, user_id, ts FROM quotes WHERE group_id = ? AND content LIKE ? ORDER BY id DESC LIMIT ?',
        (group_id, f'%{keyword}%', limit)).fetchall()
    return [{'id': r[0], 'category': r[1], 'content': r[2], 'user_id': r[3], 'ts': r[4]} for r in rows]


def delete_quote(bot, quote_id, group_id):
    conn = get_conn(bot)
    cur = conn.execute('DELETE FROM quotes WHERE id = ? AND group_id = ?', (quote_id, group_id))
    conn.commit()
    return cur.rowcount > 0


def fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')


async def _extract_reply_content(bot, event):
    """从消息的 reply 段获取被引用消息的文本内容

    优先使用 Milky 协议 reply 段自带的内容（segments），
    协议端未附带时（如 OneBot）再通过 get_message API 拉取。
    """
    for seg in getattr(event, 'message', []) or []:
        if seg.type == 'reply':
            reply_id = seg.data.get('id')
            group_id = getattr(event, 'group_id', None)
            if not reply_id:
                return None
            # 1) reply 段自带内容（Milky 1.2+，IncomingSegment 格式）
            quoted = seg.data.get('quoted_segments') or []
            text = ''.join(s.get('data', {}).get('text', '')
                           for s in quoted if s.get('type') == 'text')
            if text.strip():
                return text.strip()
            # 2) 降级：通过 get_message API 拉取（Milky IncomingMessage 的 segments 位于 data 顶层）
            if not group_id:
                return None
            try:
                result = await bot.diy.get_message(
                    message_scene='group', peer_id=group_id, message_seq=int(reply_id))
                if result and result.get('status') == 'ok':
                    msg = result.get('data', {})
                    segs = msg.get('segments', []) if isinstance(msg, dict) else []
                    text = ''.join(s.get('data', {}).get('text', '')
                                   for s in segs if s.get('type') == 'text')
                    return text.strip() or None
            except Exception:
                pass
    return None


async def on_message(event, bot):
    if not event.is_command:
        return False
    cmd = event.command
    args = (event.args or '').strip()
    group_id = getattr(event, 'group_id', None)
    if not group_id:
        if cmd in ('语录', '收录', '语录列表', '语录搜索', '删除语录'):
            await bot.send('语录功能仅支持群聊使用', user_id=event.user_id)
            return True
        return False

    cfg = _cfg(bot)
    max_len = int(cfg.get('max_content_length', 200))
    max_cat = int(cfg.get('max_category_length', 6))
    list_limit = int(cfg.get('list_limit', 20))
    pool = int(cfg.get('random_pool', 500))

    # ---------- 收录 ----------
    if cmd == '收录':
        if not args:
            # 引用模式：引用一条消息后发 $收录，收录被引用内容
            content = await _extract_reply_content(bot, event)
            if not content:
                await bot.send('用法: $收录 [分类] <内容>；或引用（回复）一条消息后发 $收录，收录被引用的内容',
                               group_id=group_id)
                return True
            category = ''
        else:
            parts = args.split(' ', 1)
            if len(parts) == 2 and len(parts[0]) <= max_cat:
                category, content = parts[0].strip(), parts[1].strip()
            else:
                category, content = '', args.strip()
        if len(content) > max_len:
            await bot.send(f'语录内容太长了（最多 {max_len} 字）', group_id=group_id)
            return True
        qid = add_quote(bot, group_id, event.user_id, category, content)
        prefix = f'[{category}] ' if category else ''
        await bot.send(f'📌 已收录 #{qid}：{prefix}{content}', group_id=group_id)
        return True

    # ---------- 随机语录 ----------
    if cmd == '语录':
        category = args.strip()
        q = random_quote(bot, group_id, category, pool)
        if not q:
            await bot.send(f'还没有{category + "分类的" if category else ""}语录，用 $收录 添加吧', group_id=group_id)
            return True
        prefix = f'[{q["category"]}] ' if q['category'] else ''
        await bot.send(f'💬 #{q["id"]} {prefix}{q["content"]}', group_id=group_id)
        return True

    # ---------- 语录列表 ----------
    if cmd == '语录列表':
        category = args.strip()
        quotes = list_quotes(bot, group_id, category, list_limit)
        if not quotes:
            await bot.send(f'还没有{category + "分类的" if category else ""}语录', group_id=group_id)
            return True
        lines = [f"#{q['id']} [{q['category'] or '默认'}] {q['content'][:50]}（{q['user_id']} {fmt_ts(q['ts'])}）"
                 for q in quotes]
        await bot.send(f'📚 最近 {len(quotes)} 条语录：\n' + '\n'.join(lines), group_id=group_id)
        return True

    # ---------- 搜索 ----------
    if cmd == '语录搜索':
        if not args:
            await bot.send('用法: $语录搜索 <关键词>', group_id=group_id)
            return True
        quotes = search_quotes(bot, group_id, args, list_limit)
        if not quotes:
            await bot.send(f'未找到包含「{args}」的语录', group_id=group_id)
            return True
        lines = [f"#{q['id']} [{q['category'] or '默认'}] {q['content'][:50]}" for q in quotes]
        await bot.send(f'🔍 找到 {len(quotes)} 条：\n' + '\n'.join(lines), group_id=group_id)
        return True

    # ---------- 删除（管理员）----------
    if cmd == '删除语录':
        if not bot._check_permission(event.user_id, 'manager'):
            await bot.send('权限不足，只有管理员才能删除语录', group_id=group_id)
            return True
        try:
            qid = int(args)
        except ValueError:
            await bot.send('用法: $删除语录 <编号>（用 $语录列表 查看编号）', group_id=group_id)
            return True
        if delete_quote(bot, qid, group_id):
            await bot.send(f'🗑️ 已删除语录 #{qid}', group_id=group_id)
        else:
            await bot.send(f'未找到语录 #{qid}（或不属于本群）', group_id=group_id)
        return True

    return False
