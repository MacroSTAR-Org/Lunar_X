"""ai_chat 非阻塞回归测试。

框架的插件分发是串行的，LLM 调用和拟人打字延迟必须在后台任务里完成，
否则一个慢模型会卡住整个 Bot（管理命令、定时任务、其他群全部排队）。
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"))

import setup as ai_setup

from core.events import GroupMessageEvent
from core.message import TextSegment


class _Logger:
    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass


class FakeBot:
    def __init__(self, cfg):
        self.cfg = cfg
        self.plugin_logger = _Logger()
        self.sent = []

    def plugin_config(self, name):
        return self.cfg

    async def send(self, message, user_id=None, group_id=None):
        self.sent.append((message, group_id, user_id))


def _group_event(text="hello", user_id=456, group_id=123):
    event = GroupMessageEvent(
        {
            "post_type": "message",
            "message_type": "group",
            "group_id": group_id,
            "user_id": user_id,
            "self_id": 999,
            "raw_message": text,
        }
    )
    event.message = [TextSegment(text)]
    event.is_command = True
    event.command = "ai"
    event.args = text
    return event


def _classic_cfg():
    return {
        "enabled": True,
        "mode": "classic",
        "allow_users": [],
        "allow_groups": [],
        "direct_chat": False,
        "api_base": "https://example.invalid",
        "api_key": "test",
        "model": "test",
    }


class NonBlockingReplyTests(unittest.TestCase):
    def setUp(self):
        self._original_ask_ai = ai_setup._ask_ai
        ai_setup._reply_locks.clear()
        ai_setup._reply_pending.clear()
        ai_setup._background_tasks.clear()

    def tearDown(self):
        ai_setup._ask_ai = self._original_ask_ai
        ai_setup._reply_locks.clear()
        ai_setup._reply_pending.clear()
        ai_setup._background_tasks.clear()

    def test_on_message_returns_before_llm_finishes(self):
        async def scenario():
            async def slow_ask(bot, cfg, event, question):
                await asyncio.sleep(0.2)
                return "后台回答"

            ai_setup._ask_ai = slow_ask
            bot = FakeBot(_classic_cfg())

            started = time.monotonic()
            handled = await ai_setup.on_message(_group_event(), bot)
            elapsed = time.monotonic() - started

            self.assertTrue(handled)
            self.assertLess(elapsed, 0.1, "on_message 不应等待 LLM 返回")

            tasks = list(ai_setup._background_tasks)
            self.assertTrue(tasks, "回复应交给后台任务")
            await asyncio.gather(*tasks)
            self.assertEqual(bot.sent, [("后台回答", 123, 456)])

        asyncio.run(scenario())

    def test_same_group_replies_are_serialized(self):
        async def scenario():
            active = 0
            max_active = 0

            async def tracked_ask(bot, cfg, event, question):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                try:
                    await asyncio.sleep(0.05)
                    return f"回答:{question}"
                finally:
                    active -= 1

            ai_setup._ask_ai = tracked_ask
            bot = FakeBot(_classic_cfg())

            await ai_setup.on_message(_group_event("一"), bot)
            await ai_setup.on_message(_group_event("二"), bot)

            tasks = list(ai_setup._background_tasks)
            await asyncio.gather(*tasks)

            self.assertEqual(max_active, 1, "同一会话的回复必须串行，避免插话乱序")
            self.assertEqual(len(bot.sent), 2)

        asyncio.run(scenario())

    def test_pending_replies_are_capped_per_conversation(self):
        """慢模型期间同一会话的待回复任务不能无限增长。"""

        async def scenario():
            async def slow_ask(bot, cfg, event, question):
                await asyncio.sleep(0.2)
                return f"回答:{question}"

            ai_setup._ask_ai = slow_ask
            cfg = _classic_cfg()
            cfg["agent.max_pending_replies"] = 2
            bot = FakeBot(cfg)

            for index in range(4):
                await ai_setup.on_message(_group_event(f"问题{index}"), bot)

            key = "group:123"
            self.assertEqual(ai_setup._reply_pending.get(key), 2)
            self.assertEqual(len(ai_setup._background_tasks), 2)

            await asyncio.gather(*list(ai_setup._background_tasks))
            self.assertEqual(len(bot.sent), 2, "超过上限的消息应直接丢弃")
            self.assertNotIn(key, ai_setup._reply_pending)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
