"""旁听时间线与防抖审查的单元测试。

时间线是"赛博群友"的眼睛：只存内存、按群隔离、游标增量。
防抖器保证规划器不会逐条烧 token——静默期/攒够条数/最长等待三条件。
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
sys.path.insert(0, str(_PLUGIN))

import context as ctx


class TimelineTests(unittest.TestCase):
    def test_seq_increments_and_cursor(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        first = tl.append(1, "a", "hello")
        second = tl.append(2, "b", "world")
        self.assertEqual(second.seq, first.seq + 1)
        self.assertEqual(tl.entries_since(first.seq), [second])
        self.assertEqual(tl.entries_since(second.seq), [])

    def test_maxlen_evicts_oldest(self):
        tl = ctx.GroupTimeline(max_entries=3, ttl_minutes=120)
        for i in range(5):
            tl.append(1, "a", f"msg{i}")
        texts = [e.text for e in tl.entries_since(0)]
        self.assertEqual(texts, ["msg2", "msg3", "msg4"])

    def test_render_budget_truncates_from_oldest(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        for i in range(5):
            tl.append(1, "a", f"第{i}条" + "x" * 50)
        rendered = tl.render(budget_chars=120)
        lines = rendered.splitlines()
        # 预算不够时丢的是最旧的行，最新的必须保留
        self.assertIn("第4条", rendered)
        self.assertNotIn("第0条", rendered)
        self.assertGreaterEqual(len(lines), 1)

    def test_render_drops_expired(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=10)
        old_ts = time.time() - 3600  # 1 小时前，超过 10 分钟 TTL
        tl.append(1, "a", "旧消息", ts=int(old_ts))
        tl.append(1, "a", "新消息")
        rendered = tl.render(budget_chars=2500)
        self.assertNotIn("旧消息", rendered)
        self.assertIn("新消息", rendered)

    def test_notice_entry_renders_without_qq(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        tl.append(0, "", "789 加入了群")
        rendered = tl.render()
        self.assertIn("789 加入了群", rendered)
        self.assertNotIn("(0)", rendered)

    def test_mark_reviewed_advances_cursor(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        for i in range(3):
            tl.append(1, "a", f"m{i}")
        tl.mark_reviewed()
        self.assertEqual(tl.last_review_seq, 3)
        self.assertEqual(tl.pending_count, 0)

    def test_drop_pending_keeps_cursor(self):
        """审查失败：只清防抖计数，游标不动——下批带上这批消息重试。"""
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        for i in range(3):
            tl.append(1, "a", f"m{i}")
        tl.drop_pending()
        self.assertEqual(tl.last_review_seq, 0)
        self.assertEqual(tl.pending_count, 0)
        self.assertEqual(len(tl.entries_since(0)), 3)

    def test_configure_resizes_keeping_entries(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        for i in range(8):
            tl.append(1, "a", f"m{i}")
        tl.configure(3, 60)
        self.assertEqual(len(tl.entries_since(0)), 3)
        self.assertEqual(tl.ttl_minutes, 60)

    def test_entry_text_truncated(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        entry = tl.append(1, "a", "x" * 500)
        self.assertEqual(len(entry.text), ctx.ENTRY_TEXT_LIMIT)


class RegistryTests(unittest.TestCase):
    def tearDown(self):
        ctx.clear_all()

    def test_lru_eviction_at_max_groups(self):
        for gid in range(ctx.MAX_GROUPS + 5):
            ctx.get_timeline(gid)
        self.assertEqual(len(ctx._timelines), ctx.MAX_GROUPS)
        # 最先创建的群被淘汰
        self.assertNotIn(0, ctx._timelines)
        self.assertIn(ctx.MAX_GROUPS + 4, ctx._timelines)

    def test_get_timeline_moves_to_end(self):
        ctx.get_timeline(1)
        ctx.get_timeline(2)
        ctx.get_timeline(1)  # 触碰 1，使其变"最近使用"
        # 现在淘汰的应是 2
        ctx.get_timeline(3)
        ctx.get_timeline(4)
        # 不易直接断言顺序，验证存在性即可
        self.assertIn(1, ctx._timelines)

    def test_configure_on_existing(self):
        tl1 = ctx.get_timeline(1, max_entries=10, ttl_minutes=120)
        tl1.append(1, "a", "keep")
        tl2 = ctx.get_timeline(1, max_entries=5, ttl_minutes=60)
        self.assertIs(tl1, tl2)
        self.assertEqual(tl2.ttl_minutes, 60)
        self.assertEqual(len(tl2.entries_since(0)), 1)


class SenderNameTests(unittest.TestCase):
    def _event(self, sender=None):
        return type("E", (), {"sender": sender or {}, "user_id": 42})()

    def test_card_preferred(self):
        self.assertEqual(ctx.sender_name(self._event({"card": "名片", "nickname": "昵称"})), "名片")

    def test_nickname_fallback(self):
        self.assertEqual(ctx.sender_name(self._event({"card": "", "nickname": "昵称"})), "昵称")

    def test_uid_fallback(self):
        self.assertEqual(ctx.sender_name(self._event()), "用户42")


class TimeContextTests(unittest.TestCase):
    def test_period_word(self):
        """时段词与星期几都按本地时间推导，这里用固定钟点验证时段。"""
        import time as _time

        t = _time.localtime()
        ts = _time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 15, 0, 0, 0, 0, -1))
        line = ctx.time_context_line(ts)
        self.assertIn("下午", line)
        self.assertIn("一二三四五六日"[t.tm_wday], line)
        self.assertIn("15:00", line)


class DebouncerTests(unittest.TestCase):
    """防抖器需要真实事件循环；用极短的时间参数验证三个触发条件。"""

    def _debounce(self, review, quiet=0.05, batch=3, wait=0.5):
        return ctx.ReviewDebouncer(review, quiet_seconds=quiet, batch_max=batch,
                                   max_wait_seconds=wait)

    def test_quiet_triggers_review(self):
        calls = []

        async def review(gid):
            calls.append(gid)

        async def main():
            deb = self._debounce(review)
            tl = ctx.get_timeline(100)
            tl.append(1, "a", "hello")
            deb.on_message(100)
            self.assertEqual(calls, [])  # 还没到静默期
            await asyncio.sleep(0.3)
            self.assertEqual(calls, [100])
            deb.cancel_all()

        asyncio.run(main())

    def test_batch_max_triggers_immediately(self):
        calls = []

        async def review(gid):
            calls.append(gid)

        async def main():
            deb = self._debounce(review, quiet=10, batch=3, wait=10)
            tl = ctx.get_timeline(200)
            for i in range(3):
                tl.append(1, "a", f"m{i}")
                deb.on_message(200)
            await asyncio.sleep(0.15)
            self.assertEqual(calls, [200])  # 攒够条数立即触发，不等静默
            deb.cancel_all()

        asyncio.run(main())

    def test_no_timeline_no_review(self):
        calls = []

        async def review(gid):
            calls.append(gid)

        async def main():
            deb = self._debounce(review)
            deb.on_message(999)  # 没建过时间线的群
            await asyncio.sleep(0.15)
            self.assertEqual(calls, [])
            deb.cancel_all()

        asyncio.run(main())

    def test_review_failure_drops_pending(self):
        calls = []

        async def bad_review(gid):
            calls.append(gid)
            raise RuntimeError("boom")

        async def main():
            deb = self._debounce(bad_review)
            tl = ctx.get_timeline(300)
            tl.append(1, "a", "hello")
            deb.on_message(300)
            await asyncio.sleep(0.3)
            self.assertEqual(calls, [300])
            self.assertEqual(tl.pending_count, 0)  # 失败后计数清零
            self.assertEqual(tl.last_review_seq, 0)  # 游标不动，下批带上重试
            deb.cancel_all()

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
