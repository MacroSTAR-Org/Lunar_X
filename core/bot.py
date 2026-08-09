import asyncio
import json
import os
import sys
import time
from typing import Dict, Any, List, Optional, Union
from core.diy import DiyAPI
from .connection import WebSocketConnection
from .plugin_manager import PluginManager
from .logger import logger
from .message import MessageBuilder, ReplyUtils, BaseSegment, TextSegment, ImageSegment, AtSegment, ReplySegment, ForwardNodeSegment
from .events import Event, MessageEvent, PrivateMessageEvent, GroupMessageEvent, NoticeEvent, RequestEvent, MetaEvent, EventFactory, Events

class LunarBot:
    def __init__(self, config: Dict[str, Any], main_loop: asyncio.AbstractEventLoop):
        self.config = config
        logger.configure_from_config(config)
        self.protocol_name = config.get('protocol', 'onebot_v11')
        if self.protocol_name == 'milky':
            from .transport import MilkyTransport
            from .milky_adapter import MilkyAdapter
            milky_server = config.get('milky_server', 'http://127.0.0.1:3803')
            milky_token = config.get('milky_token') or config.get('token')
            self.connection = MilkyTransport(milky_server, milky_token, max_retries=5)
            self.protocol = MilkyAdapter(self)
            logger.info(f"协议模式: Milky (协议端: {milky_server})")
        else:
            from .onebot_adapter import OneBotAdapter
            self.connection = WebSocketConnection(
                config['ws_server'],
                config.get('token'),
                max_retries=5
            )
            self.protocol = OneBotAdapter(self)
            logger.info("协议模式: OneBot v11")
        self.plugin_manager = PluginManager(self, config, main_loop) 
        self.message_count = {'sent': 0, 'received': 0}
        self.start_time = time.time()
        self.restart_info = None
        self.plugins = self.plugin_manager.plugins
        self.reply = ReplyUtils(self)
        self.plugin_logger = logger.get_logger('LunarPlugins')
        self.msg = MessageBuilder(self)
        self.diy = DiyAPI(self)
        # 插件私有配置的 mtime 缓存：{插件名: (config.json 的 mtime, 合并后的配置)}
        # 有它才能做到 WebUI 改完配置下一条消息就生效，不必重启
        self._plugin_cfg_cache: Dict[str, Any] = {}
        self._register_native_commands()

    # ------------------------------------------------------------------
    # 插件私有配置 / 数据目录
    # ------------------------------------------------------------------
    @property
    def _root_dir(self) -> str:
        """框架根目录（core/ 的上一级），不依赖进程 cwd"""
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def plugin_dir(self, name: str) -> Optional[str]:
        """插件自己的目录；单文件插件没有目录，返回 None"""
        from .plugin_config import plugin_dir as _pd
        return _pd(os.path.join(self._root_dir, 'plugins'), name)

    def plugin_config(self, name: str) -> Dict[str, Any]:
        """读插件私有配置（plugin.json 的 default 打底 + config.json 覆盖）。

        插件里推荐写 `lunar.plugin_config(__name__)`——框架用
        `spec_from_file_location(插件名, ...)` 注册模块，所以 `__name__` 就是插件名。

        按 config.json 的 mtime 缓存：文件没动就走缓存，动了就重读。
        这样 WebUI 保存后下一条消息即可读到新值，无需重启。
        """
        from . import plugin_config as pc
        plugins_root = os.path.join(self._root_dir, 'plugins')
        d = pc.plugin_dir(plugins_root, name)
        if not d:
            return {}

        cfg_path = os.path.join(d, pc.CONFIG_FILE)
        try:
            mtime = os.stat(cfg_path).st_mtime
        except OSError:
            mtime = 0.0                      # config.json 还没建出来，只用 default

        cached = self._plugin_cfg_cache.get(name)
        if cached and cached[0] == mtime:
            return cached[1]

        merged = pc.load_config(plugins_root, name)
        self._plugin_cfg_cache[name] = (mtime, merged)
        return merged

    def plugin_data_dir(self, name: str) -> str:
        """插件的数据目录 <root>/data/<name>/，自动创建。

        统一走这里，插件就不用再各自 dirname 往上数层数——
        以前 quote.py 数两层、tools/store.py 数三层，插件一挪目录路径就断。
        """
        path = os.path.join(self._root_dir, 'data', name)
        os.makedirs(path, exist_ok=True)
        return path

    async def gen_message(self, data: Union[Dict, List]) -> List[BaseSegment]:
        return self.msg.gen_message(data)
    def _convert_segments_to_dicts(self, data: Union[Dict, List, Any]) -> Union[Dict, List, Any]:
        if isinstance(data, BaseSegment):
            return data.to_dict()
        elif isinstance(data, dict):
            return {k: self._convert_segments_to_dicts(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._convert_segments_to_dicts(item) for item in data]
        else:
            return data
    async def _diy_call(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        try:
            processed_params = self._convert_segments_to_dicts(params)
            response_data = await self.protocol.call_api(action, processed_params)
            
            if response_data:
                if response_data.get('status') == 'ok':
                    logger.success(f"调用自定义API成功: {action}, 参数: {params}, 响应: {response_data}")
                    return {'status': 'ok', 'data': response_data.get('data'), 'raw_response': response_data}
                else:
                    error_msg = response_data.get('msg') or response_data.get('message', '未知错误')
                    logger.error(f"调用自定义API失败 (服务器响应错误): {action}, 错误信息: {error_msg}, 响应: {response_data}")
                    return {'status': 'failed', 'msg': error_msg, 'raw_response': response_data}
            else:
                logger.error(f"调用自定义API失败: {action}, 未收到服务器响应")
                return {'status': 'failed', 'msg': '未收到服务器响应'}
            
        except TimeoutError:
            logger.error(f"调用自定义API {action} 超时，参数: {params}")
            return {'status': 'timeout', 'msg': '等待服务器响应超时'}
        except Exception as e:
            logger.error(f"调用自定义API {action} 时发生错误: {e}, 参数: {params}")
            return {'status': 'error', 'msg': str(e)}

    async def _handle_event(self, event_data: Dict[str, Any]):
        if self.protocol_name == 'milky':
            event = self.protocol.parse_event(event_data)
        else:
            event = EventFactory.create_event(event_data, self.msg)
        if isinstance(event, MessageEvent):
            await self._handle_message_event(event)
        elif isinstance(event, NoticeEvent):
            await self._handle_notice_event(event)
        elif isinstance(event, RequestEvent):
            await self._handle_request_event(event)
        elif isinstance(event, MetaEvent):
            await self._handle_meta_event(event)
        else:
            event.log_event()
            await self._handle_plugin_event(event)

    async def _handle_message_event(self, event: MessageEvent):
        event.log_event()
        if isinstance(event, GroupMessageEvent):
            self.message_count['received'] += 1
        elif isinstance(event, PrivateMessageEvent):
            self.message_count['received'] += 1
        
        trigger_keyword = self.config.get('trigger_keyword', '/')
        # 命令判定仅基于文本段拼接：忽略 reply/at/图片等非文本段，
        # 否则引用（回复）消息开头的 "[回复]" 占位符会让 "$xxx" 判定失败
        message_text = ''.join(seg.text for seg in event.message if isinstance(seg, TextSegment))
        
        logger.debug(f"处理消息事件: 原始消息='{message_text}', 触发词='{trigger_keyword}'")

        if message_text and message_text.startswith(trigger_keyword):
            command_full = message_text[len(trigger_keyword):].strip()
            parts = command_full.split(" ", 1)
            cmd = parts[0]
            args = parts[1] if len(parts) > 1 else ""

            event.is_command = True
            event.command = cmd
            event.args = args
            event.processed_text = args
            
            logger.debug(f"识别为命令: cmd='{cmd}', args='{args}'")

            if cmd in self.native_commands:
                logger.info(f"执行原生命令: {cmd}")
                handled_by_native = await self.native_commands[cmd](args, event)
                if handled_by_native:
                    logger.debug(f"原生命令 {cmd} 已处理事件并阻断后续处理")
                    return
            else:
                logger.debug(f"命令 '{cmd}' 未在原生命令中找到，将尝试插件处理")

            await self._handle_plugin_event(event)
            return
        
        event.processed_text = message_text
        logger.debug(f"未识别为命令，将消息事件交给插件处理: '{event.processed_text}'")
        await self._handle_plugin_event(event)

    async def get_message_detail(self, message_id: int):
        try:
            result = await self.diy.get_msg(message_id=message_id)
            return result
        except Exception as e:
            logger.error(f"获取消息详情失败: {e}")
            return None
            
    async def _handle_notice_event(self, event: NoticeEvent):
        event.log_event()
        await self._handle_plugin_event(event)

    async def _handle_request_event(self, event: RequestEvent):
        event.log_event()
        await self._handle_plugin_event(event)
        
    async def _handle_meta_event(self, event: MetaEvent):
        event.log_event()
        await self._handle_plugin_event(event)

    async def _handle_plugin_event(self, event: Event):
        try:
            result = await self.plugin_manager.handle_event(event, self)
            if result:
                logger.debug("插件已处理事件并阻断后续处理")
                
        except Exception as e:
            logger.error(f"处理插件事件时发生错误: {e}")

    def _register_native_commands(self):
        self.native_commands = {
            '添加管理员': self._add_manager,
            '删除管理员': self._remove_manager,
            '查看管理员': self._list_managers,
            '启用插件': self._enable_plugin,
            '禁用插件': self._disable_plugin,
            '重启': self._restart_bot,
            '重载插件': self._reload_plugins,
            '帮助': self._show_help,
            '查看插件': self._list_plugins,
            '消息统计': self._message_stats
        }
    
    def _check_permission(self, user_id: int, required_level: str = 'manager') -> bool:
        root_user = self.config.get('root_user')
        if user_id == root_user:
            return True
        if required_level == 'super':
            return user_id in self.config.get('super_users', [])
        else:
            return (user_id in self.config.get('manager_users', []) or 
                    user_id in self.config.get('super_users', []))
    
    async def _show_help(self, args: str, event: Event) -> bool:
        logger.info(f"调用 _show_help 方法，args='{args}'")
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        help_message = ""
        if not args.strip():
            help_message = self._build_help_message()
        else:
            help_message = self._build_plugin_detail_help(args.strip())
        
        if not help_message:
            help_message = "未能生成帮助信息，请检查配置或插件状态。"
            logger.warning("生成的帮助信息为空。")

        if group_id:
            logger.info(f"向群 {group_id} 发送帮助信息。")
            await self.send(help_message, group_id=group_id)
        else:
            logger.info(f"向用户 {user_id} 发送帮助信息。")
            await self.send(help_message, user_id=user_id)
        
        return True

    def _build_help_message(self):
        trigger = self.config.get('trigger_keyword', '/')
        native_help = "Lunar X 帮助菜单\n\n"
        native_help += "🔧 原生命令:\n"
        native_help += f"{trigger}添加管理员 <用户ID> - 添加管理员\n"
        native_help += f"{trigger}删除管理员 <用户ID> - 删除管理员\n"
        native_help += f"{trigger}查看管理员 - 查看管理员列表\n"
        native_help += f"{trigger}启用插件 <插件名> - 启用插件\n"
        native_help += f"{trigger}禁用插件 <插件名> - 禁用插件\n"
        native_help += f"{trigger}重启 - 重启BOT\n"
        native_help += f"{trigger}查看插件 - 查看插件列表\n"
        native_help += f"{trigger}重载插件 - 重载所有插件\n"
        native_help += f"{trigger}帮助 - 显示此帮助菜单\n"
        native_help += f"{trigger}消息统计 - 查看消息统计\n\n"

        plugin_help = "🧩 插件功能:\n"
        normal_plugin_count = 0
        any_trigger_plugins = []
        
        current_plugins_info = self.plugin_manager.get_plugin_list()
        
        
        for name, plugin_info in current_plugins_info['enabled_on_disk'].items():
            
            if 'error' not in plugin_info and plugin_info.get('help') != '未加载 (可能在上次加载时被跳过)':
                if plugin_info['trigger'] == 'Any':
                    any_trigger_plugins.append(name)
                else:
                    plugin_help += f"• {trigger}{plugin_info['trigger']} - {plugin_info['help']}\n"
                    normal_plugin_count += 1
        
        if normal_plugin_count == 0:
            plugin_help += "暂无普通插件\n"

        if any_trigger_plugins:
            plugin_help += f"\n⚡ 永久触发插件 ({len(any_trigger_plugins)}个):\n"
            plugin_help += "这些插件会对所有消息做出响应\n"
            for plugin_name in any_trigger_plugins:
                detail = self.plugin_manager.plugins.get(plugin_name)
                if detail:
                    plugin_help += f"• {plugin_name} - {detail['help']}\n"
        
        return native_help + plugin_help

    def _build_plugin_detail_help(self, plugin_name: str):
        found_plugin = None
        for name, plugin in self.plugin_manager.plugins.items():
            if name.lower() == plugin_name.lower() or plugin['trigger'].lower() == plugin_name.lower():
                found_plugin = plugin
                break
        
        if found_plugin:
            trigger = self.config.get('trigger_keyword', '/')
            detail_help = f"🧩 插件详情: {found_plugin['module'].__name__}\n\n"
            
            if found_plugin['trigger'] == 'Any':
                detail_help += f"📝 触发方式: 永久触发（对所有消息响应）\n"
            else:
                detail_help += f"📝 触发方式: {trigger}{found_plugin['trigger']}\n"
            
            detail_help += f"📋 帮助信息: {found_plugin['help']}\n"
            
            if found_plugin['trigger'] != 'Any':
                detail_help += f"🔧 使用方法: {trigger}{found_plugin['trigger']} [参数]\n"
            
            detail_help += f"✅ 状态: 已启用并加载\n"
            detail_help += f"📁 类型: {found_plugin['type']}\n"
            return detail_help
        
        all_plugins_status = self.plugin_manager.get_plugin_list()
        
        for name, info in all_plugins_status['disabled_on_disk'].items():
            if name.lower() == plugin_name.lower():
                return f"插件 {plugin_name} 已禁用 (文件/目录名为 d_{name}.py 或 d_{name})，未加载。"
        
        for name, info in all_plugins_status['enabled_on_disk'].items():
            if name.lower() == plugin_name.lower() and 'error' in info:
                return f"插件 {plugin_name} 启用状态，但加载失败: {info['error']}"
            elif name.lower() == plugin_name.lower() and info.get('help') == '未加载 (可能在上次加载时被跳过)':
                 return f"插件 {plugin_name} 启用状态，但未加载 (可能在上次加载时被跳过)。"

        return f"未找到插件: {plugin_name} (可能不存在或已被禁用)"

    async def _reload_plugins(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'manager'):
            response = "权限不足，只有管理员才能重载插件"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        try:
            success = await self.plugin_manager.reload_plugins()
            
            if success:
                response = f"外部插件后端重载已完成！\n发送 {self.config.get('trigger_keyword')}帮助 来知道更多！"
                self.plugins = self.plugin_manager.plugins
            else:
                response = "插件重载失败"
            
        except Exception as e:
            response = f"插件重载失败: {str(e)}"
            logger.error(f"重载插件时发生错误: {e}")
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True

    async def run(self):
        logger.info("Lunar X 机器人启动中...")
        if not await self.connection.connect():
            logger.error("连接失败，退出程序")
            return
        await self._handle_restart_info()
        self.plugin_manager.load_plugins()
        self.plugins = self.plugin_manager.plugins
        if self.config.get('auto_reload_plugins') == True:
            self.plugin_manager.start_file_monitoring()

        await self._handle_plugin_event(Events.LunarStartListen())
        
        await self._listen_events()
            
    async def _handle_restart_info(self):
        restart_info_file = os.path.abspath('restart_info.json')
        
        if not os.path.exists(restart_info_file):
            logger.info("未找到重启信息文件")
            return
        
        logger.info(f"找到重启信息文件: {restart_info_file}")
        
        try:
            with open(restart_info_file, 'r', encoding='utf-8') as f:
                restart_info = json.load(f)
            
            logger.info(f"重启信息内容: {restart_info}")
            
            if 'start_time' in restart_info:
                restart_time = time.time() - restart_info['start_time']
                success_message = f"重启成功，用时 {restart_time:.2f} 秒"

                if hasattr(self, 'connection') and self.connection.websocket:
                    if restart_info.get('message_type') == 'group' and 'group_id' in restart_info:
                        await self.send(success_message, group_id=restart_info['group_id'])
                        logger.info(f"向群 {restart_info['group_id']} 发送重启成功消息")
                    elif 'user_id' in restart_info:
                        await self.send(success_message, user_id=restart_info['user_id'])
                        logger.info(f"向用户 {restart_info['user_id']} 发送重启成功消息")
                    
                    logger.info(f"重启成功，用时 {restart_time:.2f} 秒")
            
            os.remove(restart_info_file)
            logger.info("重启信息文件已删除")
            
        except Exception as e:
            logger.error(f"处理重启信息失败: {e}")
            import traceback
            logger.error(f"错误详情: {traceback.format_exc()}")
            if os.path.exists(restart_info_file):
                try:
                    os.remove(restart_info_file)
                except:
                    pass
    async def _listen_events(self):
        logger.info("开始监听事件...")
        
        try:
            async for event_data in self.connection.listen():
                logger.debug(f"收到原始事件: {json.dumps(event_data, ensure_ascii=False, indent=2)}")
                await self._handle_event(event_data)
        except Exception as e:
            logger.error(f"监听事件时发生错误: {e}")
        finally:
            logger.info("事件监听已停止")
            await self._handle_plugin_event(Events.LunarStopListen())
    
    def _format_message_for_log(self, message_segments: List[Union[Dict, BaseSegment]]) -> str:
        log_parts = []
        
        for segment in message_segments:
            if isinstance(segment, BaseSegment):
                segment_type = segment.type
                segment_data = segment.data
            elif isinstance(segment, dict):
                segment_type = segment.get('type')
                segment_data = segment.get('data', {})
            else:
                continue
            
            if segment_type == 'text':
                text = segment_data.get('text', '')
                if text.strip():
                    log_parts.append(text)
            elif segment_type == 'image':
                log_parts.append('[图片]')
            elif segment_type == 'face':
                face_id = segment_data.get('id', '')
                log_parts.append(f'[表情:{face_id}]')
            elif segment_type == 'at':
                qq = segment_data.get('qq', '')
                log_parts.append(f'[@{qq}]')
            elif segment_type == 'record':
                log_parts.append('[语音]')
            elif segment_type == 'reply':
                log_parts.append('[回复]')
            else:
                log_parts.append(f'[{segment_type}]')
        
        return ' '.join(log_parts) if log_parts else '[空消息]'
    
    def _extract_text_from_message(self, message_data: List[Union[Dict, BaseSegment]]) -> str:
        text_parts = []
        for segment in message_data:
            if isinstance(segment, TextSegment):
                text_parts.append(segment.text)
            elif isinstance(segment, dict) and segment.get('type') == 'text':
                text = segment.get('data', {}).get('text', '')
                if text.strip():
                    text_parts.append(text)
        result = ' '.join(text_parts)
        return result
    
    async def send_message_segments(self, message_segments: List[Union[Dict, BaseSegment]], user_id: Optional[int] = None, group_id: Optional[int] = None) -> bool:
        if not message_segments:
            logger.warning("消息段为空，不发送")
            return False

        try:
            log_message = self._format_message_for_log(message_segments)

            if group_id:
                logger.info(f"向群 {group_id} 发送消息: {log_message}")
                response_data = await self.protocol.send_message(message_segments, group_id=group_id)
            elif user_id:
                logger.info(f"向用户 {user_id} 发送消息: {log_message}")
                response_data = await self.protocol.send_message(message_segments, user_id=user_id)
            else:
                logger.error("发送消息需要指定user_id或group_id")
                return False

            if response_data and response_data.get('status') == 'ok':
                logger.debug(f"消息发送成功并收到服务器确认: {response_data}")
                self.message_count['sent'] += 1
                return response_data
            else:
                error_msg = response_data.get('message', '未收到服务器确认或服务器返回错误') if response_data else '未收到服务器确认或服务器返回错误'
                logger.error(f"消息发送失败或未收到服务器确认: {error_msg}")
                return False
            
        except TimeoutError:
            logger.error(f"发送消息超时，未收到服务器确认。目标: {'群' if group_id else '用户'} {group_id if group_id else user_id}")
            return False
        except Exception as e:
            logger.error(f"发送消息段失败: {e}")
            return False

    async def del_message(self, message_id: int, user_id: Optional[int] = None, group_id: Optional[int] = None):
        try:
            result = await self.protocol.delete_message(message_id, user_id=user_id, group_id=group_id)
            logger.info(f"撤回消息 {message_id}")
            return result
        except Exception as e:
            logger.error(f"撤回消息时发生错误: {e}")
            return False

    async def send(self, message: Union[str, Dict, List[Union[Dict, BaseSegment]], BaseSegment], user_id: Optional[int] = None, group_id: Optional[int] = None) -> bool:
        if isinstance(message, str):
            message_segments = [self.msg.text(message)]
        elif isinstance(message, BaseSegment):
            message_segments = [message]
        elif isinstance(message, dict):
            parsed_segment = self.msg._parse_dict_to_segment(message)
            if parsed_segment:
                message_segments = [parsed_segment]
            else:
                message_segments = [message] 
        elif isinstance(message, list):
            processed_segments = []
            for item in message:
                if isinstance(item, BaseSegment):
                    processed_segments.append(item)
                elif isinstance(item, dict):
                    parsed_segment = self.msg._parse_dict_to_segment(item)
                    if parsed_segment:
                        processed_segments.append(parsed_segment)
                    else:
                        processed_segments.append(item)
                elif isinstance(item, str):
                    processed_segments.append(self.msg.text(item))
                else:
                    logger.warning(f"send() 方法中不支持的消息段类型: {type(item)}, 跳过。")
            message_segments = processed_segments
        else:
            logger.error(f"不支持的message类型: {type(message)}")
            return False
        
        return await self.send_message_segments(message_segments, user_id, group_id)


    async def _list_plugins(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        plugin_info_categorized = self.plugin_manager.get_plugin_list()
        
        response = "插件列表\n\n"
        
        if plugin_info_categorized['enabled_on_disk']:
            response += "✅ 已启用 (文件/目录未禁用):\n"
            for name, info in plugin_info_categorized['enabled_on_disk'].items():
                status_text = "已加载"
                if 'error' in info:
                    status_text = f"加载失败: {info['error']}"
                elif info.get('help') == '未加载 (可能在上次加载时被跳过)':
                    status_text = "未加载"
                
                response += f"• {name} (触发词: {info['trigger']}, 优先级: {info['priority']}, 状态: {status_text})\n"
                if info['help'] and '加载失败' not in info['help'] and '未加载' not in info['help']:
                    response += f"  帮助: {info['help']}\n"
        else:
            response += "暂无已启用插件\n"
        
        if plugin_info_categorized['disabled_on_disk']:
            response += "\n❌ 已禁用 (文件/目录已禁用):\n"
            for name, info in plugin_info_categorized['disabled_on_disk'].items():
                response += f"• {name} (状态: 已禁用, 触发词: {info['trigger']})\n"
                if info['help']:
                    response += f"  帮助: {info['help']}\n"
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True

    async def send_forward_msg(self, messages: List[Union[Dict, ForwardNodeSegment]], group_id: Optional[int] = None, user_id: Optional[int] = None):
        if not messages:
            logger.warning("转发消息为空，不发送")
            return False

        try:
            if group_id:
                logger.info(f"向群 {group_id} 发送消息: [forward_msg]")
            elif user_id:
                logger.info(f"向用户 {user_id} 发送消息: [forward_msg]")
            else:
                logger.error("发送转发消息需要指定user_id或group_id")
                return False

            success = await self.protocol.send_forward(messages, group_id=group_id, user_id=user_id)
            if success:
                self.message_count['sent'] += 1
            return success

        except Exception as e:
            logger.error(f"发送合并转发消息失败: {e}")
            return False

    async def get_forward_msg(self, message_id: str) -> Dict:
        return await self.protocol.get_forward_message(message_id)

    async def restart(self):
        logger.info("正在执行框架重启...")
        self.plugin_manager.stop_file_monitoring()
        await self._cleanup_resources()
        os._exit(0)
    
    async def _cleanup_resources(self):
        logger.info("正在清理资源...")
        await self._handle_plugin_event(Events.LunarStopListen())
        
        try:
            current_task = asyncio.current_task()
            tasks = [t for t in asyncio.all_tasks() if t is not current_task]
            
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("部分任务取消超时")
                except Exception as e:
                    logger.error(f"等待任务取消时出错: {e}")

            if hasattr(self, 'connection') and self.connection:
                await self.connection.close()
                self.connection = None

            if hasattr(self, 'plugin_manager') and hasattr(self.plugin_manager, 'stop_file_monitoring'):
                self.plugin_manager.stop_file_monitoring()
                
        except Exception as e:
            logger.error(f"资源清理过程中出错: {e}")
        finally:
            logger.info("资源清理完成")

    async def _restart_bot(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'manager'):
            response = "权限不足，只有管理员才能重启机器人"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True

        restart_info_file = os.path.abspath('restart_info.json')
        restart_info = {
            'start_time': time.time(),
            'user_id': user_id,
            'group_id': group_id,
            'message_type': 'group' if group_id else 'private'
        }
        
        try:
            os.makedirs(os.path.dirname(restart_info_file), exist_ok=True)
            
            with open(restart_info_file, 'w', encoding='utf-8') as f:
                json.dump(restart_info, f, indent=4, ensure_ascii=False)
            
            logger.info(f"重启信息已保存到: {restart_info_file}")
            
        except Exception as e:
            logger.error(f"保存重启信息失败: {e}")
            await self.send("重启信息保存失败", group_id=group_id, user_id=user_id)
            return True
        
        restarting_message = "开始重启，请稍候..."
        if group_id:
            await self.send(restarting_message, group_id=group_id)
        else:
            await self.send(restarting_message, user_id=user_id)
        
        logger.info(f"收到重启命令，来自用户 {user_id}" + (f" 群 {group_id}" if group_id else ""))

        await asyncio.sleep(3)
        
        await self._perform_restart()
        return True
    
    async def _perform_restart(self):
        logger.info("正在执行进程重启...")
        await self._cleanup_resources()
        python = sys.executable
        os.execv(python, [python] + sys.argv)

    async def _enable_plugin(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'manager'):
            response = "权限不足，只有管理员才能启用插件"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        if not args:
            response = "请提供插件名称"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        success = await self.plugin_manager.enable_plugin(args)
        if success:
            response = f"已启用插件: {args}"
            self.plugins = self.plugin_manager.plugins
        else:
            response = f"启用插件失败: {args} (可能未找到或操作失败)"
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True

    async def _disable_plugin(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'manager'):
            response = "权限不足，只有管理员才能禁用插件"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        if not args:
            response = "请提供插件名称"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        success = await self.plugin_manager.disable_plugin(args)
        if success:
            response = f"已禁用插件: {args}"
            self.plugins = self.plugin_manager.plugins
        else:
            response = f"禁用插件失败: {args} (可能未找到或操作失败)"
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True
    
    async def _add_manager(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'super'):
            response = "权限不足，只有超级用户才能添加管理员"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        try:
            new_manager = int(args)
            managers = self.config.get('manager_users', [])
            if new_manager not in managers:
                with open('admin114.json', 'r+', encoding='utf-8') as f:
                    data = json.load(f)
                    data['manager_users'] = managers + [new_manager]
                    f.seek(0)
                    json.dump(data, f, indent=4, ensure_ascii=False)
                    f.truncate()
                
                self.config['manager_users'] = managers + [new_manager]
                
                response = f"已添加用户 {new_manager} 为管理员"
            else:
                response = f"用户 {new_manager} 已经是管理员"
        except ValueError:
            response = "参数错误，请提供有效的用户ID"
        except FileNotFoundError:
            response = "配置文件 admin114.json 未找到"
        except Exception as e:
            response = f"添加管理员失败: {e}"
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True
    
    async def _remove_manager(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'super'):
            response = "权限不足，只有超级用户才能删除管理员"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        try:
            remove_manager = int(args)
            managers = self.config.get('manager_users', [])
            if remove_manager in managers:
                managers.remove(remove_manager)
                with open('admin114.json', 'r+', encoding='utf-8') as f:
                    data = json.load(f)
                    data['manager_users'] = managers
                    f.seek(0)
                    json.dump(data, f, indent=4, ensure_ascii=False)
                    f.truncate()

                self.config['manager_users'] = managers
                
                response = f"已移除用户 {remove_manager} 的管理员权限"
            else:
                response = f"用户 {remove_manager} 不是管理员"
        except ValueError:
            response = "参数错误，请提供有效的用户ID"
        except FileNotFoundError:
            response = "配置文件 admin114.json 未找到"
        except Exception as e:
            response = f"删除管理员失败: {e}"
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True
    
    async def _list_managers(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        super_users = self.config.get('super_users', [])
        manager_users = self.config.get('manager_users', [])
        
        response = "超级用户:\n" + "\n".join(str(uid) for uid in super_users)
        response += "\n\n管理员:\n" + "\n".join(str(uid) for uid in manager_users)
        
        if group_id:
            await self.send(response, group_id=group_id)
        else:
            await self.send(response, user_id=user_id)
        
        return True
    
    async def _message_stats(self, args: str, event: Event) -> bool:
        user_id = event.user_id
        group_id = getattr(event, 'group_id', None)
        
        if not self._check_permission(user_id, 'manager'):
            response = "权限不足，只有管理员才能查看消息统计"
            if group_id:
                await self.send(response, group_id=group_id)
            else:
                await self.send(response, user_id=user_id)
            return True
        
        uptime = time.time() - self.start_time
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        stats = (
            f"📊 消息统计:\n"
            f"发送: {self.message_count['sent']}\n"
            f"接收: {self.message_count['received']}\n"
            f"运行时间: {int(hours)}小时 {int(minutes)}分钟 {int(seconds)}秒"
        )
        
        if group_id:
            await self.send(stats, group_id=group_id)
        else:
            await self.send(stats, user_id=user_id)
        
        return True

# MacroSTAR-Studio 2025
# 项目名称: Lunar X
# 版本: BETA 0.1.0.122
# 全新一代QQ机器人框架