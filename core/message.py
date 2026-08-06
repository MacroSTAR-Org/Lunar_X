import os
import re
from typing import Dict, Any, List, Union, Optional
import time as time_module

class BaseSegment:
    def __init__(self, type: str, data: Dict[str, Any]):
        self._type = type
        self._data = data

    @property
    def type(self) -> str:
        return self._type

    @property
    def data(self) -> Dict[str, Any]:
        return self._data

    def to_dict(self) -> Dict[str, Any]:
        return {'type': self._type, 'data': self._data}

    def __repr__(self):
        return f"{self.__class__.__name__}(type='{self.type}', data={self.data})"

    def __str__(self):
        return f"[{self.type}: {self.data}]"

class TextSegment(BaseSegment):
    def __init__(self, text: str):
        super().__init__('text', {'text': text})
    
    @property
    def text(self) -> str:
        return self.data.get('text', '')

class ImageSegment(BaseSegment):
    def __init__(self, file: str, cache: bool = True, proxy: bool = True, timeout: int = 30):
        super().__init__('image', {'file': file, 'cache': cache, 'proxy': proxy, 'timeout': timeout})
    
    @property
    def file(self) -> str:
        return self.data.get('file', '')

class AtSegment(BaseSegment):
    def __init__(self, user_id: Union[int, str]):
        super().__init__('at', {'qq': str(user_id)}) 
    
    @property
    def qq(self) -> str:
        return self.data.get('qq', '')

class FaceSegment(BaseSegment):
    def __init__(self, face_id: int):
        super().__init__('face', {'id': face_id})
    
    @property
    def id(self) -> int:
        return self.data.get('id', 0)

class RecordSegment(BaseSegment):
    def __init__(self, file: str, magic: bool = False, cache: bool = True, proxy: bool = True, timeout: int = 30):
        super().__init__('record', {'file': file, 'magic': magic, 'cache': cache, 'proxy': proxy, 'timeout': timeout})
    
    @property
    def file(self) -> str:
        return self.data.get('file', '')

class VideoSegment(BaseSegment):
    def __init__(self, file: str, cache: bool = True, proxy: bool = True, timeout: int = 30):
        super().__init__('video', {'file': file, 'cache': cache, 'proxy': proxy, 'timeout': timeout})
    
    @property
    def file(self) -> str:
        return self.data.get('file', '')

class FileSegment(BaseSegment):
    def __init__(self, file_id: str, file_name: str = '', file_size: int = 0):
        super().__init__('file', {'file_id': file_id, 'file_name': file_name, 'file_size': file_size})
    
    @property
    def file_id(self) -> str:
        return self.data.get('file_id', '')

class ReplySegment(BaseSegment):
    def __init__(self, message_id: Union[int, str], quoted_segments: Optional[List] = None):
        data = {'id': str(message_id)}
        if quoted_segments:
            # Milky 协议 reply 段自带被引用消息内容（segments），随事件透传
            data['quoted_segments'] = quoted_segments
        super().__init__('reply', data)
    
    @property
    def id(self) -> str:
        return self.data.get('id', '')

class ForwardNodeSegment(BaseSegment):
    def __init__(self, user_id: Union[int, str], nickname: str, content: List[BaseSegment]):
        super().__init__('node', {
            'user_id': str(user_id),
            'nickname': nickname,
            'content': [seg.to_dict() for seg in content]
        })
    
    @property
    def user_id(self) -> str:
        return self.data.get('user_id', '')
    
    @property
    def nickname(self) -> str:
        return self.data.get('nickname', '')
    
    @property
    def content(self) -> List[Dict]:
        return self.data.get('content', [])

class ForwardSegment(BaseSegment):
    def __init__(self, nodes: List[ForwardNodeSegment]):
        super().__init__('forward', {
            'id': str(int(time_module.time())),
            'nodes': [node.to_dict() for node in nodes]
        })
    
    @property
    def nodes(self) -> List[Dict]:
        return self.data.get('nodes', [])

