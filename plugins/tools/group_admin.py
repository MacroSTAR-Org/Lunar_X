"""工具箱插件：群管理指令（需管理员权限）"""

import re

DURATION_UNITS = {
    "秒": 1,
    "s": 1,
    "分钟": 60,
    "分": 60,
    "m": 60,
    "小时": 3600,
    "时": 3600,
    "h": 3600,
    "天": 86400,
    "日": 86400,
    "d": 86400,
}


def parse_duration(text):
    """解析时长，纯数字默认分钟"""
    text = text.strip().lower()
    m = re.match(r"^(\d+)\s*([\u4e00-\u9fa5a-z]+)?$", text)
    if not m:
        return None
    num = int(m.group(1))
    unit = m.group(2) or "分钟"
    if unit not in DURATION_UNITS:
        return None
    return num * DURATION_UNITS[unit]


def extract_target_user(event, args):
    """从消息的 @ 段或参数文本中提取目标 QQ 号"""
    for seg in getattr(event, "message", []) or []:
        if seg.type == "at" and seg.data.get("qq", "") != "all":
            try:
                return int(seg.data["qq"])
            except (TypeError, ValueError):
                pass
    m = re.search(r"\b(\d{5,})\b", args)
    if m:
        return int(m.group(1))
    return None


async def handle(bot, event, cmd, args):
    group_id = getattr(event, "group_id", None)
    if not group_id:
        await bot.send("群管理指令仅支持群聊使用", user_id=event.user_id)
        return True

    # 权限：管理员及以上
    if not bot._check_permission(event.user_id, "manager"):
        await bot.send("权限不足，只有管理员才能使用群管理指令", group_id=group_id)
        return True

    # ---------- 禁言 / 解禁 ----------
    if cmd in ("禁言", "解禁"):
        if not args:
            await bot.send(
                f"用法: ${cmd} @某人 <时长>（时长示例: 10分钟 / 1小时 / 30秒）", group_id=group_id
            )
            return True
        target = extract_target_user(event, args)
        if not target:
            await bot.send("请 @ 要操作的用户或提供 QQ 号", group_id=group_id)
            return True
        if cmd == "禁言":
            duration = parse_duration(args)
            if duration is None:
                await bot.send("时长格式不支持，示例: 10分钟 / 1小时 / 30秒", group_id=group_id)
                return True
            result = await bot.diy.set_group_ban(
                group_id=group_id, user_id=target, duration=duration
            )
        else:
            result = await bot.diy.set_group_ban(group_id=group_id, user_id=target, duration=0)
        if result and result.get("status") == "ok":
            await bot.send(
                f"✅ 已{'禁言' if cmd == '禁言' else '解除禁言'} {target}"
                + (f"（{args.split(' ', 1)[0]}）" if cmd == "禁言" else ""),
                group_id=group_id,
            )
        else:
            await bot.send(
                f"操作失败: {result.get('msg', '未知错误') if result else '无响应'}",
                group_id=group_id,
            )
        return True

    # ---------- 踢人 ----------
    if cmd == "踢人":
        if not args:
            await bot.send("用法: $踢人 @某人 或 $踢人 <QQ号>", group_id=group_id)
            return True
        target = extract_target_user(event, args)
        if not target:
            await bot.send("请 @ 要踢出的用户或提供 QQ 号", group_id=group_id)
            return True
        result = await bot.diy.kick_group_member(group_id=group_id, user_id=target)
        if result and result.get("status") == "ok":
            await bot.send(f"✅ 已将 {target} 移出群聊", group_id=group_id)
        else:
            await bot.send(
                f"操作失败: {result.get('msg', '未知错误') if result else '无响应'}",
                group_id=group_id,
            )
        return True

    # ---------- 全体禁言 / 取消 ----------
    if cmd in ("全体禁言", "取消全体禁言"):
        enable = cmd == "全体禁言"
        result = await bot.diy.set_group_whole_ban(group_id=group_id, enable=enable)
        if result and result.get("status") == "ok":
            await bot.send(f"✅ 已{'开启' if enable else '取消'}全体禁言", group_id=group_id)
        else:
            await bot.send(
                f"操作失败: {result.get('msg', '未知错误') if result else '无响应'}",
                group_id=group_id,
            )
        return True

    # ---------- 群公告 ----------
    if cmd == "公告":
        if not args:
            await bot.send("用法: $公告 <公告内容>", group_id=group_id)
            return True
        result = await bot.diy.send_group_announcement(group_id=group_id, content=args)
        if result and result.get("status") == "ok":
            await bot.send("✅ 群公告已发布", group_id=group_id)
        else:
            await bot.send(
                f"发布失败: {result.get('msg', '未知错误') if result else '无响应'}",
                group_id=group_id,
            )
        return True

    return False
