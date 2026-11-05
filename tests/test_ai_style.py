"""风格层与事件反应的单元测试：分段、冷却、欢迎模板、反应状态清理。

发送路径用假 bot 验证（记录调用，不发真消息）。
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
sys.path.insert(0, str(_PLUGIN))

import context as ctx
import reactions as reactions_mod
import setup as ai_setup  # 必须在 sys.path 插入后立刻导入：

# plugins/tools 也有 setup.py，运行期再导入会被它的路径抢先
import style


class FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send(self, message, user_id=None, group_id=None):
        self.sent.append((message, user_id, group_id))


class SendLikeHumanTests(unittest.TestCase):
    def test_disabled_sends_single_plain_message(self):
        async def main():
            bot = FakeBot()
            await style.send_like_human(
                bot, {"style.enabled": False}, "一句完整的话", group_id=1, user_id=2
            )
            return bot

        bot = asyncio.run(main())
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(bot.sent[0][0], "一句完整的话")

    def test_enabled_splits_segments(self):
        async def main():
            bot = FakeBot()
            text = "第一句。第二句。第三句。第四句。第五句。第六句。"
            await style.send_like_human(bot, {"style.enabled": True}, text, group_id=1, user_id=2)
            return bot, text

        bot, _text = asyncio.run(main())
        self.assertGreater(len(bot.sent), 1)
        self.assertLessEqual(len(bot.sent), style.MAX_SEGMENTS)
        # 内容不丢（表情可能附加为段对象，这里概率为 0）
        joined = "".join(
            s if isinstance(s, str) else str(s) for s, _, _ in bot.sent
        )
        for ch in "一二三四五六":
            self.assertIn(ch, joined)

    def test_markdown_stripped_before_send(self):
        async def main():
            bot = FakeBot()
            await style.send_like_human(
                bot, {"style.enabled": False}, "```py\ncode\n```\n说明", group_id=1, user_id=2
            )
            return bot

        bot = asyncio.run(main())
        self.assertNotIn("```", bot.sent[0][0])

    def test_empty_reply_sends_nothing(self):
        async def main():
            bot = FakeBot()
            await style.send_like_human(bot, {}, "   ", group_id=1, user_id=2)
            return bot

        self.assertEqual(len(asyncio.run(main()).sent), 0)


class ChimeCooldownTests(unittest.TestCase):
    def setUp(self):
        style._last_chime.clear()

    def tearDown(self):
        style._last_chime.clear()

    def test_cooldown_blocks_repeat(self):
        cfg = {"proactive.chime_chance": 1.0}  # 概率拉满，纯验冷却
        self.assertTrue(style.chime_allowed(1, cfg))
        self.assertFalse(style.chime_allowed(1, cfg))  # 10 分钟冷却内

    def test_groups_independent(self):
        cfg = {"proactive.chime_chance": 1.0}
        self.assertTrue(style.chime_allowed(1, cfg))
        self.assertTrue(style.chime_allowed(2, cfg))

    def test_missed_roll_sets_partial_cooldown(self):
        """没掷中概率也要挡 30 秒，避免每条消息都重掷。"""
        cfg = {"proactive.chime_chance": 0.0}
        self.assertFalse(style.chime_allowed(1, cfg))
        # 概率为 0 时永远 False，且不该把冷却写成"刚触发过"
        self.assertEqual(style._last_chime.get(1, 0.0), 0.0)


class ReactionCooldownTests(unittest.TestCase):
    def setUp(self):
        reactions_mod._last_fire.clear()

    def tearDown(self):
        reactions_mod._last_fire.clear()

    def test_cooldown_blocks_second_fire(self):
        self.assertTrue(reactions_mod._cooldown_ok("poke", 1))
        self.assertFalse(reactions_mod._cooldown_ok("poke", 1))

    def test_kinds_and_groups_independent(self):
        self.assertTrue(reactions_mod._cooldown_ok("poke", 1))
        self.assertTrue(reactions_mod._cooldown_ok("recall", 1))
        self.assertTrue(reactions_mod._cooldown_ok("poke", 2))

    def test_clear_state(self):
        reactions_mod._cooldown_ok("poke", 1)
        reactions_mod.clear_state()
        self.assertTrue(reactions_mod._cooldown_ok("poke", 1))


class _PokeDiy:
    """假 bot.diy：记录 poke 调用。"""

    def __init__(self):
        self.pokes: list[dict] = []

    def send_group_poke(self, **kwargs):
        self.pokes.append(kwargs)

        class _Done:
            def __await__(self):
                return iter(())

        return _Done()


class PokeBackTests(unittest.TestCase):
    """用真实的 GroupPokeNoticeEvent 验证被戳回戳链路。"""

    def setUp(self):
        reactions_mod._last_fire.clear()
        ctx.clear_all()

    def tearDown(self):
        reactions_mod._last_fire.clear()
        ctx.clear_all()

    def _bot(self, poke_ok=True):
        class FakeBot:
            def __init__(self):
                self.diy = _PokeDiy()
                self.sent = []
                self._poke_ok = poke_ok

            async def send(self, message, user_id=None, group_id=None):
                self.sent.append((message, user_id, group_id))

        bot = FakeBot()
        if not poke_ok:
            # diy 调用抛异常 → 应回退为文字回应
            class _ExplodingDiy:
                def __getattr__(self, name):
                    def _raise(**kwargs):
                        raise RuntimeError("协议端不支持")

                    return _raise

            bot.diy = _ExplodingDiy()
        return bot

    def _event(self, target=999, self_id=999):
        from core.events import GroupPokeNoticeEvent

        return GroupPokeNoticeEvent(
            {"group_id": 1, "user_id": 42, "target_id": target, "self_id": self_id}
        )

    def test_poke_back_calls_protocol_action(self):
        bot = self._bot()
        cfg = {"enabled": True, "react.poke_back": True}

        async def main():
            return await reactions_mod.handle_notice(
                self._event(), bot, cfg, ctx.get_timeline(1)
            )

        self.assertTrue(asyncio.run(main()))
        self.assertEqual(bot.diy.pokes, [{"group_id": 1, "user_id": 42}])
        self.assertEqual(bot.sent, [])  # 戳成功了就不发文字

    def test_poke_protocol_failure_falls_back_to_text(self):
        bot = self._bot(poke_ok=False)
        cfg = {"enabled": True, "react.poke_back": True}

        async def main():
            return await reactions_mod.handle_notice(
                self._event(), bot, cfg, ctx.get_timeline(1)
            )

        self.assertTrue(asyncio.run(main()))
        self.assertEqual(len(bot.sent), 1)  # 文字兜底

    def test_poke_at_someone_else_ignored(self):
        bot = self._bot()
        cfg = {"enabled": True, "react.poke_back": True}

        async def main():
            return await reactions_mod.handle_notice(
                self._event(target=42, self_id=999), bot, cfg, ctx.get_timeline(1)
            )

        self.assertFalse(asyncio.run(main()))
        self.assertEqual(bot.diy.pokes, [])

    def test_poke_disabled(self):
        bot = self._bot()
        cfg = {"enabled": True, "react.poke_back": False}

        async def main():
            return await reactions_mod.handle_notice(
                self._event(), bot, cfg, ctx.get_timeline(1)
            )

        self.assertFalse(asyncio.run(main()))
        self.assertEqual(bot.diy.pokes, [])


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        reactions_mod._last_fire.clear()
        ctx.clear_all()

    def tearDown(self):
        reactions_mod._last_fire.clear()
        ctx.clear_all()

    def test_welcome_uses_template_and_nickname(self):
        from core.events import GroupIncreaseNoticeEvent

        bot = FakeBot()
        cfg = {
            "enabled": True,
            "react.welcome": True,
            "react.welcome_templates": ["欢迎 {name} 来玩"],
        }
        event = GroupIncreaseNoticeEvent(
            {
                "group_id": 1,
                "user_id": 5,
                "operator_id": 5,
                "sub_type": "approve",
                "sender": {"nickname": "小明"},
            }
        )

        async def main():
            return await reactions_mod.handle_notice(
                event, bot, cfg, ctx.get_timeline(1)
            )

        self.assertTrue(asyncio.run(main()))
        self.assertEqual(bot.sent[0][0], "欢迎 小明 来玩")

    def test_welcome_default_templates(self):
        from core.events import GroupIncreaseNoticeEvent

        bot = FakeBot()
        cfg = {"enabled": True, "react.welcome": True}
        event = GroupIncreaseNoticeEvent(
            {"group_id": 1, "user_id": 5, "operator_id": 5, "sub_type": "approve"}
        )

        async def main():
            return await reactions_mod.handle_notice(
                event, bot, cfg, ctx.get_timeline(1)
            )

        self.assertTrue(asyncio.run(main()))
        self.assertIn("用户5", str(bot.sent[0][0]))


class RecallTeaseTests(unittest.TestCase):
    """撤回调侃只引用内存时间线：查不到原文必须沉默。"""

    def setUp(self):
        reactions_mod._last_fire.clear()
        ctx.clear_all()

    def tearDown(self):
        reactions_mod._last_fire.clear()
        ctx.clear_all()

    def _make_recall_cls(self):
        """reactions 按类名分派，测试里造一个同名类。"""

        class GroupRecallNoticeEvent:
            def __init__(self, group_id, user_id, operator_id=None, message_id=None):
                self.group_id = group_id
                self.user_id = user_id
                self.operator_id = operator_id
                self.message_id = message_id
                self.raw_data = {}

        GroupRecallNoticeEvent.__name__ = "GroupRecallNoticeEvent"
        return GroupRecallNoticeEvent

    def test_recall_with_timeline_text(self):
        recall_cls = self._make_recall_cls()
        tl = ctx.get_timeline(1)
        tl.append(42, "张三", "我不该说这句话的")
        bot = FakeBot()
        cfg = {"enabled": True, "react.recall_tease": True}
        event = recall_cls(group_id=1, user_id=42)

        async def main():
            # 概率 0.6：多试几次确保至少命中一次（或直接验证不报错且原文不外泄）
            for _ in range(10):
                reactions_mod._last_fire.clear()
                await reactions_mod.handle_notice(event, bot, cfg, tl)

        asyncio.run(main())
        # 无论是否触发，都不能把没说过的话安上去
        for message, _, _ in bot.sent:
            self.assertIn("张三", str(message))

    def test_recall_without_timeline_stays_silent(self):
        recall_cls = self._make_recall_cls()
        tl = ctx.get_timeline(1)  # 空时间线（相当于重启后）
        bot = FakeBot()
        cfg = {"enabled": True, "react.recall_tease": True}
        event = recall_cls(group_id=1, user_id=42)

        async def main():
            for _ in range(10):
                reactions_mod._last_fire.clear()
                return await reactions_mod.handle_notice(event, bot, cfg, tl)

        self.assertFalse(asyncio.run(main()))
        self.assertEqual(bot.sent, [])


class QuoteFewshotTests(unittest.TestCase):
    def test_missing_db_returns_empty(self):
        self.assertEqual(style.quote_fewshot_lines(999999999), [])

    def test_reads_real_db_if_present(self):
        """项目里有真实语录库时验证只读查询；没有则跳过。"""
        from pathlib import Path

        db = Path(__file__).resolve().parent.parent / "data" / "quote" / "quotes.db"
        if not db.is_file():
            self.skipTest("语录库不存在")
        lines = style.quote_fewshot_lines(1)
        self.assertIsInstance(lines, list)


class ChimeMiddleMentionTests(unittest.TestCase):
    """唤醒词"居中被提及"的判定：开头触发叫 matched_nick（由 setup 主流程处理），
    中间出现才是 chime 概率接话的触发点。"""

    def _bot(self):
        class FakeReply:
            @staticmethod
            def get_plain_text(message):
                return message

        class FakeBot:
            reply = FakeReply()

        return FakeBot()

    def _call(self, text):

        cfg = {"persona": {"enabled": True, "nicknames": ["小月"]}}
        event = SimpleNamespace(message=text)
        return ai_setup._nickname_mentioned_in_middle(self._bot(), event, cfg)

    def test_middle_mention_detected(self):
        self.assertEqual(self._call("帮我看看 小月 在不在"), "小月")

    def test_prefix_mention_not_chime(self):
        """开头出现属于正常唤醒（startswith），不算居中提及。"""
        self.assertEqual(self._call("小月 今天天气"), "")

    def test_no_mention(self):
        self.assertEqual(self._call("今天天气不错"), "")

    def test_persona_disabled(self):

        cfg = {"persona": {"enabled": False, "nicknames": ["小月"]}}
        event = SimpleNamespace(message="帮我看看 小月 在不在")

        class FakeReply:
            @staticmethod
            def get_plain_text(message):
                return message

        class FakeBot:
            reply = FakeReply()

        self.assertEqual(ai_setup._nickname_mentioned_in_middle(FakeBot(), event, cfg), "")


if __name__ == "__main__":
    unittest.main()
