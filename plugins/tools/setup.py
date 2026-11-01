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
  $清理消息 [天数]            清理历史消息（管理员，默认按保留天数清理）
"""

TRIGGHT_KEYWORD = "Any"
PLT_ST = 2
HELP_MESSAGE = "工具箱：定时任务 / 群管理 / 消息持久化"

import asyncio
import contextlib

import group_admin
from scheduler import Scheduler
from store import (
    close_db,
    count_messages,
    count_stored_messages,
    purge_all_messages,
    purge_old_messages,
    save_message,
    search_messages,
)

from core.events import Events, MessageEvent

scheduler = Scheduler()
_restore_task = None
_purge_task = None

# 保留策略检查间隔：1 小时
_PURGE_INTERVAL = 3600


async def on_lunar_event(event, bot):
    """框架启动时恢复未到期的定时任务"""
    global _restore_task
    if isinstance(event, Events.LunarStartListen):
        _restore_task = asyncio.create_task(scheduler.restore(bot))
        _start_purge_loop(bot)


def _start_purge_loop(bot):
    """定期清理过期消息。群消息是个人数据，不该无限期留在磁盘上。"""
    global _purge_task
    if _purge_task and not _purge_task.done():
        return

    async def _loop():
        while True:
            try:
                cfg = bot.plugin_config(__name__)
                days = int(cfg.get("retention_days", 30) or 0)
                if days > 0 and cfg.get("log_messages"):
                    deleted = purge_old_messages(days)
                    if deleted:
                        bot.plugin_logger.info(f"已清理 {deleted} 条超过 {days} 天的历史消息")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                bot.plugin_logger.warning(f"清理历史消息失败: {e}")
            await asyncio.sleep(_PURGE_INTERVAL)

    _purge_task = asyncio.create_task(_loop())


async def on_unload(bot):
    """热重载 / 退出时取消定时任务并关闭 SQLite 连接。"""
    global _restore_task, _purge_task
    for task in (_restore_task, _purge_task):
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _restore_task = None
    _purge_task = None
    await scheduler.shutdown()
    close_db()


def _should_log(cfg, event):
    """这条消息要不要入库。以前是无条件全存，现在受配置控制。"""
    if not cfg.get("log_messages", True):
        return False
    group_id = getattr(event, "group_id", None)
    if not group_id:
        return bool(cfg.get("log_private", False))
    allow = cfg.get("log_groups", [])
    return (not allow) or (group_id in allow)  # 留空 = 不限制


async def on_message(event, bot):
    cfg = bot.plugin_config(__name__)

    # 1. 消息持久化（不阻断其他插件）
    if isinstance(event, MessageEvent) and _should_log(cfg, event):
        save_message(event)

    if not event.is_command:
        return False

    cmd = event.command
    args = (event.args or "").strip()
    group_id = getattr(event, "group_id", None)

    if cmd in ("定时", "定时列表", "定时取消"):
        # 文档里的 $定时列表 / $定时取消 是独立命令，不是 $定时 的参数
        if cmd == "定时列表":
            args = "列表"
        elif cmd == "定时取消":
            args = f"取消 {args}".strip()
        return await scheduler.handle(bot, event, args)

    if cmd in ("禁言", "解禁", "踢人", "全体禁言", "取消全体禁言", "公告"):
        return await group_admin.handle(bot, event, cmd, args)

    if cmd == "历史消息":
        if not group_id:
            await bot.send("消息检索仅支持群聊使用", user_id=event.user_id)
            return True
        if not args:
            await bot.send("用法: $历史消息 <关键词> [条数]，搜索本群消息记录", group_id=group_id)
            return True
        parts = args.split(None, 1)
        keyword = parts[0].strip()
        max_limit = max(1, min(int(cfg.get("search_limit", 50)), 200))
        limit = min(20, max_limit)
        if len(parts) > 1:
            import contextlib

            with contextlib.suppress(ValueError):
                limit = max(1, min(int(parts[1].strip()), max_limit))
        results = search_messages(keyword, group_id=group_id, limit=limit)
        if not results:
            await bot.send(f"未找到包含「{keyword}」的消息", group_id=group_id)
            return True
        lines = [
            f"[{__import__('time').strftime('%H:%M', __import__('time').localtime(r['ts']))}] {r['user_id']}: {r['text'][:60]}"
            for r in results
        ]
        await bot.send(
            f"🔍 找到 {len(results)} 条包含「{keyword}」的消息：\n" + "\n".join(lines),
            group_id=group_id,
        )
        return True

    if cmd == "群消息统计":
        if not group_id:
            await bot.send("消息统计仅支持群聊使用", user_id=event.user_id)
            return True
        rows = count_messages(7, group_id=group_id)
        total = sum(r[1] for r in rows)
        detail = "\n".join(f"{r[0]}: {r[1]} 条" for r in rows[:7]) or "暂无"
        await bot.send(
            f"📊 本群消息统计（近 7 天）：\n{detail}\n合计: {total} 条", group_id=group_id
        )
        return True

    if cmd == "清理消息":
        return await _handle_purge(bot, event, args, cfg)

    return False


async def _handle_purge(bot, event, args: str, cfg: dict) -> bool:
    """$清理消息 [天数|全部]

    天数缺省时按配置的保留天数清理；给了天数则清理该天数以前的消息；
    「全部」会清空整库消息（仅管理员）。
    """
    if not bot._check_permission(event.user_id, "manager"):
        await bot.send("仅管理员可清理消息记录", user_id=event.user_id)
        return True

    total = count_stored_messages()
    args = (args or "").strip()

    if args in ("全部", "all"):
        deleted = purge_all_messages()
        await bot.send(
            f"🗑 已清空全部历史消息（删除 {deleted} 条，清理前共 {total} 条）",
            user_id=event.user_id,
        )
        return True

    if args:
        try:
            days = int(args)
        except ValueError:
            await bot.send("用法: $清理消息 [天数|全部]", user_id=event.user_id)
            return True
        if days <= 0:
            await bot.send("天数需为正整数，或用「全部」清空", user_id=event.user_id)
            return True
    else:
        days = int(cfg.get("retention_days", 30) or 0)
        if days <= 0:
            await bot.send(
                "当前保留天数为 0（永久保留）。请指定天数：$清理消息 30，或用「全部」清空",
                user_id=event.user_id,
            )
            return True

    deleted = purge_old_messages(days)
    await bot.send(
        f"🗑 已清理 {deleted} 条超过 {days} 天的消息（清理前共 {total} 条）",
        user_id=event.user_id,
    )
    return True
