"""AI 对话插件（OpenAI 兼容接口）—— 双模式：agent（多智能体）/ classic（单模型直答）。

agent 模式（默认）是"赛博群友"管线：
    每条群消息先进内存旁听时间线（零 LLM 成本）；
    被 @/叫名字/命令/私聊时直接由回复器应答（跳过规划器，零延迟）；
    其余消息由防抖器攒批，交给规划器判断要不要回、要不要看图；
    回复走风格层（打字延迟/分段/表情），像人打字而不是模型输出。

classic 模式 = 插件最早的单模型直答行为，原样保留，随时可切回；
agent 管线故障时若开启 agent.fallback_classic 也会自动降级，保证不失联。

配置在 WebUI 的「插件管理 → ai_chat → 配置」里改，
落盘位置是本目录的 config.json，字段说明见 README.md。
"""

TRIGGHT_KEYWORD = "Any"
PLT_ST = 1
HELP_MESSAGE = "AI 对话，用法: $ai <问题>；发送 $ai 忘了我 可清除 AI 对你的长期记忆"

import asyncio
import contextlib
import random
import re
import time
from datetime import date
from typing import Any
from urllib.parse import urlparse

import agents
import aiohttp
import context as ctx
import memory_store
import reactions as reactions_mod
import style
import vision

from core.events import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupPokeNoticeEvent,
    GroupRecallNoticeEvent,
    MessageEvent,
)
from core.tools import ToolCallContext

_session = None
_histories: dict[str, list[dict[str, Any]]] = {}
_MAX_HISTORY_KEYS = 200

# 运行时句柄：框架每次事件都传同一个 bot 对象，这里缓存引用给防抖回调用
_bot: Any = None
_debouncer: ctx.ReviewDebouncer | None = None
_bubble_task: asyncio.Task | None = None

# 记忆抽取限流：同一用户 60s 内只抽一次（每次抽取都是一次 LLM 调用）
_last_extract: dict[int, float] = {}
_EXTRACT_INTERVAL = 60

# event+timer 冒泡的每日配额：{群号: (日期, 次数)}
_bubble_quota: dict[int, tuple[str, int]] = {}
_bubble_running = False

BUBBLE_TICK_SECONDS = 300
BUBBLE_RECENT_WINDOW = 1800  # 近 30 分钟有话才值得插
BUBBLE_CHANCE = 0.1
BUBBLE_DAILY_MAX = 3


async def on_unload(bot):
    """热重载 / 退出时释放全部资源：session、防抖任务、内存状态、记忆库连接。"""
    global _session, _debouncer, _bubble_task
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
    _histories.clear()
    if _debouncer is not None:
        _debouncer.cancel_all()
        _debouncer = None
    if _bubble_task is not None and not _bubble_task.done():
        _bubble_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _bubble_task
    _bubble_task = None
    for task in list(_background_tasks):
        task.cancel()
    _background_tasks.clear()
    ctx.clear_all()
    reactions_mod.clear_state()
    _last_extract.clear()
    _bubble_quota.clear()
    memory_store.close_db()


async def on_lunar_event(event, bot):
    """框架启动钩子：记录 bot 引用，供防抖审查回调与冒泡循环使用。"""
    global _bot, _bubble_task
    _bot = bot
    is_start = type(event).__name__ == "LunarStartListen"
    if is_start and (_bubble_task is None or _bubble_task.done()):
        _bubble_task = asyncio.create_task(_bubble_loop())


# 后台任务的强引用：create_task 的结果没人持有时可能被 GC 半路回收
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _cfg(bot):
    # __name__ 就是插件名（框架用 spec_from_file_location(插件名, ...) 注册模块）
    return bot.plugin_config(__name__)


def _session_ref():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _check_permission(event, cfg):
    allowed_users = cfg.get("allow_users", [])
    allowed_groups = cfg.get("allow_groups", [])
    if allowed_users and event.user_id not in allowed_users:
        return False
    group_id = getattr(event, "group_id", None)
    return not (group_id and allowed_groups and group_id not in allowed_groups)