class MessageBuilder:
    def __init__(self, bot):
        self.bot = bot
    
    def image(self, file: str, cache: bool = True, proxy: bool = True, timeout: int = 30) -> ImageSegment:
        if not os.path.isabs(file) and not (file.startswith('http://') or file.startswith('https://') or file.startswith('base64://')):
            file = os.path.abspath(file)
        return ImageSegment(file, cache, proxy, timeout)
    
    def text(self, text: str) -> TextSegment:
        return TextSegment(text)
    
    def at(self, user_id: int) -> AtSegment:
        return AtSegment(user_id)
    
    def face(self, face_id: int) -> FaceSegment:
        return FaceSegment(face_id)
    
    def record(self, file: str, magic: bool = False, cache: bool = True, proxy: bool = True, timeout: int = 30) -> RecordSegment:
        if not os.path.isabs(file) and not (file.startswith('http://') or file.startswith('https://') or file.startswith('base64://')):
            file = os.path.abspath(file)
        return RecordSegment(file, magic, cache, proxy, timeout)

    def video(self, file: str, cache: bool = True, proxy: bool = True, timeout: int = 30) -> VideoSegment:
        if not os.path.isabs(file) and not (file.startswith('http://') or file.startswith('https://') or file.startswith('base64://')):
            file = os.path.abspath(file)
        return VideoSegment(file, cache, proxy, timeout)

    def file(self, file_id: str, file_name: str = '', file_size: int = 0) -> FileSegment:
        return FileSegment(file_id, file_name, file_size)

    def reply(self, message_id: Union[int, str], quoted_segments: Optional[List] = None) -> ReplySegment:
        return ReplySegment(message_id, quoted_segments)

    def forward_node(self, user_id: int, nickname: str, content: Union[str, List[Union[BaseSegment, Dict]]]) -> ForwardNodeSegment:
        if isinstance(content, str):
            message_content = [self.text(content)]
        elif isinstance(content, list):
            message_content = []
            for item in content:
                if isinstance(item, BaseSegment):
                    message_content.append(item)
                elif isinstance(item, dict):
                    parsed_segment = self._parse_dict_to_segment(item)
                    if parsed_segment:
                        message_content.append(parsed_segment)
                    else:
                        from .logger import logger
                        logger.warning(f"无法解析的转发节点内容字典，保留为原始字典: {item}")
                        message_content.append(item)
                else:
                    from .logger import logger
                    logger.warning(f"不支持的转发节点内容类型，跳过: {type(item)}")
        else:
            raise ValueError("content 必须是字符串、消息段对象列表或消息段字典列表")
        
        return ForwardNodeSegment(user_id, nickname, message_content)

    def forward(self, nodes: List[ForwardNodeSegment]) -> ForwardSegment:
        return ForwardSegment(nodes)

    def combine(self, *message_segments: Union[BaseSegment, str, Dict]) -> List[BaseSegment]:
        combined = []
        for segment in message_segments:
            if isinstance(segment, str):
                combined.append(self.text(segment))
            elif isinstance(segment, BaseSegment):
                combined.append(segment)
            elif isinstance(segment, dict):
                parsed_segment = self._parse_dict_to_segment(segment)
                if parsed_segment:
                    combined.append(parsed_segment)
                else:
                    from .logger import logger
                    logger.warning(f"无法组合的字典消息段，跳过: {segment}")
            else:
                from .logger import logger
                logger.warning(f"不支持的组合消息段类型，跳过: {type(segment)}")
        return combined
    
    def _parse_dict_to_segment(self, segment_dict: Dict[str, Any]) -> Optional[BaseSegment]:
        seg_type = segment_dict.get('type')
        seg_data = segment_dict.get('data', {})
        
        if seg_type == 'text':
            return self.text(seg_data.get('text', ''))
        elif seg_type == 'image':
            return self.image(seg_data.get('file', ''), seg_data.get('cache', True), seg_data.get('proxy', True), seg_data.get('timeout', 30))
        elif seg_type == 'at':
            return self.at(seg_data.get('qq'))
        elif seg_type == 'face':
            return self.face(seg_data.get('id'))
        elif seg_type == 'record':
            return self.record(seg_data.get('file', ''), seg_data.get('magic', False), seg_data.get('cache', True), seg_data.get('proxy', True), seg_data.get('timeout', 30))
        elif seg_type == 'video':
            return self.video(seg_data.get('file', ''), seg_data.get('cache', True), seg_data.get('proxy', True), seg_data.get('timeout', 30))
        elif seg_type == 'file':
            return self.file(seg_data.get('file_id', ''), seg_data.get('file_name', ''), seg_data.get('file_size', 0))
        elif seg_type == 'reply':
            return self.reply(seg_data.get('id'), seg_data.get('quoted_segments'))
        return None

    def gen_message(self, data: Union[Dict, List]) -> List[BaseSegment]:
        if isinstance(data, dict):
            if 'message' in data:
                message_data = data['message']
            else:
                message_data = [data]
        elif isinstance(data, list):
            message_data = data
        else:
            raise ValueError("输入数据必须是字典或列表")
        
        message_segments = []
        
        for segment in message_data:
            if isinstance(segment, BaseSegment):
                message_segments.append(segment)
            elif isinstance(segment, dict):
                parsed_segment = self._parse_dict_to_segment(segment)
                if parsed_segment:
                    message_segments.append(parsed_segment)
                else:
                    from .logger import logger
                    logger.warning(f"无法将字典解析为消息段对象，跳过: {segment.get('type', '未知类型')}")
                    logger.debug(str(segment))
            elif isinstance(segment, str):
                message_segments.append(self.text(segment))
            else:
                from .logger import logger
                logger.warning(f"不支持的消息段类型，跳过: {type(segment)}")
        
        return message_segments

