import json
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import ClassVar

from core.security import (
    CrossProcessLock,
    SecurityError,
    atomic_write_json,
    is_safe_plugin_name,
    resolve_within,
    safe_extract_zip,
    sanitize_log_text,
    validate_download_url,
)


class PluginNameTests(unittest.TestCase):
    def test_valid(self):
        for name in ("ping", "ai_chat", "my-plugin", "plugin.v2", "A1"):
            self.assertTrue(is_safe_plugin_name(name), name)

    def test_invalid(self):
        for name in (
            "",
            ".",
            "..",
            "../core",
            "a/b",
            "a\\b",
            "core",
            "json",
            "os",
            "-leading",
            "trailing.",
            "with space",
            "中文",
            "x" * 65,
        ):
            self.assertFalse(is_safe_plugin_name(name), name)

    def test_non_string(self):
        self.assertFalse(is_safe_plugin_name(None))
        self.assertFalse(is_safe_plugin_name(123))


class PathTests(unittest.TestCase):
    def test_resolve_within(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugins"
            root.mkdir()
            self.assertEqual(resolve_within(root, "demo"), (root / "demo").resolve())
            self.assertEqual(resolve_within(root, "."), root.resolve())

            with self.assertRaises(SecurityError):
                resolve_within(root, "..")
            with self.assertRaises(SecurityError):
                resolve_within(root, "../config.json")
            with self.assertRaises(SecurityError):
                resolve_within(root, "/etc/passwd")


class ZipTests(unittest.TestCase):
    def _make_zip(self, path: Path, members: list[tuple[str, bytes]]) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members:
                archive.writestr(name, data)

    def test_extract_normal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "p.zip"
            self._make_zip(zip_path, [("repo/setup.py", b"print(1)")])
            target = tmp_path / "out"
            count, size = safe_extract_zip(zip_path, target)
            self.assertEqual(count, 1)
            self.assertGreater(size, 0)
            self.assertTrue((target / "repo" / "setup.py").exists())

    def test_reject_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "p.zip"
            self._make_zip(zip_path, [("../evil.py", b"x")])
            with self.assertRaises(SecurityError):
                safe_extract_zip(zip_path, tmp_path / "out")

    def test_reject_absolute(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "p.zip"
            self._make_zip(zip_path, [("/etc/evil", b"x")])
            with self.assertRaises(SecurityError):
                safe_extract_zip(zip_path, tmp_path / "out")

    def test_reject_backslash_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "p.zip"
            self._make_zip(zip_path, [("..\\..\\evil.py", b"x")])
            with self.assertRaises(SecurityError):
                safe_extract_zip(zip_path, tmp_path / "out")

    def test_reject_too_many_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            zip_path = tmp_path / "p.zip"
            self._make_zip(zip_path, [(f"f{i}.txt", b"x") for i in range(5)])
            with self.assertRaises(SecurityError):
                safe_extract_zip(zip_path, tmp_path / "out", max_files=3)


class DownloadUrlTests(unittest.TestCase):
    allowed: ClassVar[set[str]] = {"github.com", "codeload.github.com"}

    def test_allowed(self):
        self.assertEqual(
            validate_download_url("https://github.com/a/b.zip", self.allowed),
            "https://github.com/a/b.zip",
        )
        self.assertTrue(
            validate_download_url("https://codeload.github.com/a/b/zip/main", self.allowed)
        )

    def test_rejected(self):
        for url in (
            "http://github.com/a.zip",
            "https://127.0.0.1/a.zip",
            "https://localhost/a.zip",
            "https://10.0.0.1/a.zip",
            "https://evil.com/a.zip",
            "https://user:pass@github.com/a.zip",
            "https://github.com:8443/a.zip",
            "ftp://github.com/a.zip",
            "",
            None,
        ):
            with self.assertRaises(SecurityError):
                validate_download_url(url, self.allowed)


class AtomicWriteTests(unittest.TestCase):
    def test_write_and_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "config.json"
            atomic_write_json(path, {"a": "中文"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"a": "中文"})
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class SanitizeTests(unittest.TestCase):
    def test_strips_control_chars(self):
        self.assertEqual(sanitize_log_text("a\nb\x1b[31mc", 20), "a?b?[31mc")
        self.assertEqual(sanitize_log_text("x" * 100, 10), "x" * 10)


class CrossProcessLockTests(unittest.TestCase):
    def test_second_lock_waits_until_release(self):
        """多 worker 部署时，第二个安装任务必须被文件锁挡住。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plugin-install.lock"
            first = CrossProcessLock(path, timeout=0)
            second = CrossProcessLock(path, timeout=0)

            self.assertTrue(first.acquire(blocking=False))
            self.assertFalse(second.acquire(blocking=False))

            first.release()
            self.assertTrue(second.acquire(blocking=False))
            second.release()

    def test_context_manager_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "init.lock"
            with CrossProcessLock(path, timeout=1):
                self.assertFalse(CrossProcessLock(path, timeout=0).acquire(blocking=False))
            self.assertTrue(CrossProcessLock(path, timeout=0).acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
