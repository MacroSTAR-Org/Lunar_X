"""拟人化风格层：让回复"像人打的字"，而不是"模型输出的文档"。

QQ 不渲染 markdown，模型输出里的 ``` 和 ## 会非常出戏；
一次性甩出 500 字长文也完全不像群友说话。这里做三件事：
打字延迟、按标点分段发送、偶尔带个表情。
全部默认关闭（style.enabled），逐项可调。
"""

from __future__ import annotations

import asyncio
import random
import re
import time

# 复用 agents 的 markdown 清理，语义上同属"把模型腔调成群友腔"
from agents import strip_markdown_noise

# 一批常用、语义中性的 QQ 小黄脸，随回复随机带上
FACE_IDS = (14, 21, 23, 28, 32, 49, 66, 74, 76, 78, 85, 97)

MAX_SEGMENTS = 5

# 唤醒词"居中被提及"接话的冷却（每群），避免同一个话题被反复插嘴
CHIME_COOLDOWN_SECONDS = 600


def typing_delay_seconds(text: str) -> float:
    """模拟打字时间：基础 1s + 每 16 字 0.4s，封顶 6s。

    真人回 300 字也要打好一会儿；秒回长文一眼假。
    """
    return random.uniform(0.8, 1.2) + min(len(text) * 0.4 / 16, 5.0)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """没有标点可切时的兜底：按长度硬切。"""
    return [text[i : i + max_chars] for i in range(0, len(text), max_chars)]


def split_reply(text: str, max_chars: int = 80) -> list[str]:
    """把回复切成自然句段：先按句末标点切，超长段再按逗号切。

    切完做两步整理：太短的碎片并入相邻段（避免"嗯"单独成条）；
    超过 MAX_SEGMENTS 时把剩余内容并回最后一段。
    """
    text = text.strip()
    if not text:
        return []

    sentences = [s for s in re.split(r"(?<=[。！？!?~…；;\n])", text) if s.strip()]
    pieces: list[str] = []
    for sentence in sentences:
        sentence = sentence.strip()
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        # 长句按逗号切，切完仍超长的按长度硬切
        sub = [s.strip() for s in re.split(r"(?<=[，,、])", sentence) if s.strip()]
        for part in sub:
            pieces.extend(_hard_split(part, max_chars) if len(part) > max_chars else [part])

    # 短碎片并入前一段（阈值 2：只剩标点残渣才合并，正常短句保留）
    merged: list[str] = []
    for piece in pieces:
        if merged and len(piece) <= 2:
            merged[-1] += piece
        else:
            merged.append(piece)

    # 段数上限：剩余内容并回最后一段
    if len(merged) > MAX_SEGMENTS:
        head, tail = merged[: MAX_SEGMENTS - 1], merged[MAX_SEGMENTS - 1 :]
        merged = [*head, "".join(tail)]
    return [m for m in merged if m]


def style_hint(cfg: dict) -> str:
    """注入 system 的说话方式约束——风格层开启时让模型直接说人话。"""
    if not cfg.get("style.enabled"):
        return ""
    return (
        "你的回复会直接发在 QQ 群里：像群友聊天一样口语化，"
        "一次别超过 80 字，不要用列表、标题和 markdown 格式，"
        "不确定的信息就别编。"
    )


def pick_face(cfg: dict) -> dict | None:
    """按 face_chance 概率返回一个表情段 dict；未命中返回 None。"""
    try:
        chance = float(cfg.get("style.face_chance", 0) or 0)
    except (TypeError, ValueError):
        chance = 0.0
    if chance <= 0 or random.random() >= chance:
        return None
    return {"type": "face", "data": {"id": str(random.choice(FACE_IDS))}}


async def send_like_human(bot, cfg: dict, text: str, *, group_id: int | None, user_id: int | None) -> None:
    """按风格层配置发送回复；未开启时退化为单条直发（现状行为）。"""
    text = strip_markdown_noise(text)
    if not text:
        return

    if not cfg.get("style.enabled"):
        await bot.send(text, group_id=group_id, user_id=user_id)
        return

    is_group = group_id is not None
    segments = split_reply(text, max_chars=int(cfg.get("style.max_segment_chars", 80) or 80))

    if is_group:
        await asyncio.sleep(typing_delay_seconds(text))

    face = pick_face(cfg)
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if last and face is not None:
            await bot.send([segment, face], group_id=group_id, user_id=user_id)
        else:
            await bot.send(segment, group_id=group_id, user_id=user_id)
        if not last and is_group:
            await asyncio.sleep(random.uniform(0.5, 2.0))


# ---------------------------------------------------------------------------
# 主动行为档位
# ---------------------------------------------------------------------------


def speak_allowed(cfg: dict) -> bool:
    """规划器的 speak（主动开话题）只在高档位放行。

    event 档的定位是"事件反应 + 被间接点名时接话"，不主动开话题；
    真正主动插话是 event+timer 档的特权。
    """
    return str(cfg.get("proactive.mode", "event")) == "event+timer"


def in_quiet_hours(cfg: dict, now: float | None = None) -> bool:
    """静音时段判定，配置形如 "01:00-08:00"；解析失败视为不在静音期。"""
    raw = str(cfg.get("proactive.quiet_hours", "01:00-08:00") or "").strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})", raw)
    if not match:
        return False
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    t = time.localtime(now if now is not None else time.time())
    minute_of_day = t.tm_hour * 60 + t.tm_min
    if start <= end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end  # 跨午夜区间


def quote_fewshot_lines(group_id: int, limit: int = 4) -> list[str]:
    """从群语录库随机取几条作为"本群说话风格"示例。

    只读直连 data/quote/quotes.db（SQLite URI 只读模式），不改语录库的任何数据；
    跨插件 import 不可靠，但文件路径是稳定的。任何失败都静默返回空——
    这是锦上添花的功能，不能因为它报错影响对话。
    """
    import sqlite3
    from pathlib import Path

    db = Path(__file__).resolve().parent.parent.parent / "data" / "quote" / "quotes.db"
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT content FROM quotes WHERE group_id = ? "
                "ORDER BY RANDOM() LIMIT ?",
                (group_id, max(1, limit)),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return [str(row[0]).strip()[:80] for row in rows if row and str(row[0]).strip()]


_last_chime: dict[int, float] = {}


def chime_allowed(group_id: int, cfg: dict) -> bool:
    """唤醒词"居中被提及"时的概率接话，受概率与每群冷却双重约束。"""
    try:
        chance = float(cfg.get("proactive.chime_chance", 0.3) or 0)
    except (TypeError, ValueError):
        chance = 0.3
    if chance <= 0:
        return False
    now = time.monotonic()
    if now - _last_chime.get(group_id, 0.0) < CHIME_COOLDOWN_SECONDS:
        return False
    if random.random() >= chance:
        # 没命中概率也要刷新冷却，避免连续每条消息都掷骰子
        _last_chime[group_id] = now - CHIME_COOLDOWN_SECONDS + 30.0
        return False
    _last_chime[group_id] = now
    return True
