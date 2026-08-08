# -*- coding: utf-8 -*-
"""工具箱插件：定时任务 + 群管理 + 消息持久化

指令（群管理需管理员权限）：
  $定时 <时长或时间> <内容>   设置定时提醒（10分钟 / 2小时 / 09:00）
  $定时列表 / $定时取消 <编号>
  $禁言 @某人 <时长> / $解禁 @某人
  $踢人 @某人
  $全体禁言 / $取消全体禁言
  $公告 <内容>
  $历史消息 <关键词> [条数]    搜索本群持久化消息
  $群消息统计                 今日群消息数
"""
TRIGGHT_KEYWORD = 'Any'
PLT_ST = 2
HELP_MESSAGE = '工具箱：定时任务 / 群管理 / 消息持久化'

from core.events import MessageEvent, Events

from scheduler import Scheduler
from store import save_message, search_messages, count_messages
import group_admin

scheduler = Scheduler()


async def on_lunar_event(event, bot):
    """框架启动时恢复未到期的定时任务"""
    if isinstance(event, Events.LunarStartListen):
        import asyncio
        asyncio.ensure_future(scheduler.restore(bot))


def _should_log(cfg, event):
    """这条消息要不要入库。以前是无条件全存，现在受配置控制。"""
    if not cfg.get('log_messages', True):
        return False
    group_id = getattr(event, 'group_id', None)
    if not group_id:
        return bool(cfg.get('log_private', False))
    allow = cfg.get('log_groups', [])
    return (not allow) or (group_id in allow)      # 留空 = 不限制


async def on_message(event, bot):
    cfg = bot.plugin_config(__name__)

    # 1. 消息持久化（不阻断其他插件）
    if isinstance(event, MessageEvent) and _should_log(cfg, event):
        save_message(event)

    if not event.is_command:
        return False

    cmd = event.command
    args = (event.args or '').strip()
    group_id = getattr(event, 'group_id', None)

    if cmd == '定时':
        return await scheduler.handle(bot, event, args)

    if cmd in ('禁言', '解禁', '踢人', '全体禁言', '取消全体禁言', '公告'):
        return await group_admin.handle(bot, event, cmd, args)

    if cmd == '历史消息':
        if not args:
            await bot.send('用法: $历史消息 <关键词> [条数]，搜索本群消息记录', group_id=group_id)
            return True
        parts = args.split(' ', 1)
        keyword = parts[0]
        limit = 20
        if len(parts) > 1:
            try:
                limit = min(int(parts[1].strip()), 50)
            except ValueError:
                pass
        results = search_messages(keyword, group_id=group_id, limit=limit)
        if not results:
            await bot.send(f'未找到包含「{keyword}」的消息', group_id=group_id)
            return True
        lines = [f"[{__import__('time').strftime('%H:%M', __import__('time').localtime(r['ts']))}] {r['user_id']}: {r['text'][:60]}" for r in results]
        await bot.send(f'🔍 找到 {len(results)} 条包含「{keyword}」的消息：\n' + '\n'.join(lines), group_id=group_id)
        return True

    if cmd == '群消息统计':
        rows = count_messages(1, group_id=group_id)
        total = sum(r[1] for r in rows)
        detail = '\n'.join(f"{r[0]}: {r[1]} 条" for r in rows[:7]) or '暂无'
        await bot.send(f'📊 本群消息统计（近 7 天）：\n{detail}\n今日合计: {total} 条', group_id=group_id)
        return True

    return False
