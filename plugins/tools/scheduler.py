# -*- coding: utf-8 -*-
"""工具箱插件：定时任务（支持相对时间/绝对时间，SQLite 持久化，重启恢复）"""
import asyncio
import re
from datetime import datetime, timedelta

from store import (add_scheduled, load_pending_scheduled, list_scheduled,
                   cancel_scheduled, mark_scheduled_done, fmt_time)

DURATION_UNITS = {
    '秒': 1, 's': 1, 'sec': 1,
    '分钟': 60, '分': 60, 'm': 60, 'min': 60,
    '小时': 3600, '时': 3600, 'h': 3600, 'hour': 3600,
    '天': 86400, '日': 86400, 'd': 86400, 'day': 86400,
}


def parse_duration(text):
    """解析时长：'30秒' '5分钟' '2小时' '1天'，纯数字默认分钟"""
    text = text.strip().lower()
    m = re.match(r'^(\d+)\s*([\u4e00-\u9fa5a-z]+)?$', text)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2) or '分钟'
    if unit not in DURATION_UNITS:
        return None
    return num * DURATION_UNITS[unit]


def parse_absolute_time(text):
    """解析绝对时间 'HH:MM'（今天，过了则明天）"""
    m = re.match(r'^(\d{1,2}):(\d{2})$', text.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    now = datetime.now()
    due = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    return (due - now).total_seconds()


class Scheduler:
    def __init__(self):
        self._running = {}

    async def restore(self, bot):
        """启动时恢复未到期的定时任务"""
        for task in load_pending_scheduled():
            delay = task['due_ts'] - __import__('time').time()
            if delay <= 0:
                continue
            await self._arm(bot, task['id'], task['group_id'], task['content'], delay)

    async def _arm(self, bot, task_id, group_id, content, delay):
        async def _run():
            await asyncio.sleep(max(delay, 0))
            try:
                await bot.send(f'⏰ 定时提醒：{content}', group_id=group_id)
            except Exception:
                pass
            mark_scheduled_done(task_id)
            self._running.pop(task_id, None)
        task = asyncio.create_task(_run())
        self._running[task_id] = task

    async def handle(self, bot, event, args):
        group_id = getattr(event, 'group_id', None)
        if not group_id:
            await bot.send('定时任务仅支持群聊使用', user_id=event.user_id)
            return True

        args = args.strip()
        if not args:
            await bot.send('用法: $定时 <时长或时间> <内容>\n'
                           '示例: $定时 10分钟 提醒喝水\n'
                           '      $定时 09:00 早会提醒\n'
                           '      $定时列表 / $定时取消 <编号>', group_id=group_id)
            return True

        if args.startswith('列表'):
            tasks = list_scheduled(group_id)
            if not tasks:
                await bot.send('当前群没有待触发的定时任务', group_id=group_id)
                return True
            lines = [f"#{t['id']} {fmt_time(t['due_ts'])} - {t['content']}" for t in tasks]
            await bot.send('📋 本群定时任务：\n' + '\n'.join(lines), group_id=group_id)
            return True

        if args.startswith('取消'):
            try:
                task_id = int(args[2:].strip())
            except ValueError:
                await bot.send('格式: $定时取消 <编号>（用 $定时列表 查看编号）', group_id=group_id)
                return True
            cancel_scheduled(task_id, group_id)
            self._running.pop(task_id, None)
            await bot.send(f'已取消定时任务 #{task_id}', group_id=group_id)
            return True

        parts = args.split(' ', 1)
        first = parts[0].strip()
        content = parts[1].strip() if len(parts) > 1 else '定时提醒'

        delay = parse_duration(first)
        if delay is None:
            delay = parse_absolute_time(first)
        if delay is None:
            await bot.send('时间格式不支持，示例: $定时 10分钟 内容 或 $定时 09:00 内容', group_id=group_id)
            return True

        due_ts = int(__import__('time').time()) + int(delay)
        task_id = add_scheduled(group_id, event.user_id, content, due_ts)
        await self._arm(bot, task_id, group_id, content, delay)
        await bot.send(f'✅ 已设置定时任务 #{task_id}，将于 {fmt_time(due_ts)} 触发：{content}',
                       group_id=group_id)
        return True
