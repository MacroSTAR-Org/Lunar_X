import asyncio
import contextlib
import importlib
import os
import re
import sys
import threading
import time
from typing import Any

from .events import Event, Events, MessageEvent
from .logger import logger


class PluginManager:
    def __init__(self, bot, config: dict[str, Any], main_loop: asyncio.AbstractEventLoop):
        self.bot = bot
        self.config = config
        self.plugins_dir = "plugins"
        self.plugins: dict[str, Any] = {}
        self.enabled_plugins: list[str] = []
        self.failed_plugins: dict[str, str] = {}
        self.file_timestamps: dict[str, float] = {}
        self.monitoring_enabled = False
        self.main_loop = main_loop

        if not os.path.exists(self.plugins_dir):
            os.makedirs(self.plugins_dir)

        self.initialize_file_timestamps()

    def start_file_monitoring(self):
        self.monitoring_enabled = True
        logger.info(f"开始自动监控插件目录: {self.plugins_dir}", logger_name="LunarPlugins")

        self.monitoring_thread = threading.Thread(
            target=self.monitor_plugin_files, name="monitor_plugin_files"
        )
        self.monitoring_thread.daemon = True
        self.monitoring_thread.start()

    def stop_file_monitoring(self):
        self.monitoring_enabled = False
        if hasattr(self, "monitoring_thread") and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=1)
            logger.info("文件监控线程已停止", logger_name="LunarPlugins")
        else:
            logger.info("文件监控线程未运行或已停止", logger_name="LunarPlugins")

    # 需要纳入热重载监控的文件：插件代码 + 私有配置
    _WATCH_SUFFIXES = (".py", ".json")

    def _scan_watch_files(self) -> dict[str, float]:
        """递归扫描 plugins/ 下所有需要监控的文件及其 mtime。

        必须递归：插件全部文件夹化之后，只看顶层 .py 等于没有热重载。
        同时纳入 .json，这样在 WebUI 里改插件配置也能触发重载。
        """
        found: dict[str, float] = {}
        for root, dirs, files in os.walk(self.plugins_dir):
            # 不进 __pycache__ 之类的目录
            dirs[:] = [d for d in dirs if not d.startswith("_") and d != "__pycache__"]
            for fn in files:
                # 只跳过 dunder（__init__.py 之类），插件自己的 _helpers.py
                # 这种私有模块也要监控，否则改了不会触发重载
                if not fn.endswith(self._WATCH_SUFFIXES) or fn.startswith("__"):
                    continue
                path = os.path.join(root, fn)
                try:
                    found[path] = os.stat(path).st_mtime
                except OSError:
                    continue  # 扫描过程中被删掉，忽略
        return found

    def initialize_file_timestamps(self):
        self.file_timestamps = self._scan_watch_files()

    def monitor_plugin_files(self):
        while self.monitoring_enabled:
            time.sleep(1)
            try:
                current = self._scan_watch_files()
            except Exception as e:
                logger.error(f"扫描插件目录时发生错误: {e}", logger_name="LunarPlugins")
                continue

            # 新增 / 删除 / 修改 都要触发重载。
            # 旧实现只比较已知文件的 mtime，新增插件和重命名（启用/禁用）都捕获不到。
            added = current.keys() - self.file_timestamps.keys()
            removed = self.file_timestamps.keys() - current.keys()
            changed = {
                p
                for p in current.keys() & self.file_timestamps.keys()
                if current[p] != self.file_timestamps[p]
            }

            if not (added or removed or changed):
                continue

            sample = next(iter(changed or added or removed))
            reason = "修改" if changed else ("新增" if added else "删除")
            logger.info(
                f"检测到插件文件{reason}: {sample}，准备重载插件", logger_name="LunarPlugins"
            )

            self.file_timestamps = current  # 先更新，避免重载期间重复触发
            if self.main_loop and self.main_loop.is_running():
                asyncio.run_coroutine_threadsafe(self.reload_plugins(), self.main_loop)
            else:
                logger.error("主事件循环未运行或未设置，无法重载插件。", logger_name="LunarPlugins")

    def load_plugins(self):
        logger.info("开始加载插件...", logger_name="LunarPlugins")

        self.plugins.clear()
        self.enabled_plugins.clear()
        self.failed_plugins.clear()

        plugin_candidates_to_load = []

        for item in os.listdir(self.plugins_dir):
            item_path = os.path.join(self.plugins_dir, item)

            if item.endswith(".py") and not item.startswith("_"):
                if item.startswith("d_"):
                    plugin_name = item[2:-3]
                    logger.warning(
                        f"插件 {plugin_name} (文件: {item}) 已禁用，跳过加载",
                        logger_name="LunarPlugins",
                    )
                    continue
                else:
                    plugin_name = item[:-3]
                plugin_candidates_to_load.append((plugin_name, item_path, "file"))

            elif os.path.isdir(item_path) and not item.startswith("_"):
                if item.startswith("d_"):
                    plugin_name = item[2:]
                    logger.warning(
                        f"插件 {plugin_name} (目录: {item}) 已禁用，跳过加载",
                        logger_name="LunarPlugins",
                    )
                    continue
                else:
                    plugin_name = item

                setup_path = os.path.join(item_path, "setup.py")
                if os.path.exists(setup_path):
                    plugin_candidates_to_load.append((plugin_name, setup_path, "dir"))
                else:
                    logger.warning(
                        f"目录插件 {item} 缺少 setup.py 文件，跳过加载", logger_name="LunarPlugins"
                    )

        for name, path, plugin_type in plugin_candidates_to_load:
            plugin_dir = os.path.dirname(path) if plugin_type == "dir" else None
            path_inserted = False
            try:
                if name in sys.modules:
                    del sys.modules[name]
                for module_name in list(sys.modules.keys()):
                    if module_name == name or re.match(rf"^{re.escape(name)}\..*", module_name):
                        del sys.modules[module_name]

                if plugin_dir:
                    # 只在本插件 exec 期间把它的目录挂到 sys.path，
                    # 结束后立刻摘掉：既避免路径无限堆积，也让不同插件里
                    # 的同名子模块（比如各自的 store.py）不会互相串
                    sys.path.insert(0, plugin_dir)
                    path_inserted = True

                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None:
                    error_msg = f"无法为 {name} 创建模块规范"
                    self.failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue

                modules_before = set(sys.modules)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                # 记下这次 exec 期间新产生、且文件位于本插件目录内的子模块。
                # 它们已经被插件代码绑定到自己的命名空间了，从 sys.modules 摘掉
                # 不影响运行，却能让下次重载真正拿到新代码（旧实现清不掉这些
                # 裸名模块，改了 store.py 重载后跑的还是老的）。
                owned = self._collect_owned_submodules(modules_before, plugin_dir, name)
                for sub in owned:
                    sys.modules.pop(sub, None)

                if not hasattr(module, "TRIGGHT_KEYWORD"):
                    error_msg = "缺少 TRIGGHT_KEYWORD 属性"
                    self.failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue

                trigger = getattr(module, "TRIGGHT_KEYWORD", "")
                priority = getattr(module, "PLT_ST", 999)
                help_message = getattr(module, "HELP_MESSAGE", "")

                plugin_info = {
                    "module": module,
                    "type": plugin_type,
                    "path": path,
                    "trigger": trigger,
                    "help": help_message,
                    "enabled": True,
                    "priority": priority,
                }

                self.plugins[name] = plugin_info
                self.enabled_plugins.append(name)

                logger.success(
                    f"成功加载插件: {name} (触发词: {trigger}, 优先级: {priority}, 状态: 启用)",
                    logger_name="LunarPlugins",
                )

            except Exception as e:
                error_msg = str(e)
                self.failed_plugins[name] = error_msg
                logger.error(f"加载插件 {name} 时发生错误: {e}", logger_name="LunarPlugins")
                import traceback

                logger.debug(
                    f"插件 {name} 加载失败详情:\n{traceback.format_exc()}",
                    logger_name="LunarPlugins",
                )
            finally:
                if path_inserted:
                    with contextlib.suppress(ValueError):
                        sys.path.remove(plugin_dir)

        self._sort_plugins_by_priority()
        logger.info(
            f"共加载 {len(self.plugins)} 个插件, 失败 {len(self.failed_plugins)} 个",
            logger_name="LunarPlugins",
        )

    @staticmethod
    def _collect_owned_submodules(
        modules_before: set, plugin_dir: str, plugin_name: str
    ) -> list[str]:
        """挑出本次 exec 新增的、且源文件位于该插件目录内的模块名"""
        if not plugin_dir:
            return []
        owned = []
        plugin_dir_abs = os.path.abspath(plugin_dir)
        for mod_name in set(sys.modules) - modules_before:
            if mod_name == plugin_name:
                continue
            mod = sys.modules.get(mod_name)
            mod_file = getattr(mod, "__file__", None)
            if not mod_file:
                continue
            try:
                if (
                    os.path.commonpath([os.path.abspath(mod_file), plugin_dir_abs])
                    == plugin_dir_abs
                ):
                    owned.append(mod_name)
            except ValueError:
                continue  # 不同盘符，commonpath 会抛
        return owned

    def _sort_plugins_by_priority(self):
        sorted_plugins = sorted(self.plugins.items(), key=lambda x: x[1]["priority"])
        self.plugins = dict(sorted_plugins)

    async def handle_event(self, event: Event, lunar) -> bool:

        lunar.plugin_logger.debug("=== 开始处理事件 ===")
        lunar.plugin_logger.debug(f"事件类型: {event.__class__.__name__}")
        lunar.plugin_logger.debug(f"是否命令: {event.is_command}")
        lunar.plugin_logger.debug(f"命令: {event.command}, 参数: {event.args}")
        if isinstance(event, MessageEvent):
            lunar.plugin_logger.debug(f"消息内容 (原始): '{event.get_text()}'")
        lunar.plugin_logger.debug(f"消息内容 (处理后): '{event.processed_text}'")
        lunar.plugin_logger.debug(f"配置触发词: '{lunar.config.get('trigger_keyword', '$')}'")
        lunar.plugin_logger.debug(f"可用插件数量: {len(self.plugins)}")

        for name, plugin in self.plugins.items():
            module = plugin["module"]

            original_print = __builtins__.get("print", print)
            try:

                def plugin_print(*args, _name=name, **kwargs):
                    sep = kwargs.get("sep", " ")
                    end = kwargs.get("end", "\n")
                    message = sep.join(str(arg) for arg in args) + end
                    if message.endswith("\n"):
                        message = message[:-1]
                    logger.info(message, logger_name=f"Plugins:{_name}")

                __builtins__["print"] = plugin_print

                if isinstance(event, (Events.LunarStartListen, Events.LunarStopListen)):
                    if hasattr(module, "on_lunar_event"):
                        lunar.plugin_logger.debug(
                            f"准备执行插件 {name} 的 on_lunar_event 方法 for {event.__class__.__name__}"
                        )
                        result = await module.on_lunar_event(event, lunar)
                        lunar.plugin_logger.debug(
                            f"插件 {name} (on_lunar_event) 执行结果: {result}"
                        )
                        if result:
                            lunar.plugin_logger.info(f"插件 {name} 处理了自定义事件并阻断后续处理")
                            return True
                    continue

                should_trigger = False
                if isinstance(event, MessageEvent):
                    trigger = plugin["trigger"]
                    if trigger == "Any":
                        should_trigger = True
                        lunar.plugin_logger.debug(f"永久触发插件 {name} 被触发")
                    elif event.is_command and trigger == event.command:
                        should_trigger = True
                        lunar.plugin_logger.debug(f"插件 {name} 被命令 '{event.command}' 触发")
                    else:
                        full_trigger = lunar.config.get("trigger_keyword", "/") + trigger
                        if event.get_text() and event.get_text().startswith(full_trigger):
                            should_trigger = True
                            event.processed_text = event.get_text()[len(full_trigger) :].strip()
                            lunar.plugin_logger.debug(
                                f"插件 {name} 被消息触发，消息以 '{full_trigger}' 开头"
                            )
                elif plugin["trigger"] == "Any":
                    should_trigger = True

                if should_trigger and hasattr(module, "on_message"):
                    lunar.plugin_logger.debug(
                        f"准备执行插件 {name} 的 on_message 方法 for {event.__class__.__name__}"
                    )
                    result = await module.on_message(event, lunar)
                    lunar.plugin_logger.debug(f"插件 {name} (on_message) 执行结果: {result}")

                    if result:
                        lunar.plugin_logger.info(f"插件 {name} 处理了事件并阻断后续处理")
                        return True

            except Exception as e:
                lunar.plugin_logger.error(f"插件 {name} 处理事件时发生错误: {e}")
                import traceback

                lunar.plugin_logger.error(f"错误详情: {traceback.format_exc()}")
            finally:
                __builtins__["print"] = original_print

        lunar.plugin_logger.debug("=== 事件处理结束 ===")
        return False

    async def reload_plugins(self) -> bool:
        logger.info("开始重载插件...", logger_name="LunarPlugins")

        try:
            modules_to_delete = []
            for module_name in sys.modules:
                if module_name.startswith("plugins.") or module_name in self.plugins:
                    modules_to_delete.append(module_name)

            for module_name in modules_to_delete:
                if module_name in sys.modules:
                    del sys.modules[module_name]

            self.plugins.clear()
            self.enabled_plugins.clear()
            self.failed_plugins.clear()

            self.load_plugins()
            self.initialize_file_timestamps()

            logger.info("插件重载完成", logger_name="LunarPlugins")
            return True

        except Exception as e:
            logger.error(f"重载插件失败: {e}", logger_name="LunarPlugins")
            import traceback

            logger.error(f"错误详情: {traceback.format_exc()}", logger_name="LunarPlugins")
            return False

    async def enable_plugin(self, plugin_name: str) -> bool:
        logger.info(f"尝试启用插件: {plugin_name}", logger_name="LunarPlugins")

        enabled_path_py = os.path.join(self.plugins_dir, f"{plugin_name}.py")
        enabled_path_dir = os.path.join(self.plugins_dir, plugin_name)

        if os.path.exists(enabled_path_py) or os.path.exists(enabled_path_dir):
            if plugin_name in self.plugins:
                logger.info(f"插件 {plugin_name} 已经启用且已加载", logger_name="LunarPlugins")
                return True
            elif plugin_name in self.failed_plugins:
                logger.info(
                    f"插件 {plugin_name} 已经启用但加载失败，尝试重载", logger_name="LunarPlugins"
                )
                await self.reload_plugins()
                return plugin_name in self.plugins
            else:
                logger.info(
                    f"插件 {plugin_name} 文件/目录已存在且未禁用，但未加载，尝试重载",
                    logger_name="LunarPlugins",
                )
                await self.reload_plugins()
                return plugin_name in self.plugins

        disabled_path_py = os.path.join(self.plugins_dir, f"d_{plugin_name}.py")
        disabled_path_dir = os.path.join(self.plugins_dir, f"d_{plugin_name}")

        found_path = None
        if os.path.exists(disabled_path_py):
            found_path = disabled_path_py
            new_path = enabled_path_py
        elif os.path.exists(disabled_path_dir):
            found_path = disabled_path_dir
            new_path = enabled_path_dir

        if not found_path:
            logger.warning(
                f"未找到禁用状态的插件文件或目录 (d_{plugin_name}.py 或 d_{plugin_name})",
                logger_name="LunarPlugins",
            )
            return False

        try:
            os.rename(found_path, new_path)
            logger.success(
                f"已将 {found_path} 重命名为 {new_path} 以启用插件", logger_name="LunarPlugins"
            )
            await self.reload_plugins()
            return True
        except OSError as e:
            logger.error(
                f"启用插件 {plugin_name} 失败，重命名文件/目录时发生错误: {e}",
                logger_name="LunarPlugins",
            )
            return False
        except Exception as e:
            logger.error(f"启用插件 {plugin_name} 失败: {e}", logger_name="LunarPlugins")
            return False

    async def disable_plugin(self, plugin_name: str) -> bool:
        logger.info(f"尝试禁用插件: {plugin_name}", logger_name="LunarPlugins")

        disabled_path_py = os.path.join(self.plugins_dir, f"d_{plugin_name}.py")
        disabled_path_dir = os.path.join(self.plugins_dir, f"d_{plugin_name}")

        if os.path.exists(disabled_path_py) or os.path.exists(disabled_path_dir):
            logger.info(
                f"插件 {plugin_name} 文件/目录已存在且已禁用，无需重命名，尝试重载以确保其未加载",
                logger_name="LunarPlugins",
            )
            await self.reload_plugins()
            return True

        enabled_path_py = os.path.join(self.plugins_dir, f"{plugin_name}.py")
        enabled_path_dir = os.path.join(self.plugins_dir, plugin_name)

        found_path = None
        if os.path.exists(enabled_path_py):
            found_path = enabled_path_py
            new_path = disabled_path_py
        elif os.path.exists(enabled_path_dir):
            found_path = enabled_path_dir
            new_path = disabled_path_dir

        if not found_path:
            logger.warning(
                f"未找到启用状态的插件文件或目录 ({plugin_name}.py 或 {plugin_name})",
                logger_name="LunarPlugins",
            )
            return False

        try:
            os.rename(found_path, new_path)
            logger.success(
                f"已将 {found_path} 重命名为 {new_path} 以禁用插件", logger_name="LunarPlugins"
            )
            await self.reload_plugins()
            return True
        except OSError as e:
            logger.error(
                f"禁用插件 {plugin_name} 失败，重命名文件/目录时发生错误: {e}",
                logger_name="LunarPlugins",
            )
            return False
        except Exception as e:
            logger.error(f"禁用插件 {plugin_name} 失败: {e}", logger_name="LunarPlugins")
            return False

    def get_plugin_list(self) -> dict[str, Any]:
        all_plugins_info = {"enabled_on_disk": {}, "disabled_on_disk": {}}

        for item in os.listdir(self.plugins_dir):
            item_path = os.path.join(self.plugins_dir, item)
            name = None
            is_enabled_on_disk = True

            if item.endswith(".py") and not item.startswith("_"):
                if item.startswith("d_"):
                    name = item[2:-3]
                    is_enabled_on_disk = False
                else:
                    name = item[:-3]
            elif os.path.isdir(item_path) and not item.startswith("_"):
                if item.startswith("d_"):
                    name = item[2:]
                    is_enabled_on_disk = False
                else:
                    name = item
                if not os.path.exists(os.path.join(item_path, "setup.py")):
                    name = None

            if name:
                if is_enabled_on_disk:
                    if name in self.plugins:
                        plugin_info = self.plugins[name].copy()
                        plugin_info.pop("module", None)
                        plugin_info.pop("path", None)
                        plugin_info["enabled"] = True
                        all_plugins_info["enabled_on_disk"][name] = plugin_info
                    elif name in self.failed_plugins:
                        info = {
                            "trigger": "N/A",
                            "priority": 999,
                            "enabled": True,
                            "help": "加载失败",
                            "error": self.failed_plugins[name],
                        }
                        all_plugins_info["enabled_on_disk"][name] = info
                    else:
                        info = {
                            "trigger": "未知",
                            "priority": 999,
                            "enabled": True,
                            "help": "未加载 (可能在上次加载时被跳过)",
                        }
                        all_plugins_info["enabled_on_disk"][name] = info
                else:
                    info = {
                        "trigger": "N/A (已禁用)",
                        "priority": 999,
                        "enabled": False,
                        "help": "此插件已禁用，未加载",
                    }
                    all_plugins_info["disabled_on_disk"][name] = info

        return all_plugins_info
