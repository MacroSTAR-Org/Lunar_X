"""专职智能体：规划器 / 回复器 / 看图器 / 记忆抽取器。

每个角色都是一次 LLM 调用 + 各自的提示词，模型独立配置（留空回退主模型）：
- 规划器只要"判断要不要回"，便宜快的模型就够，没必要用主力模型烧钱；
- 回复器决定"说什么"，用配置里的主力模型；
- 看图器需要 vision 模型，且只在规划器标记 see_image 时才被调用；
- 记忆抽取在后台异步跑，用便宜模型。

所有函数在失败时抛 AgentError（带用户可读的信息），由 setup.py 决定
降级（agent.fallback_classic）还是忽略（旁听审查失败就忽略这批消息）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import aiohttp
from context import GroupTimeline, render_entries, time_context_line

# ---------------------------------------------------------------------------
# 通用调用层
# ---------------------------------------------------------------------------


class AgentError(RuntimeError):
    """LLM 调用失败（配置缺失 / 网络 / 接口报错），信息可直接展示给用户。"""


def resolve_model(cfg: dict[str, Any], role_key: str, default_model: str) -> str:
    """各角色独立模型配置，留空回退主模型。"""
    return str(cfg.get(role_key) or "").strip() or default_model


def _temperature(cfg: dict[str, Any], fallback: float = 0.7) -> float:
    try:
        return max(0.0, min(float(cfg.get("temperature", fallback)), 2.0))
    except (TypeError, ValueError):
        return fallback


def _max_tokens(cfg: dict[str, Any], fallback: int = 2048) -> int:
    try:
        return max(64, min(int(cfg.get("max_tokens", fallback)), 8192))
    except (TypeError, ValueError):
        return fallback


async def _post_chat(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    """一次 chat/completions 调用，返回原始 JSON。非 200 一律抛 AgentError。"""
    try:
        async with session.post(
            f"{api_base}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 401:
                raise AgentError("AI Key 无效（401），请检查配置")
            if resp.status == 429:
                raise AgentError("AI 请求过于频繁（429），请稍后再试")
            if resp.status != 200:
                raise AgentError(f"AI 接口错误（HTTP {resp.status}）")
            return await resp.json()
    except TimeoutError as e:
        raise AgentError("AI 响应超时，请稍后再试") from e
    except AgentError:
        raise
    except Exception as e:
        raise AgentError(f"AI 调用失败: {e}") from e


def _assistant_content(data: dict[str, Any]) -> str:
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except (KeyError, IndexError, TypeError) as e:
        raise AgentError("AI 返回格式异常，缺少回复内容") from e


def _tool_calls(data: dict[str, Any]) -> list[dict[str, Any]]:
    message = data.get("choices", [{}])[0].get("message", {})
    calls = message.get("tool_calls")
    return calls if isinstance(calls, list) else []


# ---------------------------------------------------------------------------
# 人格与 system 提示词
# ---------------------------------------------------------------------------

DEFAULT_PERSONA_PROMPT = "你是 Lunar X 的 AI 助手，请用中文简洁友好地回答问题。"


def persona_system_prompt(cfg: dict[str, Any]) -> str:
    """人格提示词三级优先：persona.system_prompt > system_prompt > 默认。"""
    persona = cfg.get("persona", {}) or {}
    return (
        str(persona.get("system_prompt") or "")
        or str(cfg.get("system_prompt") or "")
        or DEFAULT_PERSONA_PROMPT
    )


def build_reply_messages(
    cfg: dict[str, Any],
    timeline: GroupTimeline | None,
    memory_lines: list[str],
    history: list[dict[str, Any]],
    user_content: Any,
    *,
    nickname: str = "",
    style_hint: str = "",
    style_samples: list[str] | None = None,
) -> list[dict[str, Any]]:
    """组装回复器的 messages。

    层级：人格 → 当前时间 → 群旁听摘录 → 长期记忆 → 风格示例 → 个人历史 → 本条消息。
    旁听和记忆都是"背景参考"语气，避免模型把它们当命令复述。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": persona_system_prompt(cfg)}
    ]
    context_bits = [time_context_line()]
    if nickname:
        context_bits.append(f"当前和你说话的是：{nickname}。")
    if style_hint:
        context_bits.append(style_hint)
    messages.append({"role": "system", "content": " ".join(context_bits)})

    if timeline is not None:
        excerpt = timeline.render(budget_chars=int(cfg.get("listen.max_chars", 2500) or 2500))
        if excerpt:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "以下是本群近期的聊天记录，仅作背景参考；"
                        "用户接下来的话可能与它相关，也可能无关，不要主动复述记录内容。\n" + excerpt
                    ),
                }
            )

    if memory_lines:
        messages.append(
            {
                "role": "system",
                "content": (
                    "你长期记住的关于当前用户的背景信息（供理解上下文用，"
                    "不要主动罗列）：\n" + "\n".join(f"- {line}" for line in memory_lines)
                ),
            }
        )

    if style_samples:
        messages.append(
            {
                "role": "system",
                "content": (
                    "本群群友的说话风格示例（体会语气即可，不要复读原文）：\n"
                    + "\n".join(f"- {sample}" for sample in style_samples)
                ),
            }
        )

    messages.extend(history)
    messages.append({"role": "user", "content": user_content})
    return messages


