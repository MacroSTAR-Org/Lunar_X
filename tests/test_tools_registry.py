"""工具注册表的单元测试：注册/注销、权限门控、审计、超时兜底。

computer_use 插件不在场也能测——注册表只认 handler 与元数据。
"""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tools import ToolCallContext, ToolRegistry


def _handler(result: str = "ok", delay: float = 0.0, boom: bool = False):
    async def handler(_ctx, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        if boom:
            raise RuntimeError("工具炸了")
        return f"{result} args={kwargs}"

    return handler


class RegistryLifecycleTests(unittest.TestCase):
    def test_register_and_query(self):
        reg = ToolRegistry()
        reg.register("ping", "回个 ping", None, _handler(), owner="demo")
        self.assertEqual(len(reg), 1)
        spec = reg.get("ping")
        self.assertEqual(spec.owner, "demo")
        # 空 parameters 自动补全成合法 JSON Schema
        self.assertEqual(spec.parameters["type"], "object")

    def test_openapi_schemas(self):
        reg = ToolRegistry()
        reg.register(
            "run",
            "跑命令",
            {"type": "object", "properties": {"cmd": {"type": "string"}}},
            _handler(),
            owner="demo",
        )
        schemas = reg.openapi_schemas()
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["type"], "function")
        self.assertEqual(schemas[0]["function"]["name"], "run")

    def test_duplicate_name_from_other_owner_rejected(self):
        reg = ToolRegistry()
        reg.register("x", "第一个", None, _handler(), owner="a")
        with self.assertRaises(ValueError):
            reg.register("x", "第二个", None, _handler(), owner="b")

    def test_same_owner_reregister_allowed(self):
        """热重载会重新注册同名工具：同 owner 覆盖是合法操作。"""
        reg = ToolRegistry()
        reg.register("x", "v1", None, _handler(), owner="a")
        reg.register("x", "v2", None, _handler(), owner="a")
        self.assertEqual(reg.get("x").description, "v2")

    def test_unregister_owner(self):
        reg = ToolRegistry()
        reg.register("a1", "t", None, _handler(), owner="a")
        reg.register("a2", "t", None, _handler(), owner="a")
        reg.register("b1", "t", None, _handler(), owner="b")
        self.assertEqual(reg.unregister_owner("a"), 2)
        self.assertEqual(len(reg), 1)
        self.assertEqual(reg.unregister_owner("a"), 0)

    def test_register_requires_name_and_description(self):
        reg = ToolRegistry()
        with self.assertRaises(ValueError):
            reg.register("", "desc", None, _handler(), owner="a")
        with self.assertRaises(ValueError):
            reg.register("x", "", None, _handler(), owner="a")


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.reg = ToolRegistry()
        self.caller = ToolCallContext(user_id=42, group_id=1)

    def test_basic_execution_passes_args(self):
        self.reg.register("echo", "t", None, _handler("done"), owner="demo")
        result = asyncio.run(
            self.reg.execute("echo", {"x": 1}, self.caller, is_admin=False)
        )
        self.assertIn("done", result)
        self.assertIn("x': 1", result)

    def test_unknown_tool(self):
        result = asyncio.run(
            self.reg.execute("nope", {}, self.caller, is_admin=False)
        )
        self.assertIn("未知工具", result)

    def test_handler_crash_returns_string(self):
        """工具抛异常不能炸掉对话循环：转成文字让模型兜底。"""
        self.reg.register("boom", "t", None, _handler(boom=True), owner="demo")
        result = asyncio.run(
            self.reg.execute("boom", {}, self.caller, is_admin=False)
        )
        self.assertIn("工具执行失败", result)

    def test_timeout_capped_to_string(self):
        self.reg.register("slow", "t", None, _handler("done", delay=0.3), owner="demo", timeout=1)
        result = asyncio.run(
            self.reg.execute("slow", {}, self.caller, is_admin=False)
        )
        self.assertIn("done", result)

    def test_admin_only_gate(self):
        calls = []

        async def handler(_ctx, **kwargs):
            calls.append(kwargs)
            return "secret"

        self.reg.register("admin_tool", "t", None, handler, owner="demo", admin_only=True)

        denied = asyncio.run(
            self.reg.execute("admin_tool", {}, self.caller, is_admin=False)
        )
        self.assertIn("权限不足", denied)
        self.assertEqual(calls, [])  # 未执行

        allowed = asyncio.run(
            self.reg.execute("admin_tool", {}, self.caller, is_admin=True)
        )
        self.assertEqual(allowed, "secret")

    def test_call_count_tracked(self):
        self.reg.register("t", "t", None, _handler(), owner="demo")
        for _ in range(3):
            asyncio.run(self.reg.execute("t", {}, self.caller, is_admin=False))
        self.assertEqual(self.reg.get("t").calls, 3)

    def test_result_truncated(self):
        self.reg.register("big", "t", None, _handler("x" * 99999), owner="demo")
        result = asyncio.run(self.reg.execute("big", {}, self.caller, is_admin=False))
        self.assertLessEqual(len(result), 8100)
        self.assertIn("已截断", result)

    def test_audit_log_written(self):
        import logging

        records: list[logging.LogRecord] = []

        class Collector(logging.Handler):
            def emit(self, record):
                records.append(record)

        log = logging.getLogger("audit_test")
        log.addHandler(Collector())
        log.setLevel(logging.INFO)

        self.reg.register("t", "t", None, _handler(), owner="demo")
        asyncio.run(
            self.reg.execute("t", {"cmd": "ls"}, self.caller, is_admin=False, log=log)
        )
        self.assertTrue(any("工具审计" in r.getMessage() for r in records))
        self.assertTrue(any("cmd" in r.getMessage() for r in records))

    def test_ctx_passed_to_handler(self):
        seen = {}

        async def handler(_ctx, **kwargs):
            seen["user"] = _ctx.user_id
            seen["group"] = _ctx.group_id
            return "ok"

        self.reg.register("t", "t", None, handler, owner="demo")
        asyncio.run(self.reg.execute("t", {}, self.caller, is_admin=False))
        self.assertEqual(seen, {"user": 42, "group": 1})


if __name__ == "__main__":
    unittest.main()
