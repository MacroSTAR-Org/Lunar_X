"""ai_chat function calling 循环的单元测试（mock LLM，不发网络请求）。

覆盖：工具执行回填、权限拒绝回填、截图画面注入、次数上限、无工具直答。
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"))

import agents

from core.tools import ToolCallContext, ToolRegistry


def _llm_response(content="", tool_calls=None):
    message: dict = {"content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


def _tool_call(name, call_id="call_1", **args):
    import json

    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class FakeBot:
    def __init__(self, registry, admin=False):
        self.tools = registry
        self.plugin_logger = None
        self._admin = admin

    def _check_permission(self, user_id, level="manager"):
        return self._admin


def _run_loop(registry, responses, admin=False, cfg=None, max_rounds=None,
              max_calls=None):
    """跑一次 call_replier，返回 (最终回复, 完整 messages)。"""
    calls = {"n": 0}

    async def fake_post_chat(session, api_base, api_key, payload, timeout):
        idx = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        return responses[idx]

    original = agents._post_chat
    agents._post_chat = fake_post_chat

    bot = FakeBot(registry, admin=admin)
    tool_ctx = ToolCallContext(user_id=42, group_id=1)
    cfg = dict(cfg or {})
    if max_rounds is not None:
        cfg["agent.max_rounds"] = max_rounds
    if max_calls is not None:
        cfg["agent.max_tool_calls"] = max_calls

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "干活"}]
    tools = registry.openapi_schemas()
    try:
        reply = asyncio.run(
            agents.call_replier(
                None, "https://x", "k", "m", cfg, messages,
                tools=tools, tool_ctx=tool_ctx, bot=bot,
            )
        )
    finally:
        agents._post_chat = original
    return reply, messages, tool_ctx


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()

        async def handler(_ctx, **kwargs):
            return f"执行完成 {kwargs}"

        self.reg.register("run_command", "t", None, handler, owner="cu")

    def test_tool_executed_and_final_reply(self):
        responses = [
            _llm_response(tool_calls=[_tool_call("run_command", **{"command": "ls"})]),
            _llm_response("干完了"),
        ]
        reply, messages, _ = _run_loop(self.reg, responses)

        self.assertEqual(reply, "干完了")
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("执行完成", tool_messages[0]["content"])
        # assistant 的 tool_calls 消息也被回填
        self.assertTrue(any(m.get("tool_calls") for m in messages))

    def test_admin_only_denied_fed_back(self):
        """权限不足不该终止对话：拒绝原因作为工具结果回给模型。"""
        self.reg.register("secret", "t", None,
                          lambda: None, owner="cu") if False else None

        async def handler(_ctx, **kwargs):
            return "secret"

        self.reg.register("secret", "t", None, handler, owner="cu", admin_only=True)

        responses = [
            _llm_response(tool_calls=[_tool_call("secret")]),
            _llm_response("那我改用别的方式"),
        ]
        reply, messages, _ = _run_loop(self.reg, responses, admin=False)

        self.assertEqual(reply, "那我改用别的方式")
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertIn("权限不足", tool_messages[0]["content"])

    def test_screenshot_images_injected(self):
        """截图闭环：工具把 data URL 放进 ctx.images → 注入图片消息。"""
        seen_images = []

        async def shot_handler(_ctx, **kwargs):
            _ctx.images.append("data:image/jpeg;base64,AAA")
            seen_images.append(dict(_ctx.images))
            return "截图已发送"

        self.reg.register("take_screenshot", "t", None, shot_handler, owner="cu")

        responses = [
            _llm_response(tool_calls=[_tool_call("take_screenshot")]),
            _llm_response("我看到屏幕了"),
        ]
        reply, messages, tool_ctx = _run_loop(self.reg, responses)

        self.assertEqual(reply, "我看到屏幕了")
        image_messages = [
            m for m in messages
            if m.get("role") == "user" and isinstance(m.get("content"), list)
        ]
        self.assertEqual(len(image_messages), 1)
        self.assertEqual(image_messages[0]["content"][1]["image_url"]["url"],
                         "data:image/jpeg;base64,AAA")
        # 注入后清空，不会重复注入
        self.assertEqual(tool_ctx.images, [])

    def test_max_tool_calls_cap(self):
        async def handler(_ctx, **kwargs):
            return "ok"

        self.reg.register("t", "t", None, handler, owner="cu")

        responses = [
            _llm_response(tool_calls=[_tool_call("t", call_id=f"c{i}")])
            for i in range(10)
        ]
        responses.append(_llm_response("结束"))
        _reply, messages, _ = _run_loop(self.reg, responses, max_calls=3)

        tool_messages = [m for m in messages if m.get("role") == "tool"]
        self.assertLessEqual(len(tool_messages), 3)

    def test_max_rounds_cap(self):
        async def handler(_ctx, **kwargs):
            return "ok"

        self.reg.register("t", "t", None, handler, owner="cu")

        responses = [
            _llm_response(tool_calls=[_tool_call("t", call_id=f"c{i}")]) for i in range(50)
        ]
        reply, _messages, _ = _run_loop(self.reg, responses, max_rounds=2)

        self.assertIn("未在限定轮次内完成", reply)

    def test_no_tools_plain_call(self):
        responses = [_llm_response("直接回答")]
        reply, messages, _ = _run_loop(self.reg, responses)

        self.assertEqual(reply, "直接回答")
        self.assertEqual(len(messages), 2)  # 只有 system + user，没有工具消息

    def test_registry_empty_means_no_tools_payload(self):
        empty_reg = ToolRegistry()
        responses = [_llm_response("好")]
        reply, _messages, _ = _run_loop(empty_reg, responses)
        self.assertEqual(reply, "好")

    def test_bad_arguments_json(self):
        """模型给的 arguments 不是合法 JSON：注册表转成错误文字，对话不断。"""

        async def handler(_ctx, **kwargs):
            return "ok"

        self.reg.register("t", "t", None, handler, owner="cu")
        broken = [
            _llm_response(tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "t", "arguments": "{不是json"},
            }]),
            _llm_response("兜底回答"),
        ]

        calls = {"n": 0}

        async def fake_post_chat(session, api_base, api_key, payload, timeout):
            idx = min(calls["n"], len(broken) - 1)
            calls["n"] += 1
            return broken[idx]

        original = agents._post_chat
        agents._post_chat = fake_post_chat
        bot = FakeBot(self.reg, admin=True)
        tool_ctx = ToolCallContext(user_id=1, group_id=None)
        try:
            reply = asyncio.run(
                agents.call_replier(
                    None, "https://x", "k", "m", {},
                    [{"role": "user", "content": "x"}],
                    tools=self.reg.openapi_schemas(), tool_ctx=tool_ctx, bot=bot,
                )
            )
        finally:
            agents._post_chat = original
        self.assertEqual(reply, "兜底回答")


if __name__ == "__main__":
    unittest.main()