# ---------------------------------------------------------------------------
# 规划器
# ---------------------------------------------------------------------------

PLANNER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "query_history",
        "description": "查询本群更早的聊天记录。当前只给了你最近一批消息，"
        "如果你觉得需要更早的上下文才能判断，就用它。",
        "parameters": {
            "type": "object",
            "properties": {
                "before_seq": {
                    "type": "integer",
                    "description": "返回 seq 小于该值的记录（传当前批次最早一条的 seq）",
                },
                "limit": {"type": "integer", "description": "最多返回几条，默认 20"},
            },
            "required": ["before_seq"],
        },
    },
}

PLANNER_SYSTEM_PROMPT = """你是群聊机器人「{bot_name}」的决策中枢。你会定期收到群里最近一批消息（每条带 seq 编号），你的任务是判断机器人要不要出面。

可输出的动作：
- {{"type": "reply", "seq": <编号>, "reason": "<为什么回>"}}   有人明确在和机器人说话：@它、叫它名字、向它提问、让它做事
- {{"type": "see_image", "seq": <编号>, "reason": "<为什么要看>"}}   某条带图片的消息需要理解内容才能决定怎么接话
- {{"type": "speak", "reason": "<想说什么方向>"}}   没人叫它，但当前话题它插一句会很自然（仅当主动档位允许）
- {{"type": "ignore"}}

判断原则（按优先级）：
1. 拿不准就 ignore。沉默是默认项，宁可错过也不要硬插话。
2. 有人被 @ 或被点名才 reply；群里普通聊天、别人互聊一律不打扰。
3. 消息里带 [图片] 且内容看起来和机器人有关（比如发了截图问它）才 see_image。
4. speak 极其克制：话题正好在它擅长的领域、且插话能加分时才用。
5. 需要更早的上下文时，先用 query_history 工具查询，再下结论。

输出要求：只输出一个 JSON 对象，格式如下，不要输出任何其他文字：
{{"actions": [ <动作对象>, ... ]}}
绝大多数时候它应该是 {{"actions": [{{"type": "ignore"}}]}}。"""


@dataclass
class PlannedAction:
    type: str  # reply | see_image | speak | ignore
    seq: int | None = None
    reason: str = ""


_PLANNER_MAX_TOOL_ROUNDS = 2


def parse_planner_output(text: str) -> list[PlannedAction]:
    """从模型输出里抠出 actions JSON；解析失败返回 [ignore]。

    模型偶尔会在 JSON 外面套一句解释，这里容忍前缀文本；
    但结构必须合法——规划器输出直接驱动分派，宁可忽略也不能瞎动。
    """
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return [PlannedAction(type="ignore", reason="规划器输出无法解析")]

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return [PlannedAction(type="ignore", reason="规划器输出不是合法 JSON")]

    actions_raw = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions_raw, list) or not actions_raw:
        return [PlannedAction(type="ignore")]

    valid_types = {"reply", "see_image", "speak", "ignore"}
    actions: list[PlannedAction] = []
    for item in actions_raw:
        if not isinstance(item, dict):
            continue
        action_type = str(item.get("type", "ignore")).strip()
        if action_type not in valid_types:
            continue
        seq = item.get("seq")
        try:
            seq_int = int(seq) if seq is not None else None
        except (TypeError, ValueError):
            seq_int = None  # 模型把 seq 写成"第3条"之类的文本：丢弃编号，动作仍有效
        actions.append(
            PlannedAction(
                type=action_type,
                seq=seq_int,
                reason=str(item.get("reason", ""))[:200],
            )
        )
    return actions or [PlannedAction(type="ignore")]


def _render_timeline_slice(timeline: GroupTimeline, since_seq: int, budget: int) -> str:
    """给规划器看的消息视图：since_seq<=0 时给整条时间线，否则只给增量。"""
    if since_seq <= 0:
        return timeline.render(budget_chars=budget)
    return render_entries(timeline.entries_since(since_seq), budget)


def _execute_planner_tool(
    timeline: GroupTimeline, name: str, arguments: str, budget: int
) -> str:
    """规划器只有一个只读工具：查更早的记录。解析失败返回空串让它自己兜底。"""
    if name != "query_history":
        return f"未知工具: {name}"
    try:
        args = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return "参数不是合法 JSON"
    try:
        before_seq = int(args.get("before_seq", 0))
    except (TypeError, ValueError):
        return "before_seq 必须是整数"
    try:
        limit = max(1, min(int(args.get("limit", 20)), 50))
    except (TypeError, ValueError):
        limit = 20

    entries = [e for e in timeline.entries_since(0) if e.seq < before_seq][-limit:]
    if not entries:
        return "（没有更早的记录了）"
    text = render_entries(entries, budget)
    return text if text else "（没有更早的记录了）"


