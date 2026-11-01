"""Lunar X 核心包。

这里刻意保持“空壳”：导入 ``core`` 不应该顺带创建日志文件、拉起 aiohttp
或注册插件。所有导出走 PEP 562 的惰性加载，``from core import LunarBot``
仍然可用，但 ``from core.security import ...`` 只会加载真正需要的模块。
"""

from __future__ import annotations

import importlib
from typing import Any

_EXPORTS = {
    # bot
    "LunarBot": ".bot",
    # connection / transport
    "WebSocketConnection": ".connection",
    "BaseTransport": ".transport",
    "MilkyTransport": ".transport",
    # diy
    "DiyAPI": ".diy",
    # events
    "Event": ".events",
    "EventFactory": ".events",
    "Events": ".events",
    "FriendAddNoticeEvent": ".events",
    "FriendRecallNoticeEvent": ".events",
    "FriendRequestEvent": ".events",
    "GroupAddRequestEvent": ".events",
    "GroupAdminNoticeEvent": ".events",
    "GroupBanNoticeEvent": ".events",
    "GroupDecreaseNoticeEvent": ".events",
    "GroupHonorNoticeEvent": ".events",
    "GroupIncreaseNoticeEvent": ".events",
    "GroupInviteRequestEvent": ".events",
    "GroupMessageEvent": ".events",
    "GroupPokeNoticeEvent": ".events",
    "GroupRecallNoticeEvent": ".events",
    "GroupUploadNoticeEvent": ".events",
    "HeartbeatMetaEvent": ".events",
    "LifecycleMetaEvent": ".events",
    "LunarStartListen": ".events",
    "LunarStopListen": ".events",
    "MessageEvent": ".events",
    "MetaEvent": ".events",
    "NoticeEvent": ".events",
    "PrivateMessageEvent": ".events",
    "RequestEvent": ".events",
    # logger
    "LunarLogger": ".logger",
    "logger": ".logger",
    # message
    "AtSegment": ".message",
    "BaseSegment": ".message",
    "FaceSegment": ".message",
    "FileSegment": ".message",
    "ForwardNodeSegment": ".message",
    "ForwardSegment": ".message",
    "ImageSegment": ".message",
    "MessageBuilder": ".message",
    "RecordSegment": ".message",
    "ReplySegment": ".message",
    "ReplyUtils": ".message",
    "TextSegment": ".message",
    "VideoSegment": ".message",
    # adapters / plugin manager
    "MilkyAdapter": ".milky_adapter",
    "OneBotAdapter": ".onebot_adapter",
    "PluginManager": ".plugin_manager",
}

__all__ = sorted(_EXPORTS)  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = importlib.import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
