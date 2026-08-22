from typing import Any

from .logger import logger
from .message import AtSegment, BaseSegment, ImageSegment, MessageBuilder, ReplySegment, TextSegment


class Event:
    def __init__(self, data: dict[str, Any] | None = None):
        if data is None:
            data = {}
        self.raw_data = data.copy()
        self.time = data.get("time")
        self.self_id = data.get("self_id")
        self.user_id: int | None = data.get("user_id")
        self.post_type = data.get("post_type")
        self.is_command: bool = False
        self.command: str | None = None
        self.args: str | None = None
        self.processed_text: str | None = None

    def get(self, key: str, default: Any = None) -> Any:
        # 优先从实例属性获取
        if hasattr(self, key):
            return getattr(self, key)
        # 其次从原始数据字典获取
        if key in self.raw_data:
            return self.raw_data.get(key, default)

        # 针对一些特殊或常用的键进行映射
        if key == "type":  # 兼容旧插件可能直接 event.get('type')
            return self.post_type
        if key in {"message_type", "notice_type", "request_type", "meta_event_type"}:
            return getattr(self, key, default)
        if key == "text" and isinstance(self, MessageEvent):  # 兼容 event.get('text')
            return self.get_text()
        if key == "raw_event":  # 兼容 event.get('raw_event')
            return self.raw_data

        return default

    def __getitem__(self, key: str) -> Any:
        """
        为兼容旧插件，允许字典式访问事件数据。
        优先从实例属性获取，其次从原始数据字典获取，
        并对一些常用键进行映射。
        """
        # 优先从实例属性获取
        if hasattr(self, key):
            return getattr(self, key)
        # 其次从原始数据字典获取
        if key in self.raw_data:
            return self.raw_data[key]

        # 针对一些特殊或常用的键进行映射
        if key == "type":  # 兼容 event['type']
            return self.post_type
        if key == "text" and isinstance(self, MessageEvent):  # 兼容 event['text']
            return self.get_text()
        if key == "message" and isinstance(self, MessageEvent):  # 兼容 event['message']
            return self.message  # 返回解析后的消息段列表
        if key == "raw_message" and isinstance(self, MessageEvent):  # 兼容 event['raw_message']
            return self.raw_message
        if key == "raw_event":  # 兼容 event['raw_event']
            return self.raw_data  # 返回完整的原始字典

        raise KeyError(f"'{self.__class__.__name__}' object has no attribute or key '{key}'")

    def log_event(self):
        if (
            "status" in self.raw_data
            and self.raw_data.get("status") == "ok"
            and "message_id" in self.raw_data.get("data", {})
        ):
            message_id = self.raw_data["data"]["message_id"]
            logger.success(f"自定义 API 消息发送成功 (message_id: {message_id})")
        else:
            logger.info(f"收到未知事件类型: {self.raw_data}")

    def __str__(self):
        return f"{self.__class__.__name__}(post_type={self.post_type})"


class MessageEvent(Event):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.message_id = data.get("message_id")
        self.user_id = data.get("user_id")
        self._raw_message_segments: list[dict] = data.get("message", [])
        self.message: list[BaseSegment] = []
        self.raw_message = data.get("raw_message", "")
        self.font = data.get("font", 0)

    def get_text(self) -> str:
        text_parts = []
        for segment in self.message:
            if isinstance(segment, TextSegment):
                text_parts.append(segment.text)
            elif isinstance(segment, AtSegment):
                text_parts.append(f"@{segment.qq}")
            elif isinstance(segment, ImageSegment):
                text_parts.append("[图片]")
            elif isinstance(segment, ReplySegment):
                text_parts.append("[回复]")
            else:
                text_parts.append(f"[{segment.type}]")
        return " ".join(text_parts)


