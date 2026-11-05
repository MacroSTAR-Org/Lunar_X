"""仓库卫生测试：确保含密钥的文件不会被误提交。

这些用例守的是"改完 .gitignore 但没生效"这类事故——
比如新增了 config.json 却忘了加进忽略列表，
或者某个含密钥的文件被 git add 之后又悄悄提交上去。
"""

import json
import re
import subprocess
import tomllib
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    return result.stdout


class TrackedFilesTests(unittest.TestCase):
    """含密钥的文件绝不能出现在 git 索引里。"""

    #: 这些文件含凭据，必须保持在 git 之外
    FORBIDDEN_TRACKED = (
        "config.json",
        "appsettings.json",
        "admin114.json",
        "webui.json",
        "webui.initial_password",
    )

    def test_secret_files_not_tracked(self):
        tracked = set(git("ls-files").split())
        for name in self.FORBIDDEN_TRACKED:
            self.assertNotIn(name, tracked, f"{name} 不能被 git 跟踪")

    def test_plugin_configs_not_tracked(self):
        """插件私有配置常含 API Key，同样不能进仓库。"""
        tracked = git("ls-files").split()
        offenders = [p for p in tracked if re.fullmatch(r"plugins/[^/]+/config\.json", p)]
        self.assertEqual(offenders, [], f"插件配置不应被跟踪: {offenders}")

    def test_example_config_is_tracked(self):
        """示例配置必须进仓库，否则别人没法照着改。"""
        tracked = set(git("ls-files").split())
        self.assertIn("config.example.json", tracked)
        self.assertIn("examples/config.example.jsonc", tracked)

    def test_gitignore_anchored_to_root(self):
        """忽略规则要用前导 / 锚定到根目录，否则会误伤 examples/ 下的模板。"""
        patterns = [
            line.strip()
            for line in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for name in ("config.json", "appsettings.json", "admin114.json", "webui.json"):
            self.assertIn(
                f"/{name}", patterns, f".gitignore 里 {name} 应写成 /{name} 以锚定到根目录"
            )


class ExampleConfigTests(unittest.TestCase):
    """示例配置本身必须是合法 JSON，且不含真实凭据。"""

    def test_example_config_is_valid_json(self):
        path = REPO_ROOT / "config.example.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, dict)

    def test_example_config_has_no_secrets(self):
        """示例里的 token 必须为空串——留着占位值就等于邀请别人照抄。"""
        path = REPO_ROOT / "config.example.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for key in ("token", "milky_token"):
            self.assertEqual(data.get(key, ""), "", f"config.example.json 的 {key} 必须为空")

    def test_example_config_root_user_is_zero(self):
        """root_user=0 表示不预置任何管理员，避免把别人的 QQ 写进模板。"""
        path = REPO_ROOT / "config.example.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("root_user"), 0)

    def test_example_config_only_contains_live_keys(self):
        """示例里不该出现代码根本不读的键（会误导使用者去填）。"""
        path = REPO_ROOT / "config.example.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        dead_keys = {
            "answer",
            "default_mode",
            "deepseek_key",
            "gemini_key",
            "openai_key",
            "ai",
            "bot_qq",
        }
        self.assertEqual(
            dead_keys & set(data.keys()), set(), "示例配置里有无人读取的死键"
        )


class VersionTests(unittest.TestCase):
    """版本号只能有一个来源，避免 WebUI/Bot/README 各说各话。"""

    def test_webui_and_core_version_match(self):
        import webui
        from core.version import __version__

        self.assertEqual(webui.get_framework_version(), __version__)

    def test_pyproject_uses_dynamic_version(self):
        data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(data["project"].get("dynamic"), ["version"])
        self.assertEqual(data["tool"]["hatch"]["version"]["path"], "core/version.py")

    def test_readme_badge_matches(self):
        from core.version import __version__

        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"版本-{__version__}-", readme)

    def test_changelog_mentions_current_version(self):
        """发版时 CHANGELOG 必须同步，避免文档与代码再次漂移。"""
        from core.version import __version__

        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## [{__version__}]", changelog)


class ExampleAppsettingsTests(unittest.TestCase):
    """examples/appsettings.json 是给协议端用的模板，不能含真实信息。"""

    def test_no_real_uin_or_token(self):
        path = REPO_ROOT / "examples" / "appsettings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("Account", {}).get("Uin"), 0)
        for impl in data.get("Implementations", []):
            self.assertEqual(impl.get("AccessToken", ""), "")

    def test_no_third_party_sign_server(self):
        """签名服务器地址指向个人服务器时不该留在仓库里。"""
        path = REPO_ROOT / "examples" / "appsettings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data.get("SignServerUrl", ""), "")


class ScriptSyntaxTests(unittest.TestCase):
    """维护脚本不能有语法错误，否则出事时才发现就晚了。"""

    def test_python_scripts_compile(self):
        for script in (REPO_ROOT / "scripts").glob("*.py"):
            with self.subTest(script=script.name):
                compile(script.read_text(encoding="utf-8"), str(script), "exec")

    def test_shell_script_syntax(self):
        script = REPO_ROOT / "scripts" / "purge_history_secrets.sh"
        if not script.exists():
            self.skipTest("脚本不存在")
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
