import asyncio
import contextlib
import importlib.util
import inspect
import os
import re
import shutil
import sys
import threading
import time
from typing import Any

from core.security import is_safe_plugin_name

from .events import Event, Events, MessageEvent
from .logger import logger


class PluginManager:
    def __init__(self, bot, config: dict[str, Any], main_loop: asyncio.AbstractEventLoop):
        self.bot = bot
        self.config = config
        # 不依赖进程 cwd：否则从其他目录启动时插件会“凭空消失”
        self.plugins_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "plugins"
        )
        self.plugins: dict[str, Any] = {}
        self.enabled_plugins: list[str] = []
        self.failed_plugins: dict[str, str] = {}
        self.file_timestamps: dict[str, float] = {}
        self.monitoring_enabled = False
        self.monitoring_thread: threading.Thread | None = None
        self.main_loop = main_loop
        self._reload_lock = asyncio.Lock()

        if not os.path.exists(self.plugins_dir):
            os.makedirs(self.plugins_dir)

        self.initialize_file_timestamps()

    def start_file_monitoring(self):
        if self.monitoring_enabled and self.monitoring_thread and self.monitoring_thread.is_alive():
            return
        self.monitoring_enabled = True
        logger.info(f"开始自动监控插件目录: {self.plugins_dir}", logger_name="LunarPlugins")

        self.monitoring_thread = threading.Thread(
            target=self.monitor_plugin_files, name="monitor_plugin_files"
        )
        self.monitoring_thread.daemon = True
        self.monitoring_thread.start()

    def stop_file_monitoring(self):
        self.monitoring_enabled = False
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=1)
            logger.info("文件监控线程已停止", logger_name="LunarPlugins")
        else:
            logger.info("文件监控线程未运行或已停止", logger_name="LunarPlugins")

    # 只监控代码和清单。插件私有 config.json 由 bot.plugin_config() 按
    # mtime 直接读取，保存它不需要重载整个插件目录。
    _WATCH_SUFFIXES = (".py",)
    _WATCH_EXACT_NAMES = frozenset({"plugin.json"})

    def _scan_watch_files(self) -> dict[str, float]:
        """递归扫描 plugins/ 下所有需要监控的文件及其 mtime。

        必须递归：插件全部文件夹化之后，只看顶层 .py 等于没有热重载。
        """
        found: dict[str, float] = {}
        for root, dirs, files in os.walk(self.plugins_dir):
            # 不进 __pycache__ 之类的目录
            dirs[:] = [d for d in dirs if not d.startswith("_") and d != "__pycache__"]
            for fn in files:
                if fn.startswith("__"):
                    continue
                if not (fn.endswith(self._WATCH_SUFFIXES) or fn in self._WATCH_EXACT_NAMES):
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

        # 两阶段加载：先构建新的插件表，全部处理完再一次性替换。
        # 旧实现先 clear() 再加载，一旦 load 中途异常，Bot 会处于“零插件”状态。
        plugins: dict[str, Any] = {}
        enabled_plugins: list[str] = []
        failed_plugins: dict[str, str] = {}

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
                plugin_name = item

                setup_path = os.path.join(item_path, "setup.py")
                if os.path.exists(setup_path):
                    plugin_candidates_to_load.append((plugin_name, setup_path, "dir"))
                else:
                    logger.warning(
                        f"目录插件 {item} 缺少 setup.py 文件，跳过加载", logger_name="LunarPlugins"
                    )

        for name, path, plugin_type in plugin_candidates_to_load:
            if not is_safe_plugin_name(name):
                failed_plugins[name] = "插件名不合法或与标准库/框架保留名冲突"
                logger.error(
                    f"拒绝加载插件 {name!r}: 插件名不合法或属于保留名",
                    logger_name="LunarPlugins",
                )
                continue

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

                self._purge_plugin_bytecode(path, plugin_dir)
                spec = importlib.util.spec_from_file_location(name, path)
                if spec is None:
                    error_msg = f"无法为 {name} 创建模块规范"
                    failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue
                if spec.loader is None:
                    error_msg = f"无法为 {name} 创建模块加载器"
                    failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue

                modules_before = set(sys.modules)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                # 记下这次 exec 期间新产生、且文件位于本插件目录内的子模块。
                # 它们已经被插件代码绑定到自己的命名空间了，从 sys.modules 摘掉
                # 不影响运行，却能让下次重载真正拿到新代码。
                owned = self._collect_owned_submodules(modules_before, plugin_dir, name)
                for sub in owned:
                    sys.modules.pop(sub, None)

                if not hasattr(module, "TRIGGHT_KEYWORD"):
                    error_msg = "缺少 TRIGGHT_KEYWORD 属性"
                    failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue

                trigger = getattr(module, "TRIGGHT_KEYWORD", "")
                if not isinstance(trigger, str) or not trigger.strip():
                    error_msg = "TRIGGHT_KEYWORD 必须是非空字符串"
                    failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue

                try:
                    priority = int(getattr(module, "PLT_ST", 999))
                except (TypeError, ValueError):
                    error_msg = "PLT_ST 必须是整数"
                    failed_plugins[name] = error_msg
                    logger.warning(f"插件 {name} {error_msg}，跳过加载", logger_name="LunarPlugins")
                    continue

                help_message = getattr(module, "HELP_MESSAGE", "")
                if not isinstance(help_message, str):
                    help_message = str(help_message)

                plugins[name] = {
                    "module": module,
                    "type": plugin_type,
                    "path": path,
                    "trigger": trigger,
                    "help": help_message,
                    "enabled": True,
                    "priority": priority,
                }
                enabled_plugins.append(name)

                logger.success(
                    f"成功加载插件: {name} (触发词: {trigger}, 优先级: {priority}, 状态: 启用)",
                    logger_name="LunarPlugins",
                )

            except Exception as e:
                failed_plugins[name] = str(e)
                sys.modules.pop(name, None)
                logger.error(f"加载插件 {name} 时发生错误: {e}", logger_name="LunarPlugins")
                import traceback

                logger.debug(
                    f"插件 {name} 加载失败详情:\n{traceback.format_exc()}",
                    logger_name="LunarPlugins",
                )
            finally:
                if path_inserted and plugin_dir is not None:
                    with contextlib.suppress(ValueError):
                        sys.path.remove(plugin_dir)

        # 一次性切换，旧插件表要么整体保留，要么整体更新
        self.plugins = dict(sorted(plugins.items(), key=lambda item: item[1]["priority"]))
        self.enabled_plugins = enabled_plugins
        self.failed_plugins = failed_plugins
        if getattr(self, "bot", None) is not None:
            self.bot.plugins = self.plugins

        logger.info(
            f"共加载 {len(self.plugins)} 个插件, 失败 {len(self.failed_plugins)} 个",
            logger_name="LunarPlugins",
        )

    @staticmethod
    def _purge_plugin_bytecode(path: str, plugin_dir: str | None) -> None:
        """删掉插件自己的 .pyc 缓存。

        Python 判断 .pyc 是否过期用的是「源文件 mtime（秒）+ 大小」。
        同一秒内修改插件且文件大小不变时（改个数字/字符串），会继续加载旧
        字节码，表现为“热重载没生效”。这里在每次加载前清掉缓存，强制重编译。
        """
        if plugin_dir:
            for root, dirs, _files in os.walk(plugin_dir):
                for name in list(dirs):
                    if name == "__pycache__":
                        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                        dirs.remove(name)
            return

        cache_dir = os.path.join(os.path.dirname(path), "__pycache__")
        if not os.path.isdir(cache_dir):
            return
        stem = os.path.splitext(os.path.basename(path))[0] + "."
        for name in os.listdir(cache_dir):
            if name.startswith(stem) and name.endswith(".pyc"):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(cache_dir, name))

    @staticmethod
    def _collect_owned_submodules(
        modules_before: set, plugin_dir: str | None, plugin_name: str
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

    async def _unload_modules(self, plugins: dict[str, Any]) -> None:
        """调用插件可选的 on_unload，用于关闭 aiohttp session / SQLite 连接。"""
        for name, plugin in tuple(plugins.items()):
            module = plugin.get("module")
            hook = getattr(module, "on_unload", None)
            if not callable(hook):
                continue
            try:
                result = hook(self.bot)
                if inspect.isawaitable(result):
                    await result
            except Exception as e:
                logger.error(
                    f"插件 {name} 卸载时发生错误: {e}", logger_name="LunarPlugins"
                )

    async def unload_plugins(self) -> None:
        await self._unload_modules(self.plugins)

    def _plugin_timeout(self) -> float | None:
        """单个插件 handler 的最长执行时间；<=0 表示关闭超时保护。"""
        try:
            timeout = float(self.config.get("plugin_timeout", 30))
        except (TypeError, ValueError):
            timeout = 30.0
        return timeout if timeout > 0 else None

    async def _invoke_handler(self, name: str, method: str, coro: Any) -> Any:
        timeout = self._plugin_timeout()
        if timeout is None:
            return await coro
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError:
            # 卡死的插件只影响自己所在的会话；取消后继续让后续插件处理
            logger.error(
                f"插件 {name}.{method} 处理事件超时（>{timeout:g}s），已取消",
                logger_name="LunarPlugins",
            )
            return False

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

        # 用快照分发：热重载会在 await 期间替换 self.plugins，
        # 直接迭代原字典会触发 “dictionary changed size during iteration”。
        for name, plugin in tuple(self.plugins.items()):
            module = plugin["module"]

            try:
                if isinstance(event, (Events.LunarStartListen, Events.LunarStopListen)):
                    if hasattr(module, "on_lunar_event"):
                        lunar.plugin_logger.debug(
                            f"准备执行插件 {name} 的 on_lunar_event 方法 for {event.__class__.__name__}"
                        )
                        result = await self._invoke_handler(
                            name,
                            "on_lunar_event",
                            module.on_lunar_event(event, lunar),
                        )
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
                    elif not event.is_command:
                        # 兼容“消息以 $trigger 开头”的旧写法，但必须有词边界，
                        # 否则 $pinger 会误触发 trigger="ping" 的插件。
                        full_trigger = lunar.config.get("trigger_keyword", "$") + trigger
                        text = event.get_text()
                        if text == full_trigger or text.startswith(
                            (full_trigger + " ", full_trigger + "\u3000")
                        ):
                            should_trigger = True
                            event.processed_text = text[len(full_trigger) :].strip()
                            lunar.plugin_logger.debug(
                                f"插件 {name} 被消息触发，消息以 '{full_trigger}' 开头"
                            )
                elif plugin["trigger"] == "Any":
                    should_trigger = True

                if should_trigger and hasattr(module, "on_message"):
                    lunar.plugin_logger.debug(
                        f"准备执行插件 {name} 的 on_message 方法 for {event.__class__.__name__}"
                    )
                    result = await self._invoke_handler(
                        name,
                        "on_message",
                        module.on_message(event, lunar),
                    )
                    lunar.plugin_logger.debug(f"插件 {name} (on_message) 执行结果: {result}")

                    if result:
                        lunar.plugin_logger.info(f"插件 {name} 处理了事件并阻断后续处理")
                        return True

            except Exception as e:
                lunar.plugin_logger.error(f"插件 {name} 处理事件时发生错误: {e}")
                import traceback

                lunar.plugin_logger.error(f"错误详情: {traceback.format_exc()}")

        lunar.plugin_logger.debug("=== 事件处理结束 ===")
        return False

    async def reload_plugins(self) -> bool:
        async with self._reload_lock:
            logger.info("开始重载插件...", logger_name="LunarPlugins")

            try:
                old_plugins = dict(self.plugins)

                for module_name in list(sys.modules):
                    if module_name.startswith("plugins.") or module_name in old_plugins:
                        del sys.modules[module_name]

                # load_plugins 内部两阶段构建并一次性替换；
                # 加载失败时 self.plugins 仍是旧表，不会出现“零插件”。
                self.load_plugins()
                self.initialize_file_timestamps()

                # 新表已生效，再释放旧模块持有的长连接
                await self._unload_modules(old_plugins)

                # 重新触发启动钩子：定时任务等插件需要在新模块里恢复状态
                await self.handle_event(Events.LunarStartListen(), self.bot)

                logger.info("插件重载完成", logger_name="LunarPlugins")
                return True

            except Exception as e:
                logger.error(f"重载插件失败: {e}", logger_name="LunarPlugins")
                import traceback

                logger.error(f"错误详情: {traceback.format_exc()}", logger_name="LunarPlugins")
                return False

    async def enable_plugin(self, plugin_name: str) -> bool:
        if not is_safe_plugin_name(plugin_name):
            logger.warning(f"拒绝启用非法插件名: {plugin_name!r}", logger_name="LunarPlugins")
            return False
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
        if not is_safe_plugin_name(plugin_name):
            logger.warning(f"拒绝禁用非法插件名: {plugin_name!r}", logger_name="LunarPlugins")
            return False
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
