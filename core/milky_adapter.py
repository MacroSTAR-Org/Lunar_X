from typing import Dict, Any, List, Optional, Tuple

from .events import EventFactory
from .logger import logger


# ============================================================
# OneBot action 名 → Milky action 名映射表
# ============================================================

# 名称相同的直通映射
SAME_NAME_ACTIONS = {
    'get_login_info', 'get_friend_list', 'get_friend_info', 'get_user_profile',
    'get_group_list', 'get_group_info', 'get_group_member_info', 'get_group_member_list',
    'get_message', 'get_cookies', 'get_csrf_token', 'get_friend_requests',
    'accept_friend_request', 'reject_friend_request', 'delete_friend',
    'send_friend_nudge', 'send_group_nudge', 'send_profile_like',
    'send_group_announcement', 'get_group_announcements', 'delete_group_announcement',
    'set_group_name', 'kick_group_member', 'set_group_member_mute',
    'set_group_whole_mute', 'set_group_member_admin', 'set_group_member_card',
    'set_group_member_special_title', 'quit_group',
    'get_group_essence_messages', 'set_group_essence_message',
    'get_group_files', 'upload_group_file', 'delete_group_file',
    'create_group_folder', 'rename_group_folder', 'delete_group_folder',
    'upload_private_file', 'get_private_file_download_url', 'get_group_file_download_url',
    'get_custom_face_url_list', 'get_peer_pins', 'set_peer_pin',
    'set_avatar', 'set_nickname', 'set_bio',
}

# 改名直通（OneBot → Milky，参数一致或仅键名小差异）
RENAME_ACTIONS = {
    'set_group_kick': 'kick_group_member',
    'set_group_ban': 'set_group_member_mute',
    'set_group_whole_ban': 'set_group_whole_mute',
    'set_group_admin': 'set_group_member_admin',
    'set_group_card': 'set_group_member_card',
    'set_group_special_title': 'set_group_member_special_title',
    'set_essence_msg': 'set_group_essence_message',
    'get_group_essence_list': 'get_group_essence_messages',
    'set_group_leave': 'quit_group',
    'send_group_poke': 'send_group_nudge',
    'send_private_poke': 'send_friend_nudge',
    'get_group_file_url': 'get_group_file_download_url',
    'get_private_file_url': 'get_private_file_download_url',
    'get_stranger_info': 'get_user_profile',
    'accept_group_request': 'accept_group_request',
    'reject_group_request': 'reject_group_request',
}