class PrivateMessageEvent(MessageEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.sub_type = data.get("sub_type", "friend")
        self.sender = data.get("sender", {})

    def log_event(self):
        sender_info = self.sender.get("nickname", f"用户{self.user_id}")
        logger.info(f"收到来自{sender_info}({self.user_id})的消息: {self.get_text()}")

    def __str__(self):
        return f"PrivateMessageEvent(user_id={self.user_id}, message={self.get_text()})"


class GroupMessageEvent(MessageEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.sub_type = data.get("sub_type", "normal")
        self.sender = data.get("sender", {})
        self.anonymous = data.get("anonymous")

    def log_event(self):
        if self.anonymous:
            sender_info = f"匿名用户({self.anonymous.get('name', '未知')})"
        else:
            sender_info = self.sender.get(
                "card", self.sender.get("nickname", f"用户{self.user_id}")
            )
            if not sender_info:  # If card/nickname is empty, use user_id
                sender_info = f"用户{self.user_id}"
        logger.info(
            f"收到来自群{self.group_id}内{sender_info}({self.user_id})的消息:{self.get_text()}"
        )

    def __str__(self):
        return f"GroupMessageEvent(group_id={self.group_id}, user_id={self.user_id}, message={self.get_text()})"


class NoticeEvent(Event):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.notice_type = data.get("notice_type")


class GroupUploadNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.file = data.get("file", {})

    def log_event(self):
        file_name = self.file.get("name", "未知文件")
        user_info = f"用户{self.user_id}"
        logger.info(f"收到群{self.group_id}内 {user_info} 上传了文件 {file_name}")

    def __str__(self):
        return f"GroupUploadNoticeEvent(group_id={self.group_id}, user_id={self.user_id}, file={self.file.get('name')})"


class GroupAdminNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.sub_type = data.get("sub_type")

    def log_event(self):
        action = "设置" if self.sub_type == "set" else "取消"
        user_info = f"用户{self.user_id}"
        # Attempt to get nickname from raw_data, fallback to user_id
        nickname = self.raw_data.get("nickname", user_info)
        logger.info(f"群{self.group_id}内{action}了 {nickname}({user_info}) 的管理员身份")

    def __str__(self):
        action = "设置" if self.sub_type == "set" else "取消"
        return f"GroupAdminNoticeEvent({action}管理员, group_id={self.group_id}, user_id={self.user_id})"


class GroupIncreaseNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.operator_id = data.get("operator_id")
        self.sub_type = data.get("sub_type")  # 'approve', 'invite'

    def log_event(self):
        if self.operator_id and self.operator_id != self.user_id:
            logger.info(f"用户{self.user_id} 被用户{self.operator_id} 邀请加入群{self.group_id}")
        else:
            logger.info(f"用户{self.user_id} 加入群{self.group_id}")

    def __str__(self):
        return f"GroupIncreaseNoticeEvent(group_id={self.group_id}, user_id={self.user_id}, sub_type={self.sub_type})"


class GroupDecreaseNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.operator_id = data.get("operator_id")
        self.sub_type = data.get("sub_type")  # 'leave', 'kick', 'kick_me'

    def log_event(self):
        if self.sub_type == "leave":
            logger.info(f"用户{self.user_id} 离开了群{self.group_id}")
        elif self.sub_type == "kick":
            logger.info(f"用户{self.user_id} 被管理员({self.operator_id})移出群{self.group_id}")
        elif self.sub_type == "kick_me":
            logger.info(f"机器人({self.self_id})被管理员({self.operator_id})移出群{self.group_id}")
        else:
            logger.info(f"用户{self.user_id} 离开群{self.group_id} (未知原因)")

    def __str__(self):
        return f"GroupDecreaseNoticeEvent(group_id={self.group_id}, user_id={self.user_id}, sub_type={self.sub_type})"


class GroupBanNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.operator_id = data.get("operator_id")
        self.sub_type = data.get("sub_type")
        self.duration = data.get("duration", 0)

    def log_event(self):
        if self.sub_type == "ban":
            if self.operator_id == self.self_id:
                logger.info(f"将用户{self.user_id} 在群{self.group_id} 禁言 {self.duration}秒")
            else:
                logger.info(
                    f"用户{self.user_id} 在群{self.group_id} 被用户{self.operator_id} 禁言 {self.duration}秒"
                )
        else:
            if self.operator_id == self.self_id:
                logger.info(f"将用户{self.user_id} 在群{self.group_id} 解除禁言")
            else:
                logger.info(
                    f"用户{self.user_id} 在群{self.group_id} 被用户{self.operator_id} 解除禁言"
                )

    def __str__(self):
        action = "禁言" if self.sub_type == "ban" else "解除禁言"
        return f"GroupBanNoticeEvent({action}, group_id={self.group_id}, user_id={self.user_id}, duration={self.duration})"


class FriendAddNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.user_id = data.get("user_id")

    def log_event(self):
        logger.info(f"用户{self.user_id} 添加了Bot好友")

    def __str__(self):
        return f"FriendAddNoticeEvent(user_id={self.user_id})"


class GroupRecallNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.operator_id = data.get("operator_id")
        self.message_id = data.get("message_id")

    def log_event(self):
        if self.operator_id == self.user_id:
            logger.info(
                f"用户{self.user_id} 在群{self.group_id} 撤回了自己的消息({self.message_id})"
            )
        else:
            logger.info(
                f"用户{self.user_id} 在群{self.group_id} 的消息({self.message_id})被用户{self.operator_id} 撤回"
            )

    def __str__(self):
        return f"GroupRecallNoticeEvent(group_id={self.group_id}, user_id={self.user_id}, operator_id={self.operator_id})"


class FriendRecallNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.user_id = data.get("user_id")
        self.message_id = data.get("message_id")

    def log_event(self):
        logger.info(f"用户{self.user_id} 撤回了发给机器人的消息({self.message_id})")

    def __str__(self):
        return f"FriendRecallNoticeEvent(user_id={self.user_id})"


class GroupPokeNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.target_id = data.get("target_id")

    def log_event(self):
        if self.target_id == self.self_id:
            logger.info(f"用户{self.user_id} 在群{self.group_id} 戳了机器人")
        else:
            logger.info(f"用户{self.user_id} 在群{self.group_id} 戳了用户{self.target_id}")

    def __str__(self):
        return f"GroupPokeNoticeEvent(group_id={self.group_id}, user_id={self.user_id}, target_id={self.target_id})"


class GroupHonorNoticeEvent(NoticeEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.honor_type: str | None = data.get("honor_type")

    def log_event(self):
        honor_map = {"talkative": "龙王", "performer": "群聊之火", "emotion": "快乐源泉"}
        honor_name = honor_map.get(self.honor_type or "", self.honor_type)
        logger.info(f"用户{self.user_id} 在群{self.group_id} 获得了 {honor_name} 荣誉")

    def __str__(self):
        return f"GroupHonorNoticeEvent(group_id={self.group_id}, user_id={self.user_id}, honor_type={self.honor_type})"


class RequestEvent(Event):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.request_type = data.get("request_type")
        # 确保 comment 字段如果不存在或为 None 时，默认为空字符串
        self.comment = data.get("comment", "") if data.get("comment") is not None else ""
        self.flag = data.get("flag", "")


class FriendRequestEvent(RequestEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.user_id = data.get("user_id")

    def log_event(self):
        # 确保日志输出时 comment 字段不会是 None
        comment_display = self.comment if self.comment else "无备注"
        logger.info(f"用户{self.user_id} 请求添加好友，备注: {comment_display}")

    def __str__(self):
        return f"FriendRequestEvent(user_id={self.user_id}, comment={self.comment})"


class GroupAddRequestEvent(RequestEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.sub_type = data.get("sub_type")  # 'add'

    def log_event(self):
        # 确保日志输出时 comment 字段不会是 None
        comment_display = self.comment if self.comment else "无备注"
        logger.info(f"用户{self.user_id} 申请加入群{self.group_id}，备注: {comment_display}")

    def __str__(self):
        return f"GroupAddRequestEvent(group_id={self.group_id}, user_id={self.user_id}, comment={self.comment})"


class GroupInviteRequestEvent(RequestEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.group_id = data.get("group_id")
        self.user_id = data.get("user_id")
        self.sub_type = data.get("sub_type")  # 'invite'

    def log_event(self):
        logger.info(f"用户{self.user_id} 邀请机器人加入群{self.group_id}")

    def __str__(self):
        return f"GroupInviteRequestEvent(group_id={self.group_id}, user_id={self.user_id})"


class MetaEvent(Event):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.meta_event_type = data.get("meta_event_type")


class HeartbeatMetaEvent(MetaEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.status = data.get("status", {})
        self.interval = data.get("interval", 0)

    def log_event(self):
        logger.debug(f"心跳事件: 间隔 {self.interval}ms，状态正常")

    def __str__(self):
        return f"HeartbeatMetaEvent(interval={self.interval})"


class LifecycleMetaEvent(MetaEvent):
    def __init__(self, data: dict[str, Any]):
        super().__init__(data)
        self.sub_type = data.get("sub_type")

    def log_event(self):
        if self.sub_type == "enable":
            logger.info("生命周期: 插件启用")
        elif self.sub_type == "disable":
            logger.info("生命周期: 插件禁用")
        elif self.sub_type == "connect":
            logger.info("生命周期: 连接建立")
        else:
            logger.info(f"生命周期: {self.sub_type}")

    def __str__(self):
        return f"LifecycleMetaEvent(sub_type={self.sub_type})"


class LunarStartListen(Event):
    def __init__(self):
        super().__init__(
            {
                "post_type": "meta_event",
                "meta_event_type": "lunar_framework",
                "sub_type": "start_listen",
            }
        )

    def log_event(self):
        logger.info("框架开始监听事件")

    def __str__(self):
        return "LunarStartListen()"


class LunarStopListen(Event):
    def __init__(self):
        super().__init__(
            {
                "post_type": "meta_event",
                "meta_event_type": "lunar_framework",
                "sub_type": "stop_listen",
            }
        )

    def log_event(self):
        logger.info("框架停止监听事件")

    def __str__(self):
        return "LunarStopListen()"


class EventFactory:
    @staticmethod
    def create_event(data: dict[str, Any], message_builder: MessageBuilder | None = None) -> Event:
        post_type = data.get("post_type")

        event: Event

        if post_type == "message":
            message_type = data.get("message_type")
            if message_type == "private":
                event = PrivateMessageEvent(data)
            elif message_type == "group":
                event = GroupMessageEvent(data)
            else:
                event = MessageEvent(data)

            if message_builder and isinstance(event, MessageEvent):
                event.message = message_builder.gen_message(event._raw_message_segments)

            return event

        elif post_type == "notice":
            notice_type = data.get("notice_type")
            if notice_type == "group_upload":
                event = GroupUploadNoticeEvent(data)
            elif notice_type == "group_admin":
                event = GroupAdminNoticeEvent(data)
            elif notice_type == "group_increase":
                event = GroupIncreaseNoticeEvent(data)
            elif notice_type == "group_decrease":
                event = GroupDecreaseNoticeEvent(data)
            elif notice_type == "group_ban":
                event = GroupBanNoticeEvent(data)
            elif notice_type == "friend_add":
                event = FriendAddNoticeEvent(data)
            elif notice_type == "group_recall":
                event = GroupRecallNoticeEvent(data)
            elif notice_type == "friend_recall":
                event = FriendRecallNoticeEvent(data)
            elif notice_type == "notify" and data.get("sub_type") == "poke":
                event = GroupPokeNoticeEvent(data)
            elif notice_type == "notify" and data.get("sub_type") == "honor":
                event = GroupHonorNoticeEvent(data)
            else:
                event = NoticeEvent(data)
            return event

        elif post_type == "request":
            request_type = data.get("request_type")
            if request_type == "friend":
                event = FriendRequestEvent(data)
            elif request_type == "group":
                sub_type = data.get("sub_type")
                if sub_type == "add":
                    event = GroupAddRequestEvent(data)
                elif sub_type == "invite":
                    event = GroupInviteRequestEvent(data)
                else:
                    event = RequestEvent(data)
            else:
                event = RequestEvent(data)
            return event

        elif post_type == "meta_event":
            meta_event_type = data.get("meta_event_type")
            if meta_event_type == "heartbeat":
                event = HeartbeatMetaEvent(data)
            elif meta_event_type == "lifecycle":
                event = LifecycleMetaEvent(data)
            else:
                event = MetaEvent(data)
            return event

        else:
            event = Event(data)
            return event


class Events:
    Event = Event
    MessageEvent = MessageEvent
    PrivateMessageEvent = PrivateMessageEvent
    GroupMessageEvent = GroupMessageEvent
    NoticeEvent = NoticeEvent
    GroupUploadNoticeEvent = GroupUploadNoticeEvent
    GroupAdminNoticeEvent = GroupAdminNoticeEvent
    GroupIncreaseNoticeEvent = GroupIncreaseNoticeEvent
    GroupDecreaseNoticeEvent = GroupDecreaseNoticeEvent
    GroupBanNoticeEvent = GroupBanNoticeEvent
    FriendAddNoticeEvent = FriendAddNoticeEvent
    GroupRecallNoticeEvent = GroupRecallNoticeEvent
    FriendRecallNoticeEvent = FriendRecallNoticeEvent
    GroupPokeNoticeEvent = GroupPokeNoticeEvent
    GroupHonorNoticeEvent = GroupHonorNoticeEvent
    RequestEvent = RequestEvent
    FriendRequestEvent = FriendRequestEvent
    GroupAddRequestEvent = GroupAddRequestEvent
    GroupInviteRequestEvent = GroupInviteRequestEvent
    MetaEvent = MetaEvent
    HeartbeatMetaEvent = HeartbeatMetaEvent
    LifecycleMetaEvent = LifecycleMetaEvent
    LunarStartListen = LunarStartListen
    LunarStopListen = LunarStopListen
    EventFactory = EventFactory
