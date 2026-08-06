# AI 对话插件：OpenAI 兼容接口
# 用法: $ai <问题> （或开启 direct_chat 后，私聊直接对话 / 群聊 @机器人 对话）
#
# 配置（config.json 的 "ai" 块，可在 WebUI Bot 配置中编辑）：
#   enabled:        是否启用
#   api_base:       OpenAI 兼容接口地址，如 https://api.deepseek.com/v1
#   api_key:        API Key（留空时回退使用 config 的 openai_key / deepseek_key）
#   model:          模型名，如 deepseek-chat / gpt-4o-mini
#   system_prompt:  系统提示词
#   temperature:    采样温度 (0~2)
#   max_tokens:     最大生成 token 数
#   max_history:    每个会话保留的上下文轮数
#   direct_chat:    是否支持直接对话（私聊任意消息 / 群聊 @机器人）
#   allow_users:    允许使用的用户 QQ 列表（空 = 全部）
#   allow_groups:   允许使用的群号列表（空 = 全部）
# 触发方式用 Any（永久触发）：插件内部自行判断命令模式 / 直接对话模式
TRIGGHT_KEYWORD = 'Any'
PLT_ST = 1
HELP_MESSAGE = 'AI 对话，用法: $ai <问题>'

import asyncio
import re

import aiohttp

_session = None
_histories = {}


def _cfg(bot):
    return bot.config.get('ai', {}) or {}


def _session_ref():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _check_permission(event, cfg):
    allowed_users = cfg.get('allow_users', [])
    allowed_groups = cfg.get('allow_groups', [])
    if allowed_users and event.user_id not in allowed_users:
        return False
    group_id = getattr(event, 'group_id', None)
    if group_id and allowed_groups and group_id not in allowed_groups:
        return False
    return True


def _is_mentioning_bot(event):
    if not hasattr(event, 'message'):
        return False
    self_id = str(getattr(event, 'self_id', 0))
    for seg in event.message:
        if seg.type == 'at' and str(seg.data.get('qq', '')) in (self_id, 'all'):
            return True
    return False


def _history_key(event):
    group_id = getattr(event, 'group_id', None)
    if group_id:
        return f'group:{group_id}:{event.user_id}'
    return f'private:{event.user_id}'


def _extract_question(event, args, cfg):
    if args and args.strip():
        return args.strip()
    if cfg.get('direct_chat'):
        text = re.sub(r'@\S+\s*', '', event.get_text())
        return text.strip()
    return ''


async def _ask_ai(bot, cfg, event, question):
    api_base = cfg.get('api_base', '').rstrip('/')
    api_key = cfg.get('api_key') or bot.config.get('openai_key') or bot.config.get('deepseek_key')
    model = cfg.get('model', 'gpt-3.5-turbo')
    if not api_base or not api_key:
        return '⚠️ AI 未配置：请在 WebUI「Bot端配置」中填写 AI 接口地址和 Key，并启用 AI 对话'

    key = _history_key(event)
    history = _histories.get(key)
    if not history:
        history = [{'role': 'system', 'content': cfg.get('system_prompt', '你是 Lunar X 的 AI 助手，请用中文简洁友好地回答问题。')}]
    history.append({'role': 'user', 'content': question})

    payload = {
        'model': model,
        'messages': history,
        'temperature': cfg.get('temperature', 0.7),
        'max_tokens': cfg.get('max_tokens', 2048),
    }
    try:
        async with _session_ref().post(
            f'{api_base}/chat/completions',
            json=payload,
            headers={'Authorization': f'Bearer {api_key}'},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status == 401:
                return '⚠️ AI Key 无效（401），请检查配置'
            if resp.status == 429:
                return '⚠️ AI 请求过于频繁（429），请稍后再试'
            if resp.status != 200:
                return f'⚠️ AI 接口错误（HTTP {resp.status}）'
            data = await resp.json()
        content = data['choices'][0]['message']['content'].strip()
    except asyncio.TimeoutError:
        return '⚠️ AI 响应超时，请稍后再试'
    except Exception as e:
        return f'⚠️ AI 调用失败: {e}'

    history.append({'role': 'assistant', 'content': content})
    max_history = int(cfg.get('max_history', 10))
    if len(history) > 1 + max_history * 2:
        history = [history[0]] + history[-(max_history * 2):]
    _histories[key] = history
    return content


async def on_message(event, bot):
    cfg = _cfg(bot)
    if not cfg.get('enabled', False):
        return False
    if not _check_permission(event, cfg):
        return False

    is_command = bool(event.is_command and event.command == 'ai')
    direct = cfg.get('direct_chat', False) and (
        not getattr(event, 'group_id', None) or _is_mentioning_bot(event)
    )

    if not (is_command or direct):
        return False

    question = _extract_question(event, getattr(event, 'args', ''), cfg)
    if not question:
        await bot.send('用法: $ai <问题>',
                       group_id=getattr(event, 'group_id', None),
                       user_id=event.user_id)
        return True

    reply = await _ask_ai(bot, cfg, event, question)
    await bot.send(reply,
                   group_id=getattr(event, 'group_id', None),
                   user_id=event.user_id)
    return True
