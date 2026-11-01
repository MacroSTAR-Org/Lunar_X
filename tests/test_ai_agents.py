"""智能体调用层的单元测试：规划输出解析、工具执行、消息组装、模型解析。

全部纯逻辑，不发起网络请求。
"""

import sys
import unittest
from pathlib import Path

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
sys.path.insert(0, str(_PLUGIN))

import agents
import context as ctx
import style


class ParsePlannerOutputTests(unittest.TestCase):
    def test_valid_json(self):
        actions = agents.parse_planner_output(
            '{"actions": [{"type": "reply", "seq": 5, "reason": "被点名"}]}'
        )
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].type, "reply")
        self.assertEqual(actions[0].seq, 5)
        self.assertEqual(actions[0].reason, "被点名")

    def test_json_with_prefix_text(self):
        """模型偶尔在 JSON 外套解释文字，要能容忍。"""
        actions = agents.parse_planner_output(
            '好的，我的判断是：\n{"actions": [{"type": "see_image", "seq": 2}]}\n以上。'
        )
        self.assertEqual(actions[0].type, "see_image")

    def test_invalid_json_falls_back_to_ignore(self):
        for bad in ("", "我觉得不用回复", "{actions: []}", '{"actions": "x"}'):
            actions = agents.parse_planner_output(bad)
            self.assertEqual(actions[0].type, "ignore", bad)

    def test_unknown_type_filtered(self):
        actions = agents.parse_planner_output(
            '{"actions": [{"type": "hack"}, {"type": "speak", "reason": "ok"}]}'
        )
        self.assertEqual([a.type for a in actions], ["speak"])

    def test_seq_coercion(self):
        actions = agents.parse_planner_output(
            '{"actions": [{"type": "reply", "seq": "7"}]}'
        )
        self.assertEqual(actions[0].seq, 7)

    def test_reason_truncated(self):
        import json

        actions = agents.parse_planner_output(
            json.dumps({"actions": [{"type": "speak", "reason": "x" * 500}]})
        )
        self.assertEqual(len(actions[0].reason), 200)


