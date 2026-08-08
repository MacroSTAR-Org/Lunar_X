# -*- coding: utf-8 -*-
"""AI 对话插件（OpenAI 兼容接口）

触发方式（可叠加）：
  · $ai <问题>                命令
  · 私聊任意消息              需开启「私聊直连」
  · 群聊 @机器人              需开启「私聊直连」
  · 群聊以唤醒词开头          如「小月 今天天气」

配置在 WebUI 的「插件管理 → ai_chat → 配置」里改，
落盘位置是本目录的 config.json，字段说明见 README.md。
"""
TRIGGHT_KEYWORD = 'Any'
PLT_ST = 1
HELP_MESSAGE = 'AI 对话，用法: $ai <问题>'

import asyncio
import re

import aiohttp

from core.events import MessageEvent

_session = None
_histories = {}


def _cfg(bot):
    # __name__ 就是插件名（框架用 spec_from_file_location(插件名, ...) 注册模块）
    return bot.plugin_config(__name__)


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


def _plain_text(bot, event):
    """取纯文本：event.get_text() 会把 reply 段渲染成 "[回复]"、
    把 @ 渲染成 "@QQ号"，用它做前缀匹配会误判"""
    return bot.reply.get_plain_text(event.message).strip()


def _extract_question(bot, event, args, cfg, nickname=''):
    if args and args.strip():
        return args.strip()
    text = _plain_text(bot, event)
    # 昵称前缀剥离（"小月 今天天气" → "今天天气"）
    if nickname:
        if text.startswith(nickname):
            rest = text[len(nickname):]
            rest = re.sub(r'^[，。,.!！?？:：、;；\s]+', '', rest)
            return rest
    if cfg.get('direct_chat'):
        return text
    return ''


def _persona(cfg):
    return cfg.get('persona', {}) or {}


def _persona_nicknames(cfg):
    """人格唤醒词列表（兼容旧的 nickname 单值字段）"""
    p = _persona(cfg)
    nicks = p.get('nicknames')
    if isinstance(nicks, list):
        return [n.strip() for n in nicks if n and n.strip()]
    n = (p.get('nickname') or '').strip()
    return [n] if n else []


async def _ask_ai(bot, cfg, event, question):
    api_base = (cfg.get('api_base') or '').rstrip('/')
    api_key = cfg.get('api_key')
    model = cfg.get('model') or 'gpt-3.5-turbo'
    if not api_base or not api_key:
        return '⚠️ AI 未配置：请在 WebUI「插件管理 → ai_chat → 配置」里填写接口地址和 Key'

    key = _history_key(event)
    history = _histories.get(key)
    if not history:
        # 人格提示词优先，其次通用提示词
        system_prompt = (_persona(cfg).get('system_prompt') or cfg.get('system_prompt')
                         or '你是 Lunar X 的 AI 助手，请用中文简洁友好地回答问题。')
        history = [{'role': 'system', 'content': system_prompt}]
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
    # Any 触发会收到所有事件（含通知/请求类），非消息事件直接跳过
    if not isinstance(event, MessageEvent):
        return False

    cfg = _cfg(bot)
    if not cfg.get('enabled', False):
        return False
    if not _check_permission(event, cfg):
        return False

    group_id = getattr(event, 'group_id', None)
    is_command = bool(event.is_command and event.command == 'ai')
    direct = cfg.get('direct_chat', False) and (not group_id or _is_mentioning_bot(event))

    # 名字唤醒：群聊消息以任一唤醒词开头
    matched_nick = ''
    if group_id and _persona(cfg).get('enabled', True):
        nicknames = _persona_nicknames(cfg)
        if nicknames:
            text = _plain_text(bot, event)
            # 按长度降序匹配，避免短词吃掉长词
            for nick in sorted(nicknames, key=len, reverse=True):
                if text.startswith(nick):
                    matched_nick = nick
                    break

    if not (is_command or direct or matched_nick):
        return False

    question = _extract_question(bot, event, getattr(event, 'args', ''), cfg, matched_nick)
    if not question:
        await bot.send('用法: $ai <问题>', group_id=group_id, user_id=event.user_id)
        return True

    reply = await _ask_ai(bot, cfg, event, question)
    await bot.send(reply, group_id=group_id, user_id=event.user_id)
    return True
