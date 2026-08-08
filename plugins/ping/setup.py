# 演示插件：ping / pong（展示 Milky 段映射：mention + face）
TRIGGHT_KEYWORD = 'ping'
PLT_ST = 10
HELP_MESSAGE = '测试连通性，回复 pong'


async def on_message(event, bot):
    if hasattr(event, 'group_id') and event.group_id:
        # 群聊：@发送者 + 文本 + 表情（内部段 → Milky mention/face 段）
        segments = [bot.msg.at(event.user_id), bot.msg.text(' pong 🏓'), bot.msg.face(178)]
        await bot.send(segments, group_id=event.group_id)
    else:
        await bot.send([bot.msg.text('pong 🏓'), bot.msg.face(178)], user_id=event.user_id)
    return True