class ExecutePlannerToolTests(unittest.TestCase):
    def test_query_history_returns_earlier_only(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        for i in range(5):
            tl.append(1, "a", f"m{i}")  # seq 1..5
        result = agents._execute_planner_tool(tl, "query_history", '{"before_seq": 3}', 2000)
        self.assertIn("m0", result)  # seq 1
        self.assertIn("m1", result)  # seq 2
        self.assertNotIn("m2", result)  # seq 3 不满足 < before_seq（开区间）
        self.assertNotIn("m4", result)

    def test_limit_respected(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        for i in range(5):
            tl.append(1, "a", f"m{i}")
        result = agents._execute_planner_tool(
            tl, "query_history", '{"before_seq": 99, "limit": 2}', 2000
        )
        self.assertNotIn("m0", result)  # 只保留最近 2 条
        self.assertIn("m4", result)

    def test_unknown_tool(self):
        tl = ctx.GroupTimeline(max_entries=5, ttl_minutes=120)
        result = agents._execute_planner_tool(tl, "delete_everything", "{}", 2000)
        self.assertIn("未知工具", result)

    def test_bad_arguments(self):
        tl = ctx.GroupTimeline(max_entries=5, ttl_minutes=120)
        self.assertIn("JSON", agents._execute_planner_tool(tl, "query_history", "not json", 2000))
        self.assertIn(
            "整数", agents._execute_planner_tool(tl, "query_history", '{"before_seq": "x"}', 2000)
        )


class BuildReplyMessagesTests(unittest.TestCase):
    def _cfg(self):
        return {"listen.max_chars": 2500}

    def test_layer_order(self):
        tl = ctx.GroupTimeline(max_entries=10, ttl_minutes=120)
        tl.append(1, "a", "hello")
        messages = agents.build_reply_messages(
            self._cfg(),
            tl,
            ["该用户在读研"],
            [{"role": "user", "content": "hi"}],
            "你好",
            nickname="张三",
            style_hint="口语化",
            style_samples=["哈哈哈"],
        )
        roles = [m["role"] for m in messages]
        self.assertEqual(roles.count("system"), 5)  # 人格/时间/旁听/记忆/风格示例
        contents = [m["content"] for m in messages if m["role"] == "system"]
        self.assertIn("聊天记录", contents[2])  # 旁听摘录
        self.assertIn("在读研", contents[3])  # 长期记忆
        self.assertIn("说话风格", contents[4])  # 风格示例
        self.assertEqual(messages[-1], {"role": "user", "content": "你好"})

    def test_private_chat_no_timeline_block(self):
        messages = agents.build_reply_messages(self._cfg(), None, [], [], "hi")
        self.assertEqual(len(messages), 3)  # 人格 + 时间 + user

    def test_persona_priority(self):
        cfg = {
            "persona": {"system_prompt": "人格"},
            "system_prompt": "通用",
        }
        self.assertEqual(agents.persona_system_prompt(cfg), "人格")
        self.assertEqual(agents.persona_system_prompt({"system_prompt": "通用"}), "通用")
        self.assertIn("Lunar X", agents.persona_system_prompt({}))


class ResolveModelTests(unittest.TestCase):
    def test_fallback_to_main(self):
        self.assertEqual(agents.resolve_model({}, "planner.model", "main"), "main")
        self.assertEqual(
            agents.resolve_model({"planner.model": "  "}, "planner.model", "main"), "main"
        )

    def test_role_override(self):
        self.assertEqual(
            agents.resolve_model({"planner.model": "cheap"}, "planner.model", "main"), "cheap"
        )


class StripMarkdownTests(unittest.TestCase):
    def test_code_fence_removed(self):
        text = "看这个：\n```python\nprint(1)\n```\n好了"
        self.assertNotIn("```", agents.strip_markdown_noise(text))
        self.assertIn("print(1)", agents.strip_markdown_noise(text))

    def test_heading_hashes_removed(self):
        self.assertEqual(agents.strip_markdown_noise("## 标题\n正文"), "标题\n正文")


class ProactiveGateTests(unittest.TestCase):
    def test_speak_only_in_timer_mode(self):
        self.assertFalse(style.speak_allowed({"proactive.mode": "off"}))
        self.assertFalse(style.speak_allowed({"proactive.mode": "event"}))
        self.assertTrue(style.speak_allowed({"proactive.mode": "event+timer"}))

    def test_quiet_hours(self):
        import time as _time

        cfg = {"proactive.quiet_hours": "01:00-08:00"}
        # 构造今天 03:00 与 12:00 的本地时间戳
        def ts_at(hour, minute=0):
            t = _time.localtime()
            return _time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, minute, 0, 0, 0, -1))

        self.assertTrue(style.in_quiet_hours(cfg, ts_at(3)))
        self.assertFalse(style.in_quiet_hours(cfg, ts_at(12)))

    def test_quiet_hours_cross_midnight(self):
        import time as _time

        cfg = {"proactive.quiet_hours": "23:00-02:00"}

        def ts_at(hour):
            t = _time.localtime()
            return _time.mktime((t.tm_year, t.tm_mon, t.tm_mday, hour, 0, 0, 0, 0, -1))

        self.assertTrue(style.in_quiet_hours(cfg, ts_at(23, 30) if False else ts_at(23)))
        self.assertTrue(style.in_quiet_hours(cfg, ts_at(1)))
        self.assertFalse(style.in_quiet_hours(cfg, ts_at(12)))

    def test_quiet_hours_bad_config(self):
        self.assertFalse(style.in_quiet_hours({"proactive.quiet_hours": "乱写"}))
        self.assertFalse(style.in_quiet_hours({"proactive.quiet_hours": ""}))

    def test_chime_probability_zero(self):
        import context as _ctx

        _ctx.clear_all()
        self.assertFalse(style.chime_allowed(1, {"proactive.chime_chance": 0}))


class StyleLayerTests(unittest.TestCase):
    def test_split_short_reply_unchanged(self):
        self.assertEqual(style.split_reply("你好呀"), ["你好呀"])

    def test_split_by_sentence(self):
        parts = style.split_reply("今天天气不错！你打算去哪玩？我建议去公园。")
        self.assertEqual(len(parts), 3)

    def test_split_caps_at_max_segments(self):
        text = "一。二。三。四。五。六。七。八。"
        parts = style.split_reply(text, max_chars=4)
        self.assertLessEqual(len(parts), style.MAX_SEGMENTS)
        # 被合并的内容不能丢
        self.assertEqual(len("".join(parts)), len(text))

    def test_long_unpunctuated_hard_split(self):
        parts = style.split_reply("x" * 250, max_chars=80)
        self.assertTrue(all(len(p) <= 80 for p in parts))

    def test_typing_delay_bounds(self):
        self.assertLessEqual(style.typing_delay_seconds("x" * 500), 6.3)
        self.assertGreaterEqual(style.typing_delay_seconds("hi"), 0.8)


if __name__ == "__main__":
    unittest.main()