async def call_planner(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    cfg: dict[str, Any],
    timeline: GroupTimeline,
    since_seq: int,
    bot_name: str,
    proactive_mode: str,
) -> list[PlannedAction]:
    """规划器：对增量消息给出动作判定。任何失败都由调用方按"忽略本批"处理。"""
    budget = int(cfg.get("planner.max_context_chars", 3000) or 3000)
    incremental = _render_timeline_slice(timeline, since_seq, budget)
    if not incremental.strip():
        return [PlannedAction(type="ignore")]

    system = (
        PLANNER_SYSTEM_PROMPT.format(bot_name=bot_name)
        + f"\n机器人名字：{bot_name}"
        + f"\n当前主动档位：{proactive_mode}（off=不允许 speak；event=仅在被间接点名时允许；"
        "event+timer=允许偶尔主动插话）"
    )
    user_text = (
        f"以下是 seq>{since_seq} 的新消息：\n{incremental}\n\n请给出 actions JSON。"
    )
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.2,  # 判定任务要稳定，不用回复器的高温度
        "max_tokens": 500,
        "tools": [PLANNER_TOOL_SCHEMA],
    }

    for _ in range(_PLANNER_MAX_TOOL_ROUNDS + 1):
        data = await _post_chat(session, api_base, api_key, payload, timeout=45)
        calls = _tool_calls(data)
        if not calls:
            return parse_planner_output(_assistant_content(data))

        # 模型还在查历史：执行工具，把结果喂回去再要一次最终结论
        assistant_message = data["choices"][0]["message"]
        messages.append(
            {
                "role": "assistant",
                "content": assistant_message.get("content") or "",
                "tool_calls": calls,
            }
        )
        for call in calls:
            fn = call.get("function", {})
            result = _execute_planner_tool(
                timeline, str(fn.get("name", "")), str(fn.get("arguments", "")), budget
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "content": result,
                }
            )
        payload["messages"] = messages

    # 工具轮次用尽还没给结论：按忽略处理
    return [PlannedAction(type="ignore", reason="规划器工具轮次用尽")]


# ---------------------------------------------------------------------------
# 回复器 / 看图器 / 记忆抽取
# ---------------------------------------------------------------------------


async def call_replier(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
) -> str:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": _temperature(cfg, 1.0),
        "max_tokens": _max_tokens(cfg),
    }
    data = await _post_chat(session, api_base, api_key, payload, timeout=90)
    return _assistant_content(data)


async def call_vision(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    image_parts: list[dict[str, Any]],
    question: str,
) -> str:
    """看图器：把图片（data URL）+ 问题交给 vision 模型，产出给回复器用的描述。"""
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": question
            or "请客观描述这张图片的内容：主体、文字、表情包梗、以及值得注意的细节。",
        },
        *image_parts,
    ]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
    }
    data = await _post_chat(session, api_base, api_key, payload, timeout=60)
    return _assistant_content(data)


EXTRACT_SYSTEM_PROMPT = """你负责从群聊机器人的对话片段里提取值得长期记住的"用户事实"。

规则：
- 只提取关于**用户本人**的稳定信息（身份/喜好/在做什么/重要事件），例如"在读研究生""养了只猫""下周要考试"。
- 不要提取：闲聊寒暄、时事讨论、你（机器人）说过的话、以及 3 天后就会过期的临时状态。
- 每条 fact 用一句独立、第三人称的中文短句（不超过 40 字），主语用"该用户"。
- 没有值得记的就输出 []。

只输出 JSON 数组，格式：[{"kind": "profile|fact|event", "content": "..."}]，不要输出其他文字。"""


async def extract_facts(
    session: aiohttp.ClientSession,
    api_base: str,
    api_key: str,
    model: str,
    user_text: str,
    assistant_text: str,
) -> list[dict[str, str]]:
    """记忆抽取：失败静默返回 []，由调用方吞掉——记忆是锦上添花，不能报错刷屏。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"用户说：{user_text[:600]}\n机器人回：{assistant_text[:600]}",
            },
        ],
        "temperature": 0.1,
        "max_tokens": 300,
    }
    try:
        data = await _post_chat(session, api_base, api_key, payload, timeout=30)
    except AgentError:
        return []

    text = _assistant_content(data)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        raw = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []

    valid_kinds = {"profile", "fact", "event"}
    facts: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "fact")).strip()
        content = str(item.get("content", "")).strip()[:120]
        if content and len(content) >= 4:
            facts.append({"kind": kind if kind in valid_kinds else "fact", "content": content})
    return facts[:3]


def strip_markdown_noise(text: str) -> str:
    """去掉聊天场景里最碍眼的两类 markdown 噪音：代码围栏和标题井号。

    QQ 不渲染 markdown，回复里出现 ``` 和 ## 会非常出戏。
    表格、加粗等复杂结构保留原样——强行转换更容易弄坏内容。
    """
    text = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return text.strip()
