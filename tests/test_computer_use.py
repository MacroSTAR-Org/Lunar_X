"""Computer Use 工具实现的单元测试（不依赖 pyautogui，只覆盖纯逻辑）。"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "computer_use"))

from pathlib import Path

import computer


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = {"workspace": {"root": self._tmp.name, "allow_outside": False}}

    def test_root_resolved(self):
        root = computer.workspace_root(self.cfg)
        self.assertTrue(root.is_absolute())

    def test_relative_path_stays_inside(self):
        resolved, err = computer.resolve_path(self.cfg, "sub/dir/file.txt")
        self.assertEqual(err, "")
        self.assertTrue(resolved.is_relative_to(computer.workspace_root(self.cfg)))

    def test_escape_rejected(self):
        resolved, err = computer.resolve_path(self.cfg, "../outside.txt")
        self.assertIsNone(resolved)
        self.assertIn("拒绝访问", err)

    def test_absolute_escape_rejected(self):
        resolved, err = computer.resolve_path(self.cfg, "/etc/passwd")
        self.assertIsNone(resolved)
        self.assertIn("拒绝访问", err)

    def test_symlink_escape_rejected(self):
        """resolve() 会解开软链——工作空间里的软链指向外部同样被拒。"""
        root = computer.workspace_root(self.cfg)
        link = root / "evil"
        try:
            link.symlink_to("/etc/hostname", target_is_directory=False)
        except OSError:
            self.skipTest("无法创建软链（权限或平台限制）")
        resolved, err = computer.resolve_path(self.cfg, "evil")
        self.assertIsNone(resolved)
        self.assertIn("拒绝访问", err)

    def test_allow_outside_escape_hatch(self):
        cfg = dict(self.cfg)
        cfg["workspace"]["allow_outside"] = True
        resolved, err = computer.resolve_path(cfg, "/etc/passwd")
        self.assertEqual(err, "")
        self.assertTrue(resolved.is_absolute())


class DangerCheckTests(unittest.TestCase):
    def test_deny_patterns(self):
        cfg = {"command": {"deny_patterns": ["sudo", "rm -rf /"]}}
        for cmd in ("sudo apt install", "rm -rf / --no-preserve-root", "RM -RF /"):
            err = computer.check_dangerous(cfg, cmd)
            self.assertTrue(err, cmd)

    def test_case_insensitive(self):
        cfg = {"command": {"deny_patterns": ["shutdown"]}}
        self.assertIn("禁止", computer.check_dangerous(cfg, "SHUTDOWN /s"))

    def test_safe_command(self):
        cfg = {"command": {"deny_patterns": ["sudo"]}}
        self.assertEqual(computer.check_dangerous(cfg, "ls -la"), "")

    def test_empty_patterns(self):
        self.assertEqual(computer.check_dangerous({}, "anything"), "")


class ShellRunnerTests(unittest.TestCase):
    def test_simple_command(self):
        code, output = computer._run_shell_sync("echo hello", Path("."), 10)
        self.assertEqual(code, 0)
        self.assertIn("hello", output)

    def test_stderr_captured(self):
        code, output = computer._run_shell_sync("ls /nonexistent_dir_xyz", Path("."), 10)
        self.assertNotEqual(code, 0)
        self.assertTrue(output)  # stderr 也被收集

    def test_timeout(self):
        code, output = computer._run_shell_sync("sleep 5", Path("."), 1)
        self.assertEqual(code, -1)
        self.assertIn("超时", output)


class CfgReadTests(unittest.TestCase):
    def test_nested_and_flat(self):
        cfg_nested = {"command": {"timeout": 120}}
        cfg_flat = {"command.timeout": 120}
        self.assertEqual(computer._get(cfg_nested, "command.timeout", 60), 120)
        self.assertEqual(computer._get(cfg_flat, "command.timeout", 60), 120)
        self.assertEqual(computer._get({}, "command.timeout", 60), 60)

    def test_int_clamped(self):
        cfg = {"command": {"timeout": 9999}}
        self.assertEqual(computer._get_int(cfg, "command.timeout", 60, 1, 600), 600)
        self.assertEqual(computer._get_int({}, "command.timeout", 60, 1, 600), 60)


class ToolImplTests(unittest.TestCase):
    """文件类工具（不走 pyautogui 路径）的端到端验证。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cfg = {
            "workspace": {"root": self._tmp.name, "allow_outside": False},
            "command": {"deny_patterns": ["sudo"], "max_output_chars": 4000, "timeout": 10},
        }

    def test_write_and_read_roundtrip(self):
        result = asyncio.run(
            computer.tool_write_file(None, self.cfg, None, "a/b.txt", "中文内容")
        )
        self.assertIn("写入", result)
        content = asyncio.run(computer.tool_read_file(None, self.cfg, None, "a/b.txt"))
        self.assertIn("中文内容", content)

    def test_run_command_in_workspace(self):
        result = asyncio.run(
            computer.tool_run_command(None, self.cfg, None, "pwd")
        )
        self.assertIn("退出码 0", result)
        self.assertIn(self._tmp.name, result)

    def test_run_command_dangerous_rejected(self):
        result = asyncio.run(
            computer.tool_run_command(None, self.cfg, None, "sudo rm something")
        )
        self.assertIn("拒绝", result)

    def test_run_command_empty(self):
        result = asyncio.run(computer.tool_run_command(None, self.cfg, None, "  "))
        self.assertIn("不能为空", result)

    def test_list_dir(self):
        asyncio.run(computer.tool_write_file(None, self.cfg, None, "x.txt", "1"))
        result = asyncio.run(computer.tool_list_dir(None, self.cfg, None, ""))
        self.assertIn("x.txt", result)

    def test_read_outside_rejected(self):
        result = asyncio.run(computer.tool_read_file(None, self.cfg, None, "/etc/passwd"))
        self.assertIn("拒绝访问", result)

    def test_write_outside_rejected(self):
        result = asyncio.run(
            computer.tool_write_file(None, self.cfg, None, "/tmp/evil.txt", "x")
        )
        self.assertIn("拒绝访问", result)


class PermissionTests(unittest.TestCase):
    def _bot(self, admin=False, uid=42):
        class FakeBot:
            def _check_permission(self, user_id, level="manager"):
                return admin and user_id == uid

            def plugin_config(self, name):
                return {"computer": {"allowed_users": [100]}}

        return FakeBot()

    def test_admin_allowed(self):
        self.assertTrue(computer.user_allowed(self._bot(admin=True), {}, 42))

    def test_whitelisted_allowed(self):
        cfg = {"computer": {"allowed_users": [100]}}
        self.assertTrue(computer.user_allowed(self._bot(), cfg, 100))

    def test_other_rejected(self):
        cfg = {"computer": {"allowed_users": [100]}}
        self.assertFalse(computer.user_allowed(self._bot(), cfg, 999))


if __name__ == "__main__":
    unittest.main()
