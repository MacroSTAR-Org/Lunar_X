"""群聊旁听时间线与防抖审查调度（纯内存，绝不落盘）。

设计要点：
- 每群一条共享时间线（群视角），机器人"旁听"群里在聊什么；
  采集不花任何 LLM 成本，成本只发生在防抖触发后的批量审查。
- 时间线只存内存：重启即清，不落盘、不导出，这是隐私分层约束的一半
  （另一半是长期记忆必须显式开启才写库）。
- 游标 last_review_seq 让规划器每次只看"上次审查之后的增量"，
  不需要把整个聊天记录塞给它。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# 群时间线的全局上限：超出按 LRU 淘汰最久没说话的群。
# 防止机器人在几千个群里把内存吃穿。
MAX_GROUPS = 50

# 单条消息文本截断：旁听是给 LLM 看的背景，太长只会烧 token
ENTRY_TEXT_LIMIT = 200


@dataclass
class ListenEntry:
    """时间线上的一条记录。user_id=0 表示非消息事件（戳一戳/入退群等）。

    event 保存原始事件对象的引用（仅内存）：规划器标记 see_image 时，
    回复器需要拿到消息里的图片段，而时间线文本里只有"[图片]"占位。
    """

    seq: int
    ts: int
    user_id: int
    name: str
    text: str
    event: Any = field(default=None, repr=False)


def _hhmm(ts: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


def render_entries(entries: list[ListenEntry], budget_chars: int = 2500) -> str:
    """把若干条记录渲染成给 LLM 看的文本；从最新往回取，超预算截断。

    时间线和规划器的增量视图共用这一份格式，保证两边看到的世界一致。
    """
    lines: list[str] = []
    used = 0
    for entry in reversed(entries):
        if entry.user_id:
            line = f"[{_hhmm(entry.ts)}] {entry.name}({entry.user_id}): {entry.text}"
        else:
            line = f"[{_hhmm(entry.ts)}] {entry.text}"
        if used + len(line) > budget_chars and lines:
            break
        lines.append(line)
        used += len(line)
    lines.reverse()
    return "\n".join(lines)


@dataclass
class GroupTimeline:
    """单个群的旁听时间线。"""

    max_entries: int = 30
    ttl_minutes: int = 120
    _entries: deque = field(default_factory=deque, init=False)
    _next_seq: int = field(default=1, init=False)
    # 审查游标：规划器只看 last_review_seq 之后的增量
    last_review_seq: int = field(default=0, init=False)
    # 防抖状态：本轮（距上次审查）累计的新消息数 / 首条消息的单调时钟
    pending_count: int = field(default=0, init=False)
    first_pending_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._entries = deque(maxlen=self.max_entries)

    def configure(self, max_entries: int, ttl_minutes: int) -> None:
        """配置热更新：容量变化时重建 deque 并保留最近的消息。"""
        self.ttl_minutes = ttl_minutes
        if max_entries == self.max_entries:
            return
        self.max_entries = max_entries
        items = list(self._entries)[-max_entries:]
        self._entries = deque(items, maxlen=max_entries)

    @property
    def next_seq(self) -> int:
        return self._next_seq

    def append(
        self,
        user_id: int,
        name: str,
        text: str,
        ts: int | None = None,
        event: Any = None,
    ) -> ListenEntry:
        entry = ListenEntry(
            seq=self._next_seq,
            ts=int(ts if ts is not None else time.time()),
            user_id=int(user_id),
            name=name,
            text=text[:ENTRY_TEXT_LIMIT],
            event=event,
        )
        self._next_seq += 1
        self._entries.append(entry)
        self.pending_count += 1
        if self.first_pending_at is None:
            self.first_pending_at = time.monotonic()
        return entry

    def entries_since(self, seq: int) -> list[ListenEntry]:
        return [e for e in self._entries if e.seq > seq]

    def _expired(self, entry: ListenEntry, now: int) -> bool:
        return entry.ts < now - self.ttl_minutes * 60

    def render(self, budget_chars: int = 2500, now: int | None = None) -> str:
        """把时间线渲染成给 LLM 看的文本（过期条目不计入）。"""
        now = int(now if now is not None else time.time())
        fresh = [e for e in self._entries if not self._expired(e, now)]
        # 找到最早的未过期条目之后全部交给公共渲染器做预算截断
        return render_entries(fresh, budget_chars)

    def mark_reviewed(self) -> None:
        """审查完成：游标推进到最新，防抖计数清零。"""
        if self._entries:
            self.last_review_seq = self._entries[-1].seq
        self.pending_count = 0
        self.first_pending_at = None

    def drop_pending(self) -> None:
        """审查失败时只清防抖计数，游标不推进——下次审查会带上这批消息重试。"""
        self.pending_count = 0
        self.first_pending_at = None

    def clear(self) -> None:
        self._entries.clear()
        self.last_review_seq = 0
        self.pending_count = 0
        self.first_pending_at = None


# ---------------------------------------------------------------------------
# 群注册表（LRU）
# ---------------------------------------------------------------------------

_timelines: OrderedDict[int, GroupTimeline] = OrderedDict()


def get_timeline(group_id: int, max_entries: int = 30, ttl_minutes: int = 120) -> GroupTimeline:
    """取（或建）一个群的时间线；参数热更新时在原对象上调整，游标不丢。"""
    timeline = _timelines.get(group_id)
    if timeline is None:
        if len(_timelines) >= MAX_GROUPS:
            _timelines.pop(next(iter(_timelines)))
        timeline = GroupTimeline(max_entries=max_entries, ttl_minutes=ttl_minutes)
        _timelines[group_id] = timeline
    else:
        _timelines.move_to_end(group_id)
        timeline.configure(max_entries, ttl_minutes)
    return timeline


def clear_all() -> None:
    """热重载 / 退出时清空全部内存状态。"""
    _timelines.clear()


def sender_name(event: Any) -> str:
    """群名片 > 昵称 > 用户ID，与 core/events.py 的日志逻辑保持一致。"""
    sender = getattr(event, "sender", None) or {}
    name = str(sender.get("card") or sender.get("nickname") or "").strip()
    return name or f"用户{getattr(event, 'user_id', 0)}"


def time_context_line(now: float | None = None) -> str:
    """当前时间的口语化描述，注入人格让回复带"现在几点"的实感。"""
    t = time.localtime(now if now is not None else time.time())
    weekday = "一二三四五六日"[t.tm_wday]
    hour = t.tm_hour
    if 5 <= hour < 8:
        period = "清晨"
    elif 8 <= hour < 12:
        period = "上午"
    elif 12 <= hour < 14:
        period = "中午"
    elif 14 <= hour < 18:
        period = "下午"
    elif 18 <= hour < 23:
        period = "晚上"
    else:
        period = "深夜"
    return f"现在是周{weekday}{period} {t.tm_hour:02d}:{t.tm_min:02d}。"


# ---------------------------------------------------------------------------
# 防抖审查调度
# ---------------------------------------------------------------------------


class ReviewDebouncer:
    """按「静默期 / 攒够条数 / 最长等待」三条件调度批量审查。

    用户明确否决了"每条消息都送规划器"——群里热闹时那全是 token。
    这里用 asyncio 定时任务实现防抖：消息进来刷新静默计时器，
    攒够条数立即触发，一直热闹则由最长等待兜底。
    """

    def __init__(
        self,
        review: Callable[[int], Awaitable[None]],
        quiet_seconds: int = 20,
        batch_max: int = 12,
        max_wait_seconds: int = 120,
    ):
        self._review = review
        self.quiet_seconds = quiet_seconds
        self.batch_max = batch_max
        self.max_wait_seconds = max_wait_seconds
        self._quiet_handles: dict[int, asyncio.TimerHandle] = {}
        self._watchdogs: dict[int, asyncio.Task] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        # 后台审查任务的强引用，防 create_task 的结果被 GC 半路回收
        self._running: set[asyncio.Task] = set()

    # -- 生命周期 -----------------------------------------------------------

    def cancel_all(self) -> None:
        for handle in self._quiet_handles.values():
            handle.cancel()
        for task in self._watchdogs.values():
            task.cancel()
        for task in self._running:
            task.cancel()
        self._quiet_handles.clear()
        self._watchdogs.clear()
        self._running.clear()

    def _lock(self, group_id: int) -> asyncio.Lock:
        lock = self._locks.get(group_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[group_id] = lock
        return lock

    # -- 触发路径 -----------------------------------------------------------

    def on_message(self, group_id: int, *, force_check: bool = False) -> None:
        """每条群消息调用一次。force_check 供测试与手动触发用。"""
        timeline = _timelines.get(group_id)
        if timeline is None:
            return

        # 攒够条数：立即审查，不再等静默
        if force_check or timeline.pending_count >= self.batch_max:
            self._schedule(group_id)
            return

        # 静默期：刷新计时器（取消旧的，重计 quiet_seconds）
        self._reschedule_quiet(group_id)
        self._ensure_watchdog(group_id, timeline)

    def _reschedule_quiet(self, group_id: int) -> None:
        old = self._quiet_handles.get(group_id)
        if old is not None:
            old.cancel()
        loop = asyncio.get_running_loop()
        self._quiet_handles[group_id] = loop.call_later(
            self.quiet_seconds, self._schedule, group_id
        )

    def _ensure_watchdog(self, group_id: int, timeline: GroupTimeline) -> None:
        """最长等待兜底：群里一直热闹永不安静时，保证审查仍会发生。"""
        if timeline.first_pending_at is None:
            return
        if group_id in self._watchdogs and not self._watchdogs[group_id].done():
            return
        elapsed = time.monotonic() - timeline.first_pending_at
        remaining = max(0.0, self.max_wait_seconds - elapsed)
        self._watchdogs[group_id] = asyncio.create_task(self._watchdog(group_id, remaining))

    async def _watchdog(self, group_id: int, delay: float) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            self._schedule(group_id)
        except asyncio.CancelledError:
            pass

    def _schedule(self, group_id: int) -> None:
        """真正发起一次审查；同一群已在审查中则跳过（锁内幂等）。"""
        handle = self._quiet_handles.pop(group_id, None)
        if handle is not None:
            handle.cancel()
        watchdog = self._watchdogs.pop(group_id, None)
        if watchdog is not None and not watchdog.done():
            watchdog.cancel()
        task = asyncio.create_task(self._run(group_id))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(self, group_id: int) -> None:
        lock = self._lock(group_id)
        if lock.locked():
            return  # 已有审查在跑，本次消息留给下一轮
        async with lock:
            try:
                await self._review(group_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 审查失败不能拖垮消息派发
                import logging

                logging.getLogger("LunarPlugins").warning(f"群{group_id} 规划审查失败: {e}")
                timeline = _timelines.get(group_id)
                if timeline is not None:
                    timeline.drop_pending()