def _convert_delete_msg(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """delete_msg {message_id} → recall_*_message {message_scene, peer_id, message_seq}"""
    message_id = params.get('message_id')
    new_params = {'message_seq': message_id}
    if params.get('group_id'):
        new_params['message_scene'] = 'group'
        new_params['peer_id'] = params['group_id']
        return 'recall_group_message', new_params
    elif params.get('user_id'):
        new_params['message_scene'] = 'friend'
        new_params['peer_id'] = params['user_id']
        return 'recall_private_message', new_params
    raise ValueError("delete_msg 需要 group_id 或 user_id 来确定消息场景")


def _convert_get_msg(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """get_msg {message_id} → get_message {message_scene, peer_id, message_seq}"""
    message_id = params.get('message_id')
    new_params = {'message_seq': message_id}
    if params.get('message_scene'):
        new_params['message_scene'] = params['message_scene']
        new_params['peer_id'] = params.get('peer_id')
    elif params.get('group_id'):
        new_params['message_scene'] = 'group'
        new_params['peer_id'] = params['group_id']
    elif params.get('user_id'):
        new_params['message_scene'] = 'friend'
        new_params['peer_id'] = params['user_id']
    else:
        raise ValueError("get_msg 需要 message_scene+peer_id 或 group_id/user_id 来确定消息场景")
    return 'get_message', new_params


def _convert_get_forward_msg(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """get_forward_msg {message_id} → get_forwarded_messages {forward_id}"""
    return 'get_forwarded_messages', {'forward_id': params.get('message_id')}


def _convert_send_forward_msg(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """send_group_forward_msg / send_private_forward_msg → send_*_message + forward 段"""
    target = 'send_group_message' if params.get('group_id') else 'send_private_message'
    messages = params.get('messages', [])
    forward_data = []
    for node in messages:
        if isinstance(node, dict):
            data = node.get('data', node)
            forward_data.append({
                'user_id': data.get('user_id', 0),
                'sender_name': data.get('nickname', ''),
                'segments': data.get('content', []),
            })
    new_params = {'message': [{'type': 'forward', 'data': {'messages': forward_data}}]}
    if params.get('group_id'):
        new_params['group_id'] = params['group_id']
    elif params.get('user_id'):
        new_params['user_id'] = params['user_id']
    return target, new_params


def _convert_accept_friend_request(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    return 'accept_friend_request', {
        'initiator_uid': str(params.get('flag', '')),
        'is_filtered': params.get('is_filtered', False),
    }


def _convert_reject_friend_request(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    new_params = {
        'initiator_uid': str(params.get('flag', '')),
        'is_filtered': params.get('is_filtered', False),
    }
    if params.get('reason'):
        new_params['reason'] = params['reason']
    return 'reject_friend_request', new_params


def _convert_accept_group_request(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    return 'accept_group_request', {
        'notification_seq': int(params.get('flag', 0)),
        'notification_type': 'join_request',
        'group_id': params.get('group_id', 0),
        'is_filtered': params.get('is_filtered', False),
    }


def _convert_reject_group_request(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    new_params = {
        'notification_seq': int(params.get('flag', 0)),
        'notification_type': 'join_request',
        'group_id': params.get('group_id', 0),
        'is_filtered': params.get('is_filtered', False),
    }
    if params.get('reason'):
        new_params['reason'] = params['reason']
    return 'reject_group_request', new_params


def _convert_get_image(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    return 'get_resource_temp_url', {'resource_id': params.get('file', '')}


def _convert_get_record(params: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    return 'get_resource_temp_url', {'resource_id': params.get('file', '')}


# 需要参数转换的映射（OneBot action → (Milky action, 转换函数)）
TRANSFORM_ACTIONS = {
    'delete_msg': _convert_delete_msg,
    'get_msg': _convert_get_msg,
    'get_forward_msg': _convert_get_forward_msg,
    'send_group_forward_msg': _convert_send_forward_msg,
    'send_private_forward_msg': _convert_send_forward_msg,
    'accept_friend_request': _convert_accept_friend_request,
    'reject_friend_request': _convert_reject_friend_request,
    'accept_group_request': _convert_accept_group_request,
    'reject_group_request': _convert_reject_group_request,
    'get_image': _convert_get_image,
    'get_record': _convert_get_record,
}


class MilkyAdapter:
    """Milky 协议适配器。

    - 事件转换：Milky {time, self_id, event_type, data} → 内部 Event 类（复用 EventFactory）
    - 消息段转换：内部段对象 ↔ Milky OutgoingSegment/IncomingSegment
    - action 映射：插件使用的 OneBot action 名 → Milky action 名
    """

    def __init__(self, bot):
        self.bot = bot

    # ============ 发送消息 ============

    def _internal_to_milky_segments(self, segments: List) -> List[Dict]:
        """内部消息段（BaseSegment / dict）→ Milky OutgoingSegment 列表"""
        result = []
        for s in segments:
            if isinstance(s, dict):
                parsed = self.bot.msg._parse_dict_to_segment(s)
                if parsed:
                    s = parsed
                else:
                    result.append(s)
                    continue
            stype = s.type
            data = s.data
            if stype == 'text':
                result.append({'type': 'text', 'data': {'text': data.get('text', '')}})
            elif stype == 'at':
                qq = data.get('qq', '')
                if str(qq) == 'all':
                    result.append({'type': 'mention_all', 'data': {}})
                else:
                    result.append({'type': 'mention', 'data': {'user_id': int(qq)}})
            elif stype == 'face':
                result.append({'type': 'face', 'data': {'face_id': data.get('id', 0), 'is_large': False}})
            elif stype == 'reply':
                result.append({'type': 'reply', 'data': {'message_seq': int(data.get('id', 0))}})
            elif stype == 'image':
                result.append({'type': 'image', 'data': {'uri': data.get('file', '')}})
            elif stype == 'record':
                result.append({'type': 'record', 'data': {'uri': data.get('file', '')}})
            elif stype == 'video':
                result.append({'type': 'video', 'data': {'uri': data.get('file', '')}})
            else:
                # 其余类型原样透传（forward/light_app 等）
                result.append({'type': stype, 'data': data})
        return result

    async def send_message(self, message_segments: List, user_id: Optional[int] = None,
                           group_id: Optional[int] = None):
        api_segments = self._internal_to_milky_segments(message_segments)
        if group_id:
            return await self.bot.connection.send({
                'action': 'send_group_message',
                'params': {'group_id': int(group_id), 'message': api_segments},
            }, wait_for_response=True)
        elif user_id:
            return await self.bot.connection.send({
                'action': 'send_private_message',
                'params': {'user_id': int(user_id), 'message': api_segments},
            }, wait_for_response=True)
        else:
            logger.error("发送消息需要指定user_id或group_id")
            return False

    async def send_forward(self, messages: List, group_id: Optional[int] = None,
                           user_id: Optional[int] = None) -> bool:
        """合并转发：Milky 无独立 API，通过 send_*_message + forward 段实现"""
        forward_data = []
        for msg in messages:
            if isinstance(msg, dict):
                data = msg.get('data', msg)
                content = data.get('content', [])
                forward_data.append({
                    'user_id': data.get('user_id', 0),
                    'sender_name': data.get('nickname', data.get('sender_name', '')),
                    'segments': self._internal_to_milky_segments(content),
                })
        if not forward_data:
            logger.warning("转发消息为空，不发送")
            return False
        segment = {'type': 'forward', 'data': {'messages': forward_data}}
        response = await self.send_message([segment], user_id=user_id, group_id=group_id)
        return bool(response and response.get('status') == 'ok')

    async def delete_message(self, message_id: int, user_id: Optional[int] = None,
                             group_id: Optional[int] = None):
        """撤回消息：Milky 需要消息场景与 peer，由调用方提供"""
        try:
            if group_id:
                action, params = 'recall_group_message', {'message_scene': 'group', 'peer_id': group_id, 'message_seq': message_id}
            elif user_id:
                action, params = 'recall_private_message', {'message_scene': 'friend', 'peer_id': user_id, 'message_seq': message_id}
            else:
                logger.error("Milky 撤回消息需要 user_id 或 group_id（消息场景）")
                return False
            result = await self.bot.connection.send({'action': action, 'params': params}, wait_for_response=True)
            return result
        except Exception as e:
            logger.error(f"撤回消息时发生错误: {e}")
            return False

    async def get_forward_message(self, message_id: str) -> Dict:
        try:
            response = await self.bot.connection.send({
                'action': 'get_forwarded_messages',
                'params': {'forward_id': message_id},
            }, wait_for_response=True)
            if response and response.get('status') == 'ok':
                return {'status': 'ok', 'messages': response.get('data', {}).get('messages', [])}
            return {'status': 'failed', 'msg': response.get('message', '获取失败') if response else '未收到响应'}
        except Exception as e:
            logger.error(f"获取合并转发消息失败: {e}")
            return {'status': 'error', 'msg': str(e)}

    # ============ action 映射 ============

    async def call_api(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        milky_action = action
        milky_params = params

        if action in TRANSFORM_ACTIONS:
            try:
                milky_action, milky_params = TRANSFORM_ACTIONS[action](params)
                logger.debug(f"action 转换: {action} → {milky_action}")
            except Exception as e:
                logger.warning(f"action '{action}' 参数转换失败: {e}，按原名透传")
        elif action in RENAME_ACTIONS:
            milky_action = RENAME_ACTIONS[action]
            logger.debug(f"action 改名: {action} → {milky_action}")
        elif action not in SAME_NAME_ACTIONS:
            logger.warning(f"action '{action}' 未在映射表中，按原名透传给 Milky 协议端（插件可能需要迁移）")

        # 发送类 action 的 message 参数做段序列化
        if milky_action in ('send_group_message', 'send_private_message') and isinstance(milky_params, dict):
            milky_params = dict(milky_params)
            milky_params['message'] = self._internal_to_milky_segments(milky_params.get('message', []))

        response = await self.bot.connection.send({'action': milky_action, 'params': milky_params}, wait_for_response=True)

        if response:
            if response.get('status') == 'ok':
                return {'status': 'ok', 'data': response.get('data'), 'raw_response': response}
            else:
                return {'status': 'failed', 'msg': response.get('message', '未知错误'), 'raw_response': response}
        return {'status': 'failed', 'msg': '未收到服务器响应'}

    # ============ 事件转换 ============

    def parse_event(self, raw: Dict[str, Any]):
        """Milky 事件 dict → 内部 Event 对象（翻译为 OneBot 风格 dict 后复用 EventFactory）"""
        onebot_like = self._to_onebot_like(raw)
        return EventFactory.create_event(onebot_like, self.bot.msg)

    def _incoming_segments_to_onebot(self, segments: List[Dict]) -> List[Dict]:
        """Milky IncomingSegment → OneBot 风格段 dict（交给 MessageBuilder 解析）"""
        result = []
        for s in segments:
            stype = s.get('type')
            data = s.get('data', {})
            if stype == 'text':
                result.append({'type': 'text', 'data': {'text': data.get('text', '')}})
            elif stype == 'mention':
                result.append({'type': 'at', 'data': {'qq': str(data.get('user_id'))}})
            elif stype == 'mention_all':
                result.append({'type': 'at', 'data': {'qq': 'all'}})
            elif stype == 'face':
                result.append({'type': 'face', 'data': {'id': data.get('face_id', 0)}})
            elif stype == 'reply':
                result.append({'type': 'reply', 'data': {'id': str(data.get('message_seq', 0))}})
            elif stype == 'image':
                result.append({'type': 'image', 'data': {'file': data.get('temp_url') or data.get('resource_id', '')}})
            elif stype == 'record':
                result.append({'type': 'record', 'data': {'file': data.get('temp_url') or data.get('resource_id', '')}})
            elif stype == 'video':
                result.append({'type': 'video', 'data': {'file': data.get('temp_url') or data.get('resource_id', '')}})
            elif stype == 'file':
                result.append({'type': 'file', 'data': {
                    'file_id': data.get('file_id', ''), 'file_name': data.get('file_name', ''),
                    'file_size': data.get('file_size', 0)}})
            elif stype == 'forward':
                result.append({'type': 'forward', 'data': {'id': data.get('forward_id', '')}})
            else:
                # 未知/其他段类型：保留原始段（框架兼容性规定：不报错）
                result.append({'type': stype, 'data': data})
        return result

    def _to_onebot_like(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Milky 事件 → OneBot 风格事件 dict"""
        event_type = raw.get('event_type')
        d = raw.get('data', {}) if isinstance(raw.get('data'), dict) else {}
        base = {'time': raw.get('time'), 'self_id': raw.get('self_id'), 'raw_event': raw}

        if event_type == 'message_receive':
            scene = d.get('message_scene')
            segs = self._incoming_segments_to_onebot(d.get('segments', []))
            text = ' '.join(s.get('data', {}).get('text', '') for s in segs if s.get('type') == 'text')
            message = {
                **base,
                'post_type': 'message',
                # OneBot 的 message_type 是 private/group；Milky 的 friend/temp 均归为私聊
                'message_type': 'group' if scene == 'group' else 'private',
                'message_id': d.get('message_seq'),
                'user_id': d.get('sender_id'),
                'message': segs,
                'raw_message': text,
            }
            if scene == 'group':
                member = d.get('group_member', {})
                message['group_id'] = d.get('peer_id')
                message['sender'] = {
                    'user_id': d.get('sender_id'),
                    'card': member.get('card', ''),
                    'nickname': member.get('nickname', ''),
                    'role': member.get('role', 'member'),
                }
            elif scene == 'friend':
                friend = d.get('friend', {})
                message['sender'] = {
                    'user_id': d.get('sender_id'),
                    'nickname': friend.get('nickname', ''),
                }
            else:  # temp
                message['sender'] = {'user_id': d.get('sender_id')}
            return message

        if event_type == 'message_recall':
            scene = d.get('message_scene')
            if scene == 'group':
                return {**base, 'post_type': 'notice', 'notice_type': 'group_recall',
                        'group_id': d.get('peer_id'), 'user_id': d.get('sender_id'),
                        'operator_id': d.get('operator_id'), 'message_id': d.get('message_seq')}
            return {**base, 'post_type': 'notice', 'notice_type': 'friend_recall',
                    'user_id': d.get('sender_id'), 'message_id': d.get('message_seq')}

        if event_type == 'group_member_increase':
            return {**base, 'post_type': 'notice', 'notice_type': 'group_increase',
                    'group_id': d.get('group_id'), 'user_id': d.get('user_id'),
                    'operator_id': d.get('operator_id', 0) or 0,
                    'sub_type': 'approve' if d.get('operator_id') else 'invite'}

        if event_type == 'group_member_decrease':
            return {**base, 'post_type': 'notice', 'notice_type': 'group_decrease',
                    'group_id': d.get('group_id'), 'user_id': d.get('user_id'),
                    'operator_id': d.get('operator_id', 0) or 0,
                    'sub_type': 'kick' if d.get('operator_id') else 'leave'}

        if event_type == 'group_admin_change':
            return {**base, 'post_type': 'notice', 'notice_type': 'group_admin',
                    'group_id': d.get('group_id'), 'user_id': d.get('user_id'),
                    'sub_type': 'set' if d.get('is_set') else 'unset'}

        if event_type == 'group_mute':
            return {**base, 'post_type': 'notice', 'notice_type': 'group_ban',
                    'group_id': d.get('group_id'), 'user_id': d.get('user_id'),
                    'operator_id': d.get('operator_id'), 'duration': d.get('duration', 0),
                    'sub_type': 'ban' if d.get('duration', 0) > 0 else 'lift_ban'}

        if event_type == 'group_whole_mute':
            return {**base, 'post_type': 'notice', 'notice_type': 'group_ban',
                    'group_id': d.get('group_id'), 'user_id': 0, 'operator_id': d.get('operator_id'),
                    'duration': 0, 'sub_type': 'ban' if d.get('is_mute') else 'lift_ban'}

        if event_type == 'group_nudge':
            return {**base, 'post_type': 'notice', 'notice_type': 'notify', 'sub_type': 'poke',
                    'group_id': d.get('group_id'), 'user_id': d.get('sender_id'),
                    'target_id': d.get('receiver_id')}

        if event_type == 'friend_nudge':
            return {**base, 'post_type': 'notice', 'notice_type': 'notify', 'sub_type': 'poke',
                    'group_id': 0, 'user_id': d.get('user_id'), 'target_id': 0}

        if event_type == 'friend_request':
            return {**base, 'post_type': 'request', 'request_type': 'friend',
                    'user_id': d.get('initiator_id'), 'comment': d.get('comment', ''),
                    'flag': d.get('initiator_uid', '')}

        if event_type == 'group_join_request':
            return {**base, 'post_type': 'request', 'request_type': 'group', 'sub_type': 'add',
                    'group_id': d.get('group_id'), 'user_id': d.get('initiator_id'),
                    'comment': d.get('comment', ''), 'flag': str(d.get('notification_seq', ''))}

        if event_type == 'group_invited_join_request':
            return {**base, 'post_type': 'request', 'request_type': 'group', 'sub_type': 'invite',
                    'group_id': d.get('group_id'), 'user_id': d.get('initiator_id'),
                    'comment': '', 'flag': str(d.get('notification_seq', ''))}

        if event_type == 'group_file_upload':
            return {**base, 'post_type': 'notice', 'notice_type': 'group_upload',
                    'group_id': d.get('group_id'), 'user_id': d.get('user_id'),
                    'file': {'id': d.get('file_id'), 'name': d.get('file_name'), 'size': d.get('file_size')}}

        # 其余事件类型（peer_pin_change / group_message_reaction / bot_offline /
        # group_name_change / group_essence_message_change / friend_file_upload ...）：
        # 无对应内部事件类，映射为基类 NoticeEvent，原始数据保留（兼容 Milky 规定：未知事件不报错）
        return {**base, 'post_type': 'notice', 'notice_type': event_type}
