"""事件反应：被戳回戳、撤回调侃、入群欢迎。

这些是"赛博群友"最像人的部分——群里发生事情它有反应，
而不是只有被 @ 才说话。安全阀：
- 每类反应独立开关（默认全关）；
- 每群每类冷却 ≥60s，防止连环刷屏；
- 撤回调侃**只引用内存时间线里的原文**，重启后无据可查就沉默，
  绝不为了调侃去查库——那会违反隐私分层的约定。
"""

from __future__ import annotations

import random
import time
from typing import Any

import context as ctx

REACT_COOLDOWN_SECONDS = 60

# 撤回调侃的概率：有梗才接，不是每条撤回都值得说
RECALL_TEASE_CHANCE = 0.6

POKE_TEXTS = ("戳我干嘛~", "别戳啦，痒", "再戳就要生气了哦", "戳戳回敬！")

RECALL_TEMPLATES = (
    "{name} 刚才撤回了「{text}」，让我看见了……",
    "哦？{name} 撤回了什么好东西",
    "{name} 手滑了吧，我都看到了~",
)

# 入群欢迎默认模板（plugin.json 里可通过 react.welcome_templates 覆盖）
DEFAULT_WELCOME_TEMPLATES = (
    "欢迎 {name} 入群～",
    "新朋友 {name} 来了，随便坐",
    "欢迎 {name}！记得看群公告哦",
)

# (反应类型, 群号) -> 上次触发时刻；跨热重载不保留（反应类状态丢了无所谓）
_last_fire: dict[tuple[str, int], float] = {}


def _cooldown_ok(kind: str, group_id: int) -> bool:
    now = time.monotonic()
    key = (kind, group_id)
    if now - _last_fire.get(key, 0.0) < REACT_COOLDOWN_SECONDS:
        return False
    _last_fire[key] = now
    return True


def clear_state() -> None:
    _last_fire.clear()


def _group_allowed(event: Any, cfg: dict) -> bool:
    """事件反应只受群白名单约束——allow_users 是对话权限，不该拦住"戳一下"。"""
    allowed_groups = cfg.get("allow_groups", [])
    group_id = getattr(event, "group_id", None)
    return not (group_id and allowed_groups and group_id not in allowed_groups)


def describe_notice(event: Any) -> str | None:
    """把通知事件压成一句话，作为时间线上的轻量条目（拟人化的环境感知）。"""
    if event.__class__.__name__ == "GroupPokeNoticeEvent":
        return f"{ctx.sender_name(event)} 戳了戳 {event.target_id}"
    if event.__class__.__name__ == "GroupIncreaseNoticeEvent":
        return f"{event.user_id} 加入了群"
    if event.__class__.__name__ == "GroupDecreaseNoticeEvent":
        return f"{event.user_id} 离开了群"
    if event.__class__.__name__ == "GroupRecallNoticeEvent":
        return f"{event.user_id} 撤回了一条消息"
    return None


async def handle_notice(
    event: Any, bot, cfg: dict, timeline: ctx.GroupTimeline | None
) -> bool:
    """处理通知事件，返回 True 表示发出了消息（已处理）。"""
    if not cfg.get("enabled", False):
        return False
    if not _group_allowed(event, cfg):
        return False

    cls_name = event.__class__.__name__
    group_id: int = getattr(event, "group_id", 0) or 0

    # ---- 被戳回戳 -------------------------------------------------------
    if cls_name == "GroupPokeNoticeEvent" and ctx.get_cfg(cfg, "react.poke_back"):
        if getattr(event, "target_id", None) != getattr(event, "self_id", None):
            return False
        if not _cooldown_ok("poke", group_id):
            return False
        # 协议端的戳一戳 action 名不统一（Milky 映射 + go-cqhttp 扩展），
        # 逐个尝试，全失败就退化为文字回应——戳都戳了，总得理一下
        for action in ("send_group_poke", "group_poke"):
            try:
                await getattr(bot.diy, action)(group_id=group_id, user_id=event.user_id)
                return True
            except Exception:
                continue
        await bot.send(random.choice(POKE_TEXTS), group_id=group_id)
        return True

    # ---- 撤回调侃 -------------------------------------------------------
    if cls_name == "GroupRecallNoticeEvent" and ctx.get_cfg(cfg, "react.recall_tease"):
        if timeline is None:
            return False  # 旁听没开就无从知道原文，保持沉默
        if random.random() >= RECALL_TEASE_CHANCE:
            return False
        if not _cooldown_ok("recall", group_id):
            return False
        # 只从内存时间线找原文：找不到（重启过/没旁听）就沉默
        original = None
        for entry in reversed(timeline.entries_since(0)):
            if entry.user_id == event.user_id and entry.text and entry.text != "[图片]":
                original = entry
                break
        if original is None:
            return False
        name = original.name
        template = random.choice(RECALL_TEMPLATES)
        await bot.send(
            template.format(name=name, text=original.text[:30]), group_id=group_id
        )
        return True

    # ---- 入群欢迎 -------------------------------------------------------
    if cls_name == "GroupIncreaseNoticeEvent" and ctx.get_cfg(cfg, "react.welcome"):
        if not _cooldown_ok("welcome", group_id):
            return False
        templates = ctx.get_cfg(cfg, "react.welcome_templates") or []
        templates = [t for t in templates if t and t.strip()] or list(DEFAULT_WELCOME_TEMPLATES)
        name = f"用户{event.user_id}"
        raw = getattr(event, "raw_data", {}) or {}
        sender = raw.get("sender") or {}
        nickname = str(sender.get("nickname") or "").strip()
        if nickname:
            name = nickname
        await bot.send(random.choice(templates).format(name=name), group_id=group_id)
        return True

    return False
