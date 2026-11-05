"""插件管理器回归测试：加载、热重载与保留名拒绝。"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logger import logger
from core.plugin_manager import PluginManager


class FakeBot:
    def __init__(self):
        self.config = {"trigger_keyword": "$"}
        self.plugins = {}
        self.plugin_logger = logger.get_logger("LunarPlugins")


class PluginManagerTests(unittest.TestCase):
    def _manager(self) -> PluginManager:
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        return PluginManager(FakeBot(), {"trigger_keyword": "$"}, loop)

    def test_reload_picks_up_new_plugin_code(self):
        source = """\
TRIGGHT_KEYWORD = "Any"
PLT_ST = 1
HELP_MESSAGE = ""
VALUE = {value}

async def on_message(event, bot):
    return False
"""

        async def scenario(tmp: str):
            manager = self._manager()
            manager.plugins_dir = tmp
            plugin_path = Path(tmp) / "reloadable.py"
            plugin_path.write_text(source.format(value=1), encoding="utf-8")

            manager.load_plugins()
            self.assertIn("reloadable", manager.plugins)
            self.assertEqual(manager.plugins["reloadable"]["module"].VALUE, 1)

            plugin_path.write_text(source.format(value=2), encoding="utf-8")
            self.assertTrue(await manager.reload_plugins())
            self.assertEqual(
                manager.plugins["reloadable"]["module"].VALUE,
                2,
                "热重载必须真正执行新代码，而不是复用过期的 sys.modules",
            )

        with tempfile.TemporaryDirectory() as tmp:
            try:
                asyncio.run(scenario(tmp))
            finally:
                sys.modules.pop("reloadable", None)

    def test_reserved_plugin_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            # os 已在 sys.modules 里；插件若以它命名会顶掉标准库
            (Path(tmp) / "os.py").write_text(
                "TRIGGHT_KEYWORD = 'Any'\nPLT_ST = 1\n", encoding="utf-8"
            )
            manager = self._manager()
            manager.plugins_dir = tmp
            manager.load_plugins()
            self.assertNotIn("os", manager.plugins)
            self.assertIn("os", manager.failed_plugins)


if __name__ == "__main__":
    unittest.main()