def _is_mentioning_bot(event):
    if not hasattr(event, "message"):
        return False
    self_id = str(getattr(event, "self_id", 0))
    # @全体成员 不算在叫机器人，避免一条 @all 触发全群 AI
    return any(
        seg.type == "at" and str(seg.data.get("qq", "")) == self_id
        for seg in event.message
    )


def _history_key(event):
    group_id = getattr(event, "group_id", None)
    if group_id:
        return f"group:{group_id}:{event.user_id}"
    return f"private:{event.user_id}"


def _plain_text(bot, event):
    """取纯文本：event.get_text() 会把 reply 段渲染成 "[回复]"、
    把 @ 渲染成 "@QQ号"，用它做前缀匹配会误判"""
    return bot.reply.get_plain_text(event.message).strip()


def _extract_question(bot, event, args, cfg, nickname=""):
    if args and args.strip():
        return args.strip()
    text = _plain_text(bot, event)
    # 昵称前缀剥离（"小月 今天天气" → "今天天气"）
    if nickname and text.startswith(nickname):
        rest = text[len(nickname) :]
        rest = re.sub(r"^[，。,.!！?？:：、;；\s]+", "", rest)
        return rest
    if cfg.get("direct_chat"):
        return text
    return ""


def _persona(cfg):
    return cfg.get("persona", {}) or {}


def _persona_nicknames(cfg):
    """人格唤醒词列表（兼容旧的 nickname 单值字段）"""
    p = _persona(cfg)
    nicks = p.get("nicknames")
    if isinstance(nicks, list):
        return [n.strip() for n in nicks if n and n.strip()]
    n = (p.get("nickname") or "").strip()
    return [n] if n else []


def _is_safe_api_base(api_base: str) -> bool:
    """只允许 https，或本机 http（兼容本地 Ollama 等）。"""
    try:
        parsed = urlparse(api_base)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _api_ready(cfg) -> tuple[str, str] | None:
    """agent 管线的 API 检查；不就绪返回 None（调用方决定降级）。"""
    api_base = (cfg.get("api_base") or "").rstrip("/")
    api_key = cfg.get("api_key")
    if not api_base or not api_key or not _is_safe_api_base(api_base):
        return None
    return api_base, api_key


def _bot_name(bot) -> str:
    try:
        return str(bot.config.get("bot_name") or "Lunar X")
    except AttributeError:
        return "Lunar X"


# ---------------------------------------------------------------------------
# classic 模式：单模型直答（原行为保真，勿动生成逻辑）
# ---------------------------------------------------------------------------