class ReplyUtils:
    
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def extract_reply_id_from_cq_code(text: str) -> Optional[str]:
        patterns = [
            r'[CQ:reply,id=(\d+)]',
            r'[reply id=(\d+)]',
            r'[CQ:reply,id="(\d+)"]',
            r'[reply id="(\d+)"]'
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        
        return None

    @staticmethod
    def parse_cq_code(text: str) -> List[Dict]:
        result = []
        reply_id = ReplyUtils.extract_reply_id_from_cq_code(text)
        if reply_id:
            result.append({'type': 'reply', 'data': {'id': reply_id}})
            text = re.sub(r'[CQ:reply,id=(\d+)]|[reply id=(\d+)]|[CQ:reply,id="(\d+)"]|[reply id="(\d+)"]', '', text)
        
        pattern = r'[CQ:(\w+)([^]]*)]'
        matches = re.findall(pattern, text)
        
        for match in matches:
            cq_type = match[0]
            param_string = match[1]
            
            params = {}
            param_pattern = r'(?:,|^)([^=]+)=([^,]]+)'
            param_matches = re.findall(param_pattern, param_string)
            
            for param_name, param_value in param_matches:
                param_name = param_name.strip()
                param_value = param_value.strip()
                if param_value.startswith('"') and param_value.endswith('"'):
                    param_value = param_value[1:-1]
                params[param_name] = param_value
            
            result.append({
                'type': cq_type,
                'data': params
            })
        
        return result
    
    def extract_reply_id(self, event_message: List[BaseSegment]) -> Optional[str]:
        for segment in event_message:
            if isinstance(segment, ReplySegment):
                return segment.id
        return None
    
    def extract_mentioned_users(self, event_message: List[BaseSegment]) -> List[int]:
        user_ids = []
        for segment in event_message:
            if isinstance(segment, AtSegment):
                try:
                    user_ids.append(int(segment.qq))
                except ValueError:
                    pass
        return user_ids
    
    def get_plain_text(self, event_message: List[BaseSegment]) -> str:
        plain_text_parts = []
        for segment in event_message:
            if isinstance(segment, TextSegment):
                plain_text_parts.append(segment.text)
        return ' '.join(plain_text_parts)

