from typing import Dict, Any, List, Union, Optional

from .message import BaseSegment
from .logger import logger


class OneBotAdapter:
    """OneBot v11 协议适配器。

    负责 OneBot action 构造与消息段序列化。
    bot 核心通过本适配器与协议交互，不直接接触 action 字符串。
    """

    def __init__(self, bot):
        self.bot = bot

    async def send_message(self, message_segments: List[Union[Dict, BaseSegment]],
                           user_id: Optional[int] = None, group_id: Optional[int] = None) -> Any:
        api_message_segments = []
        for segment in message_segments:
            if isinstance(segment, BaseSegment):
                api_message_segments.append(segment.to_dict())
            elif isinstance(segment, dict):
                if segment.get('type') == 'at':
                    qq = segment.get('data', {}).get('qq')
                    if isinstance(qq, int):
                        segment['data']['qq'] = str(qq)
                api_message_segments.append(segment)
            else:
                logger.warning(f"不支持的发送消息段类型，跳过: {type(segment)}")
                continue

        if group_id:
            request_payload = {
                'action': 'send_group_msg',
                'params': {
                    'group_id': int(group_id),
                    'message': api_message_segments
                }
            }
        elif user_id:
            request_payload = {
                'action': 'send_private_msg',
                'params': {
                    'user_id': int(user_id),
                    'message': api_message_segments
                }
            }
        else:
            logger.error("发送消息需要指定user_id或group_id")
            return False

        return await self.bot.connection.send(request_payload, wait_for_response=True)

    async def send_forward(self, messages: List[Union[Dict, BaseSegment]],
                           group_id: Optional[int] = None, user_id: Optional[int] = None) -> bool:
        formatted_messages = []
        for msg in messages:
            if isinstance(msg, BaseSegment):
                formatted_messages.append(msg.to_dict())
            elif isinstance(msg, dict):
                if msg.get('type') == 'node' and 'data' in msg:
                    data = msg['data'].copy()
                    if 'user_id' in data and isinstance(data['user_id'], int):
                        data['user_id'] = str(data['user_id'])
                    formatted_messages.append({'type': 'node', 'data': data})
                else:
                    formatted_messages.append(msg)
            else:
                logger.warning(f"不支持的转发消息节点类型，跳过: {type(msg)}")

        params = {'messages': formatted_messages}

        if group_id:
            params['group_id'] = group_id
            await self.bot.connection.send({
                'action': 'send_group_forward_msg',
                'params': params
            })
        elif user_id:
            params['user_id'] = user_id
            await self.bot.connection.send({
                'action': 'send_private_forward_msg',
                'params': params
            })
        else:
            logger.error("发送转发消息需要指定user_id或group_id")
            return False

        return True

    async def delete_message(self, message_id: int, user_id: Optional[int] = None,
                             group_id: Optional[int] = None):
        try:
            result = await self.bot.connection.send({
                'action': 'delete_msg',
                'params': {
                    'message_id': message_id
                }
            })
            return result
        except Exception as e:
            logger.error(f"撤回消息时发生错误: {e}")
            return False

    async def get_forward_message(self, message_id: str) -> Dict:
        try:
            await self.bot.connection.send({
                'action': 'get_forward_msg',
                'params': {
                    'message_id': message_id
                }
            })
            return {'status': 'ok', 'message_id': message_id}
        except Exception as e:
            logger.error(f"获取合并转发消息失败: {e}")
            return {'status': 'error', 'msg': str(e)}

    async def call_api(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_data = {
            'action': action,
            'params': params
        }
        response_data = await self.bot.connection.send(request_data, wait_for_response=True)

        if response_data:
            if response_data.get('status') == 'ok':
                return {'status': 'ok', 'data': response_data.get('data'), 'raw_response': response_data}
            else:
                error_msg = response_data.get('message', '未知错误')
                return {'status': 'failed', 'msg': error_msg, 'raw_response': response_data}
        else:
            return {'status': 'failed', 'msg': '未收到服务器响应'}
