"""核心协议链路端到端测试：假 OneBot / Milky 协议端。

覆盖真实路径：WebSocket 连接 → 事件解析 → 命令/插件分发 → 发送 API →
协议端响应 → 断线重连。这些是单元测试覆盖不到、但生产最容易出事故的部分。
"""

import asyncio
import contextlib
import json
import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import websockets
from aiohttp import web

import core.bot as bot_module
from core.bot import LunarBot


def _group_message(text: str, group_id: int = 123, user_id: int = 456) -> dict:
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "time": 1700000000,
        "self_id": 999,
        "group_id": group_id,
        "user_id": user_id,
        "message_id": 1,
        "raw_message": text,
        "message": [{"type": "text", "data": {"text": text}}],
        "sender": {"user_id": user_id, "nickname": "tester"},
    }


class FakeOneBotServer:
    def __init__(self):
        self.server = None
        self.port = 0
        self.connected = asyncio.Event()
        self.actions: list[dict] = []
        self._ws = None

    async def start(self) -> None:
        self.server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def _handler(self, ws) -> None:
        self._ws = ws
        self.connected.set()
        try:
            async for raw in ws:
                data = json.loads(raw)
                self.actions.append(data)
                echo = data.get("echo")
                if echo:
                    await ws.send(
                        json.dumps(
                            {
                                "status": "ok",
                                "retcode": 0,
                                "data": {"message_id": 1},
                                "echo": echo,
                            }
                        )
                    )
        except websockets.ConnectionClosed:
            pass

    async def send_event(self, event: dict) -> None:
        assert self._ws is not None
        await self._ws.send(json.dumps(event))

    async def disconnect(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()

    async def wait_for_actions(self, count: int, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.actions) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待 {count} 个 action 超时，当前 {len(self.actions)}")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


class FakeMilkyServer:
    """假的 Milky 协议端：HTTP API + WebSocket 事件通道。"""

    def __init__(self):
        self.app = web.Application()
        self.app.router.add_get("/event", self._ws_handler)
        self.app.router.add_post("/api/{action}", self._api_handler)
        self._runner: web.AppRunner | None = None
        self._sock: socket.socket | None = None
        self.port = 0
        self.connected = asyncio.Event()
        self.actions: list[dict] = []
        self._ws: web.WebSocketResponse | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self._sock.setblocking(False)
        self.port = self._sock.getsockname()[1]
        await web.SockSite(self._runner, self._sock).start()

    async def _ws_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        self.connected.set()
        async for _msg in ws:
            pass
        return ws

    async def _api_handler(self, request):
        action = request.match_info["action"]
        try:
            params = await request.json()
        except Exception:
            params = {}
        self.actions.append({"action": action, "params": params})
        return web.json_response({"status": "ok", "retcode": 0, "data": {}})

    async def send_event(self, event: dict) -> None:
        assert self._ws is not None
        await self._ws.send_json(event)

    async def disconnect(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()

    async def wait_for_actions(self, count: int, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.actions) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"等待 {count} 个 action 超时，当前 {len(self.actions)}")
            await asyncio.sleep(0.01)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        if self._sock is not None:
            self._sock.close()


def _milky_group_message(text: str, group_id: int = 123, user_id: int = 456) -> dict:
    return {
        "event_type": "message_receive",
        "time": 1700000000,
        "self_id": 999,
        "data": {
            "message_scene": "group",
            "peer_id": group_id,
            "sender_id": user_id,
            "message_seq": 1,
            "segments": [{"type": "text", "data": {"text": text}}],
            "group_member": {"card": "", "nickname": "tester", "role": "member"},
        },
    }


class CoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._original_begin_run = bot_module.logger.begin_run
        # 测试不归档/重建真实的 logs/ 目录
        bot_module.logger.begin_run = lambda: None

    def tearDown(self):
        bot_module.logger.begin_run = self._original_begin_run

    def test_onebot_connect_dispatch_send_and_reconnect(self):
        async def scenario():
            server = FakeOneBotServer()
            await server.start()
            bot = None
            listen_task = None
            try:
                config = {
                    "protocol": "onebot_v11",
                    "ws_server": f"ws://127.0.0.1:{server.port}",
                    "token": "",
                    "trigger_keyword": "$",
                    "log_level": "ERROR",
                }
                bot = LunarBot(config, asyncio.get_running_loop())

                class EchoPlugin:
                    TRIGGHT_KEYWORD = "Any"
                    PLT_ST = 1
                    HELP_MESSAGE = ""

                    async def on_message(self, event, lunar):
                        await lunar.send("pong", group_id=event.group_id)
                        return True

                bot.plugin_manager.plugins["e2e_echo"] = {
                    "module": EchoPlugin(),
                    "type": "file",
                    "path": "",
                    "trigger": "Any",
                    "help": "",
                    "enabled": True,
                    "priority": 1,
                }

                self.assertTrue(await bot.connection.connect())
                listen_task = asyncio.create_task(bot._listen_events())

                await asyncio.wait_for(server.connected.wait(), 3)
                await server.send_event(_group_message("hello"))
                await server.wait_for_actions(1)
                await asyncio.sleep(0.05)  # 等 Bot 收到 API 响应

                first = server.actions[0]
                self.assertEqual(first["action"], "send_group_msg")
                self.assertEqual(first["params"]["group_id"], 123)
                self.assertEqual(first["params"]["message"][0]["data"]["text"], "pong")

                # 断线后应自动重连，并继续处理新事件
                server.connected.clear()
                await server.disconnect()
                await asyncio.wait_for(server.connected.wait(), 5)

                await server.send_event(_group_message("again"))
                await server.wait_for_actions(2)
                await asyncio.sleep(0.05)
                second = server.actions[1]
                self.assertEqual(second["action"], "send_group_msg")
                self.assertEqual(second["params"]["message"][0]["data"]["text"], "pong")
            finally:
                if listen_task is not None:
                    listen_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await listen_task
                if bot is not None:
                    with contextlib.suppress(Exception):
                        await bot.connection.close()
                await server.stop()

        asyncio.run(scenario())

    def test_milky_connect_dispatch_send_and_reconnect(self):
        """Milky 走完整的 HTTP API + WebSocket 事件链路，断线后同样自动重连。"""

        async def scenario():
            server = FakeMilkyServer()
            await server.start()
            bot = None
            listen_task = None
            try:
                config = {
                    "protocol": "milky",
                    "milky_server": f"http://127.0.0.1:{server.port}",
                    "milky_token": "",
                    "trigger_keyword": "$",
                    "log_level": "ERROR",
                }
                bot = LunarBot(config, asyncio.get_running_loop())

                class EchoPlugin:
                    TRIGGHT_KEYWORD = "Any"
                    PLT_ST = 1
                    HELP_MESSAGE = ""

                    async def on_message(self, event, lunar):
                        await lunar.send("pong", group_id=event.group_id)
                        return True

                bot.plugin_manager.plugins["milky_echo"] = {
                    "module": EchoPlugin(),
                    "type": "file",
                    "path": "",
                    "trigger": "Any",
                    "help": "",
                    "enabled": True,
                    "priority": 1,
                }

                self.assertTrue(await bot.connection.connect())
                listen_task = asyncio.create_task(bot._listen_events())

                await asyncio.wait_for(server.connected.wait(), 3)
                await server.send_event(_milky_group_message("hello"))
                await server.wait_for_actions(1)
                await asyncio.sleep(0.05)  # 等 Bot 收到 API 响应

                first = server.actions[0]
                self.assertEqual(first["action"], "send_group_message")
                self.assertEqual(first["params"]["group_id"], 123)
                self.assertEqual(first["params"]["message"][0]["data"]["text"], "pong")

                server.connected.clear()
                await server.disconnect()
                await asyncio.wait_for(server.connected.wait(), 5)

                await server.send_event(_milky_group_message("again"))
                await server.wait_for_actions(2)
                await asyncio.sleep(0.05)
                second = server.actions[1]
                self.assertEqual(second["action"], "send_group_message")
                self.assertEqual(second["params"]["message"][0]["data"]["text"], "pong")
            finally:
                if listen_task is not None:
                    listen_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await listen_task
                if bot is not None:
                    with contextlib.suppress(Exception):
                        await bot.connection.close()
                await server.stop()

        asyncio.run(scenario())

    def test_milky_transport_is_constructed_with_same_queue_limit(self):
        """配置里的 event_queue_size 必须同时作用于两套协议。"""
        config = {
            "protocol": "milky",
            "milky_server": "http://127.0.0.1:3010",
            "milky_token": "",
            "event_queue_size": 7,
            "log_level": "ERROR",
        }
        loop = asyncio.new_event_loop()
        bot = LunarBot(config, loop)
        try:
            self.assertEqual(bot.connection._message_queue.maxsize, 7)
        finally:
            with contextlib.suppress(Exception):
                loop.run_until_complete(bot.connection.close())
            loop.close()

    def _run_single_event(self, config_extra, plugin):
        async def scenario():
            server = FakeOneBotServer()
            await server.start()
            bot = None
            listen_task = None
            try:
                config = {
                    "protocol": "onebot_v11",
                    "ws_server": f"ws://127.0.0.1:{server.port}",
                    "token": "",
                    "trigger_keyword": "$",
                    "log_level": "ERROR",
                    **config_extra,
                }
                bot = LunarBot(config, asyncio.get_running_loop())
                bot.plugin_manager.plugins["e2e_test"] = {
                    "module": plugin,
                    "type": "file",
                    "path": "",
                    "trigger": "Any",
                    "help": "",
                    "enabled": True,
                    "priority": 1,
                }
                self.assertTrue(await bot.connection.connect())
                listen_task = asyncio.create_task(bot._listen_events())
                await asyncio.wait_for(server.connected.wait(), 3)
                return server, bot, listen_task
            except Exception:
                if listen_task is not None:
                    listen_task.cancel()
                if bot is not None:
                    with contextlib.suppress(Exception):
                        await bot.connection.close()
                await server.stop()
                raise

        return scenario()

    @staticmethod
    async def _close_scenario(server, bot, listen_task):
        if listen_task is not None:
            listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listen_task
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.connection.close()
        await server.stop()

    def test_different_conversations_do_not_block_each_other(self):
        """一个群里的慢插件不能拖住另一个群的事件处理。"""

        class GroupPlugin:
            TRIGGHT_KEYWORD = "Any"
            PLT_ST = 1
            HELP_MESSAGE = ""

            async def on_message(self, event, lunar):
                if event.group_id == 100:
                    await asyncio.sleep(0.4)
                    await lunar.send("slow", group_id=event.group_id)
                else:
                    await lunar.send("fast", group_id=event.group_id)
                return True

        async def scenario():
            server, bot, listen_task = await self._run_single_event({}, GroupPlugin())
            try:
                await server.send_event(_group_message("first", group_id=100))
                await server.send_event(_group_message("second", group_id=200))
                await server.wait_for_actions(2, timeout=2.0)
                self.assertEqual(server.actions[0]["params"]["group_id"], 200)
                self.assertEqual(
                    server.actions[0]["params"]["message"][0]["data"]["text"], "fast"
                )
                self.assertEqual(server.actions[1]["params"]["group_id"], 100)
            finally:
                await self._close_scenario(server, bot, listen_task)

        asyncio.run(scenario())

    def test_plugin_timeout_does_not_freeze_dispatch(self):
        """卡死的插件在超时后被取消，后续插件仍能处理同一事件。"""

        class SlowPlugin:
            TRIGGHT_KEYWORD = "Any"
            PLT_ST = 1
            HELP_MESSAGE = ""

            async def on_message(self, event, lunar):
                await asyncio.sleep(5)
                await lunar.send("slow", group_id=event.group_id)
                return True

        class FastPlugin:
            TRIGGHT_KEYWORD = "Any"
            PLT_ST = 2
            HELP_MESSAGE = ""

            async def on_message(self, event, lunar):
                await lunar.send("fast", group_id=event.group_id)
                return True

        async def scenario():
            server, bot, listen_task = await self._run_single_event(
                {"plugin_timeout": 0.1}, SlowPlugin()
            )
            bot.plugin_manager.plugins["e2e_fast"] = {
                "module": FastPlugin(),
                "type": "file",
                "path": "",
                "trigger": "Any",
                "help": "",
                "enabled": True,
                "priority": 2,
            }
            try:
                await server.send_event(_group_message("timeout"))
                await server.wait_for_actions(1, timeout=1.0)
                self.assertEqual(
                    server.actions[0]["params"]["message"][0]["data"]["text"], "fast"
                )
            finally:
                await self._close_scenario(server, bot, listen_task)

        asyncio.run(scenario())

    def test_same_conversation_is_serial_and_backlog_is_capped(self):
        """同群严格串行；积压超过上限后丢弃新事件并计数。"""

        class BlockingPlugin:
            TRIGGHT_KEYWORD = "Any"
            PLT_ST = 1
            HELP_MESSAGE = ""

            def __init__(self):
                self.active = 0
                self.max_active = 0

            async def on_message(self, event, lunar):
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                try:
                    await asyncio.sleep(0.2)
                    await lunar.send("done", group_id=event.group_id)
                finally:
                    self.active -= 1
                return True

        plugin = BlockingPlugin()

        async def scenario():
            server, bot, listen_task = await self._run_single_event(
                {"conversation_queue_size": 1}, plugin
            )
            try:
                for text in ("one", "two", "three"):
                    await server.send_event(_group_message(text, group_id=300))
                await server.wait_for_actions(1, timeout=2.0)
                await asyncio.sleep(0.3)
                self.assertEqual(len(server.actions), 1, "同群积压上限外的消息应被丢弃")
                self.assertEqual(plugin.max_active, 1, "同一会话必须串行处理")
                self.assertEqual(bot._dispatch_stats["dropped"], 2)
            finally:
                await self._close_scenario(server, bot, listen_task)

        asyncio.run(scenario())

    def test_native_command_permission_is_enforced(self):
        """命令解析 → 原生命令 → 权限检查必须走真实链路。"""

        class NoopPlugin:
            TRIGGHT_KEYWORD = "Any"
            PLT_ST = 1
            HELP_MESSAGE = ""

            async def on_message(self, event, lunar):
                return False

        async def scenario():
            server, bot, listen_task = await self._run_single_event(
                {"root_user": 999}, NoopPlugin()
            )
            try:
                await server.send_event(
                    _group_message("$添加管理员 123", group_id=400, user_id=456)
                )
                await server.wait_for_actions(1, timeout=2.0)
                text = server.actions[0]["params"]["message"][0]["data"]["text"]
                self.assertIn("权限不足", text)
            finally:
                await self._close_scenario(server, bot, listen_task)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