async def _ask_ai(bot, cfg, event, question):
    api_base = (cfg.get("api_base") or "").rstrip("/")
    api_key = cfg.get("api_key")
    model = cfg.get("model") or "gpt-3.5-turbo"
    if not api_base or not api_key:
        return "⚠️ AI 未配置：请在 WebUI「插件管理 → ai_chat → 配置」里填写接口地址和 Key"
    if not _is_safe_api_base(api_base):
        return "⚠️ API 地址不安全：只允许 https（或本机 http）"

    question = question[:4000]
    key = _history_key(event)
    history = _histories.get(key)
    if not history:
        # 人格提示词优先，其次通用提示词
        system_prompt = (
            _persona(cfg).get("system_prompt")
            or cfg.get("system_prompt")
            or agents.DEFAULT_PERSONA_PROMPT
        )
        history = [{"role": "system", "content": system_prompt}]
    history.append({"role": "user", "content": question})

    temperature = _coerce_float(cfg.get("temperature", 0.7), 0.7)
    max_tokens = _coerce_int(cfg.get("max_tokens", 2048), 2048, 64, 8192)

    payload = {
        "model": model,
        "messages": history,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        async with _session_ref().post(
            f"{api_base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as resp:
            if resp.status == 401:
                return "⚠️ AI Key 无效（401），请检查配置"
            if resp.status == 429:
                return "⚠️ AI 请求过于频繁（429），请稍后再试"
            if resp.status != 200:
                return f"⚠️ AI 接口错误（HTTP {resp.status}）"
            data = await resp.json()
        content = data["choices"][0]["message"]["content"].strip()
    except TimeoutError:
        return "⚠️ AI 响应超时，请稍后再试"
    except Exception as e:
        return f"⚠️ AI 调用失败: {e}"

    history.append({"role": "assistant", "content": content})
    max_history = max(0, min(int(cfg.get("max_history", 10)), 50))
    if len(history) > 1 + max_history * 2:
        history = [history[0], *history[-(max_history * 2) :]]
    _histories[key] = history
    # 防止长期运行后按用户/群无限累积
    while len(_histories) > _MAX_HISTORY_KEYS:
        _histories.pop(next(iter(_histories)), None)
    return content


def _coerce_float(value, fallback: float) -> float:
    try:
        return max(0.0, min(float(value), 2.0))
    except (TypeError, ValueError):
        return fallback


def _coerce_int(value, fallback: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(value), maximum))
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# agent 模式：旁听 → 防抖 → 规划 → 分派
# ---------------------------------------------------------------------------


def _ensure_debouncer(cfg) -> ctx.ReviewDebouncer:
    """按配置创建/更新防抖器；审查回调需要 bot 引用，走模块级 _bot。"""
    global _debouncer
    if _debouncer is None:
        _debouncer = ctx.ReviewDebouncer(_review_group)
    _debouncer.quiet_seconds = _coerce_int(ctx.get_cfg(cfg, "planner.quiet_seconds", 20), 20, 5, 300)
    _debouncer.batch_max = _coerce_int(ctx.get_cfg(cfg, "planner.batch_max", 12), 12, 3, 50)
    _debouncer.max_wait_seconds = _coerce_int(
        ctx.get_cfg(cfg, "planner.max_wait_seconds", 120), 120, 30, 600
    )
    return _debouncer


def _timeline_for(bot, cfg, event) -> ctx.GroupTimeline | None:
    """取时间线；旁听只认群聊且 agent 模式。白名单已在 _check_permission 拦过。"""
    group_id = getattr(event, "group_id", None)
    if not group_id or str(cfg.get("mode", "agent")) != "agent":
        return None
    if not ctx.get_cfg(cfg, "listen.enabled", True):
        return None
    return ctx.get_timeline(
        group_id,
        max_entries=_coerce_int(ctx.get_cfg(cfg, "listen.max_entries", 30), 30, 10, 100),
        ttl_minutes=_coerce_int(ctx.get_cfg(cfg, "listen.ttl_minutes", 120), 120, 10, 360),
    )


def _listen_text(bot, event) -> str:
    """旁听文本：纯文本；空文本但有图记占位；两者皆空则不入线。"""
    text = _plain_text(bot, event)
    if text:
        return text
    if vision.collect_image_segments(getattr(event, "message", [])):
        return "[图片]"
    return ""


def _nickname_mentioned_in_middle(bot, event, cfg) -> str:
    """唤醒词出现在消息中间（非开头）——event 档按概率接话的触发点。"""
    if not _persona(cfg).get("enabled", True):
        return ""
    text = _plain_text(bot, event)
    if not text:
        return ""
    for nick in sorted(_persona_nicknames(cfg), key=len, reverse=True):
        if nick in text and not text.startswith(nick):
            return nick
    return ""


async def _handle_notice_event(event, bot) -> bool:
    """通知/请求类事件：写入时间线条目（环境感知）+ 触发事件反应。"""
    cfg = _cfg(bot)
    if not cfg.get("enabled", False):
        return False
    if not isinstance(
        event,
        (GroupPokeNoticeEvent, GroupRecallNoticeEvent, GroupIncreaseNoticeEvent, GroupDecreaseNoticeEvent),
    ):
        return False  # 心跳等高频 meta 事件直接忽略
    if not _check_permission(event, cfg):
        return False

    group_id = getattr(event, "group_id", None)
    timeline = None
    if group_id and str(cfg.get("mode", "agent")) == "agent" and ctx.get_cfg(cfg, "listen.enabled", True):
        timeline = ctx.get_timeline(
            group_id,
            max_entries=_coerce_int(ctx.get_cfg(cfg, "listen.max_entries", 30), 30, 10, 100),
            ttl_minutes=_coerce_int(ctx.get_cfg(cfg, "listen.ttl_minutes", 120), 120, 10, 360),
        )
        description = reactions_mod.describe_notice(event)
        if description:
            timeline.append(0, "", description)

    return await reactions_mod.handle_notice(event, bot, cfg, timeline)


async def _agent_reply(
    bot,
    cfg,
    event,
    question: str,
    timeline: ctx.GroupTimeline | None,
    reason: str,
    *,
    force_vision: bool = False,
) -> str | None:
    """回复器：多智能体管线的一次应答。

    返回 None 表示管线失败（调用方决定是否降级 classic）。
    图片处理：vision 开启则把图转成 content 片段；历史里只存占位文本。
    """
    api = _api_ready(cfg)
    if api is None:
        return None
    api_base, api_key = api
    model = cfg.get("model") or "gpt-3.5-turbo"
    group_id_ref = getattr(event, "group_id", None)

    has_images = bool(vision.collect_image_segments(getattr(event, "message", [])))
    use_vision = has_images and (ctx.get_cfg(cfg, "vision.enabled") or force_vision)

    # 本次请求的 user content：普通文本 或 [文本 + 图片片段]
    if use_vision:
        parts = await vision.build_image_parts(_session_ref(), bot, event, cfg)
        user_content: Any = (
            [
                {"type": "text", "text": question or "看看这张图"},
                *parts,
            ]
            if parts
            else (question or "[图片]")
        )
    else:
        user_content = question or "[图片]"

    key = _history_key(event)
    # build_reply_messages 自己负责 system 层，历史里只保留对话轮次
    history = [m for m in _histories.get(key, []) if m.get("role") != "system"]

    memory_lines: list[str] = []
    if ctx.get_cfg(cfg, "memory.enabled"):
        with contextlib.suppress(Exception):
            memory_lines = memory_store.recall_facts(event.user_id, question or "[图片]")

    style_samples: list[str] | None = None
    if ctx.get_cfg(cfg, "style.quote_fewshot") and group_id_ref:
        with contextlib.suppress(Exception):
            style_samples = style.quote_fewshot_lines(group_id_ref) or None

    messages = agents.build_reply_messages(
        cfg,
        timeline,
        memory_lines,
        history,
        user_content,
        nickname=ctx.sender_name(event),
        style_hint=style.style_hint(cfg),
        style_samples=style_samples,
    )

    # ---- 工具调用（agent 能力）：注册表有工具且配置开启时进入 function calling ----
    tools: list[dict[str, Any]] | None = None
    tool_ctx: ToolCallContext | None = None
    if ctx.get_cfg(cfg, "agent.enabled", False) and len(bot.tools) > 0:
        tools = bot.tools.openapi_schemas()
        tool_ctx = ToolCallContext(
            user_id=int(event.user_id or 0), group_id=group_id_ref
        )

    reply = await agents.call_replier(
        _session_ref(),
        api_base,
        api_key,
        model,
        cfg,
        messages,
        tools=tools,
        tool_ctx=tool_ctx,
        bot=bot,
    )

    # 历史只存占位（vision 片段不进历史），修剪逻辑与 classic 一致
    stored_question = (question or "[图片]")[:4000]
    _remember_turn(key, stored_question, reply, cfg)
    return reply


def _remember_turn(key: str, question: str, reply: str, cfg) -> None:
    history = _histories.get(key)
    if not history:
        history = [{"role": "system", "content": agents.persona_system_prompt(cfg)}]
    history.append({"role": "user", "content": question})
    history.append({"role": "assistant", "content": reply})
    max_history = max(0, min(int(cfg.get("max_history", 10)), 50))
    if len(history) > 1 + max_history * 2:
        history = [history[0], *history[-(max_history * 2) :]]
    _histories[key] = history
    while len(_histories) > _MAX_HISTORY_KEYS:
        _histories.pop(next(iter(_histories)), None)


def _maybe_extract_memory(event, cfg, user_text: str, reply_text: str) -> None:
    """记忆抽取：显式开启才落盘，60s 限流，后台异步不阻塞消息派发。"""
    if not ctx.get_cfg(cfg, "memory.enabled"):
        return
    user_text = (user_text or "").strip()
    reply_text = (reply_text or "").strip()
    if len(user_text) + len(reply_text) < 20:
        return
    now = time.monotonic()
    if now - _last_extract.get(event.user_id, 0.0) < _EXTRACT_INTERVAL:
        return
    _last_extract[event.user_id] = now

    async def _worker():
        api = _api_ready(cfg)
        if api is None:
            return
        api_base, api_key = api
        model = agents.resolve_model(cfg, "memory.extract_model", cfg.get("model") or "gpt-3.5-turbo")
        try:
            facts = await agents.extract_facts(
                _session_ref(), api_base, api_key, model, user_text, reply_text
            )
        except agents.AgentError:
            return
        group_id = getattr(event, "group_id", None)
        for fact in facts:
            with contextlib.suppress(Exception):
                memory_store.add_fact(
                    event.user_id, fact.get("kind", "fact"), fact.get("content", ""), group_id
                )
        max_per_user = _coerce_int(ctx.get_cfg(cfg, "memory.max_per_user", 100), 100, 10, 500)
        with contextlib.suppress(Exception):
            memory_store.enforce_max_per_user(event.user_id, max_per_user)
        retention = _coerce_int(ctx.get_cfg(cfg, "memory.retention_days", 90), 90, 0, 365)
        with contextlib.suppress(Exception):
            memory_store.lazy_daily_purge(retention)

    _spawn(_worker())


async def _review_group(group_id: int) -> None:
    """防抖触发的批量审查：规划器判定 → 分派给各专职智能体。

    在 ReviewDebouncer 的锁内执行；异常由防抖器兜底（清计数、保游标）。
    """
    bot = _bot
    if bot is None:
        return
    cfg = _cfg(bot)
    if not cfg.get("enabled", False) or str(cfg.get("mode", "agent")) != "agent":
        timeline = ctx._timelines.get(group_id)
        if timeline is not None:
            timeline.drop_pending()
        return
    if not ctx.get_cfg(cfg, "planner.enabled", True):
        timeline = ctx._timelines.get(group_id)
        if timeline is not None:
            timeline.drop_pending()
        return

    timeline = ctx._timelines.get(group_id)
    if timeline is None:
        return
    since_seq = timeline.last_review_seq
    api = _api_ready(cfg)
    if api is None:
        timeline.drop_pending()
        return
    api_base, api_key = api
    planner_model = agents.resolve_model(
        cfg, "planner.model", cfg.get("model") or "gpt-3.5-turbo"
    )
    proactive_mode = str(ctx.get_cfg(cfg, "proactive.mode", "event"))

    try:
        actions = await agents.call_planner(
            _session_ref(),
            api_base,
            api_key,
            planner_model,
            cfg,
            timeline,
            since_seq,
            _bot_name(bot),
            proactive_mode,
        )
    except agents.AgentError as e:
        bot.plugin_logger.warning(f"群{group_id} 规划器调用失败，本批忽略: {e}")
        timeline.drop_pending()
        return

    timeline.mark_reviewed()
    for action in actions:
        if action.type == "reply" and action.seq is not None:
            entry = _entry_by_seq(timeline, action.seq)
            if entry is not None and entry.event is not None:
                await _speak_to(bot, cfg, entry, timeline, action.reason)
        elif action.type == "see_image" and action.seq is not None:
            entry = _entry_by_seq(timeline, action.seq)
            if entry is not None and entry.event is not None:
                await _speak_to(
                    bot, cfg, entry, timeline, action.reason or "看看这张图", force_vision=True
                )
        elif action.type == "speak":
            await _maybe_speak_up(bot, cfg, group_id, timeline, action.reason)


def _entry_by_seq(timeline: ctx.GroupTimeline, seq: int) -> ctx.ListenEntry | None:
    for entry in timeline.entries_since(0):
        if entry.seq == seq:
            return entry
    return None


async def _speak_to(bot, cfg, entry: ctx.ListenEntry, timeline, reason: str, *, force_vision: bool = False) -> None:
    """对时间线上的一条消息（有原始事件）生成并发送回复。"""
    event = entry.event
    question = entry.text if entry.text != "[图片]" else ""
    try:
        reply = await _agent_reply(
            bot, cfg, event, question, timeline, reason, force_vision=force_vision
        )
    except agents.AgentError as e:
        bot.plugin_logger.warning(f"回复器调用失败（规划触发）: {e}")
        return
    if not reply:
        return
    group_id = getattr(event, "group_id", None)
    await style.send_like_human(bot, cfg, reply, group_id=group_id, user_id=event.user_id)
    _maybe_extract_memory(event, cfg, question, reply)


async def _maybe_speak_up(bot, cfg, group_id: int, timeline, reason: str) -> None:
    """规划器的 speak：主动插话。档位、静音时段、配额三重门控。"""
    if not style.speak_allowed(cfg) or style.in_quiet_hours(cfg):
        return
    if _bubble_quota.get(group_id, ("", 0))[1] >= BUBBLE_DAILY_MAX:
        return
    api = _api_ready(cfg)
    if api is None:
        return
    api_base, api_key = api
    model = cfg.get("model") or "gpt-3.5-turbo"
    excerpt = timeline.render(budget_chars=1200)
    if not excerpt:
        return
    messages = agents.build_reply_messages(
        cfg,
        timeline,
        [],
        [],
        f"（群里最近的聊天如下。基于话题自然地插一句短评，一两句话就好，"
        f"不要发问也不要 @ 任何人。方向：{reason or '自由发挥'}）\n{excerpt}",
        style_hint=style.style_hint(cfg),
    )
    try:
        reply = await agents.call_replier(_session_ref(), api_base, api_key, model, cfg, messages)
    except agents.AgentError as e:
        bot.plugin_logger.warning(f"主动插话生成失败: {e}")
        return
    if not reply:
        return
    today = date.today().isoformat()
    count = _bubble_quota.get(group_id, ("", 0))
    _bubble_quota[group_id] = (today, count[1] + 1 if count[0] == today else 1)
    bot.plugin_logger.info(f"群{group_id} 主动插话（今日第 {_bubble_quota[group_id][1]} 次）")
    await style.send_like_human(bot, cfg, reply, group_id=group_id, user_id=None)


async def _bubble_loop() -> None:
    """event+timer 档的低频冒泡：每 5 分钟看一眼各群，有话题且命中概率才说话。

    只在 event+timer 档生效；event 档不主动开话题（规划器 speak 也被门控）。
    """
    global _bubble_running
    try:
        while True:
            await asyncio.sleep(BUBBLE_TICK_SECONDS)
            if _bubble_running:
                continue
            bot = _bot
            if bot is None:
                continue
            cfg = _cfg(bot)
            if str(ctx.get_cfg(cfg, "proactive.mode", "event")) != "event+timer":
                continue
            if style.in_quiet_hours(cfg):
                continue
            now = time.time()
            for group_id, timeline in list(ctx._timelines.items()):
                recent = [e for e in timeline.entries_since(0) if now - e.ts < BUBBLE_RECENT_WINDOW]
                if len(recent) < 3:
                    continue
                if random.random() >= BUBBLE_CHANCE:
                    continue
                _bubble_running = True
                try:
                    await _maybe_speak_up(bot, cfg, group_id, timeline, "基于近期话题")
                finally:
                    _bubble_running = False
    except asyncio.CancelledError:
        raise


# ---------------------------------------------------------------------------
# 消息入口
# ---------------------------------------------------------------------------


async def on_message(event, bot):
    """Any 触发的统一入口：消息走对话管线，通知走反应管线。"""
    global _bot
    _bot = bot

    if not isinstance(event, MessageEvent):
        return await _handle_notice_event(event, bot)

    cfg = _cfg(bot)
    if not cfg.get("enabled", False):
        return False
    if not _check_permission(event, cfg):
        return False

    group_id = getattr(event, "group_id", None)
    agent_mode = str(cfg.get("mode", "agent")) == "agent"

    # ---- 旁听：先入时间线（采集规则在 _timeline_for / _listen_text 里）----
    timeline = _timeline_for(bot, cfg, event) if agent_mode else None
    if timeline is not None:
        text = _listen_text(bot, event)
        if text and not event.is_command and event.user_id != event.self_id:
            user_id: int = int(event.user_id or 0)
            timeline.append(user_id, ctx.sender_name(event), text, event=event)

    # ---- 触发判定（classic 与 agent 共用，保证两模式行为一致）----
    is_command = bool(event.is_command and event.command == "ai")
    direct = cfg.get("direct_chat", False) and (not group_id or _is_mentioning_bot(event))

    matched_nick = ""
    if group_id and _persona(cfg).get("enabled", True):
        nicknames = _persona_nicknames(cfg)
        if nicknames:
            text = _plain_text(bot, event)
            # 按长度降序匹配，避免短词吃掉长词
            for nick in sorted(nicknames, key=len, reverse=True):
                if text.startswith(nick):
                    matched_nick = nick
                    break

    # 唤醒词"居中被提及"：agent 档专属的概率接话（proactive.mode=event 及以上）
    chime_nick = ""
    if agent_mode and group_id and not (is_command or direct or matched_nick):
        nick = _nickname_mentioned_in_middle(bot, event, cfg)
        if nick and style.chime_allowed(group_id, cfg):
            chime_nick = nick

    if not (is_command or direct or matched_nick or chime_nick):
        # 没触发：这条消息只是旁听素材，排进防抖审查
        if timeline is not None:
            debouncer = _ensure_debouncer(cfg)
            debouncer.on_message(int(group_id or 0))
        return False

    # ---- $ai 忘了我：清除本人长期记忆 ----
    if is_command and (getattr(event, "args", "") or "").strip() in ("忘了我", "忘记我"):
        forget_uid: int = int(event.user_id or 0)
        if not ctx.get_cfg(cfg, "memory.enabled"):
            await bot.send("我还没有开启长期记忆哦", user_id=forget_uid, group_id=group_id)
            return True
        deleted = memory_store.forget_user(forget_uid)
        await bot.send(
            f"好的，已忘掉关于你的 {deleted} 条记忆" if deleted else "我本来就不记得你说过的事",
            user_id=forget_uid,
            group_id=group_id,
        )
        return True

    question = _extract_question(bot, event, getattr(event, "args", ""), cfg, matched_nick)
    if not question and not chime_nick:
        await bot.send("用法: $ai <问题>", group_id=group_id, user_id=event.user_id)
        return True

    # ---- 生成回复：agent 管线优先，失败按配置降级 classic ----
    reply: str | None = None
    if agent_mode:
        try:
            reply = await _agent_reply(
                bot, cfg, event, question or _plain_text(bot, event), timeline,
                "被直接点名",
            )
        except agents.AgentError as e:
            bot.plugin_logger.warning(f"agent 管线失败: {e}")
            reply = None
        if reply is None and ctx.get_cfg(cfg, "agent.fallback_classic", True):
            reply = await _ask_ai(bot, cfg, event, question or "")
    else:
        reply = await _ask_ai(bot, cfg, event, question)

    if not reply:
        return True

    # classic 保持原样直发；agent 走风格层（未开启时同样退化为直发）
    if agent_mode:
        await style.send_like_human(bot, cfg, reply, group_id=group_id, user_id=event.user_id)
        _maybe_extract_memory(event, cfg, question or "", reply)
    else:
        await bot.send(reply, group_id=group_id, user_id=event.user_id)
    return True
