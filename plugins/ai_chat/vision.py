"""图片理解：从消息里取图 → 下载/解码 → vision 模型的 content 片段。

来源分派按 ImageSegment.file 的形态（core/message.py:40-48）：
- http(s)://   直接下载（Milky 接收侧自带 temp_url，milky_adapter.py:430-436）
- base64://    直接解码
- file://      本地读取（协议端与机器人同机的场景）
- 其他（纯资源 ID）先调 get_image 换取临时 URL（双协议映射已存在）再下载

失败策略：逐图跳过；全部失败时调用方降级为纯文本请求。
历史里只存"[图片]"占位，vision 片段仅当次请求有效——
旧图的临时 URL 会过期，塞进历史只会撑大 payload。
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp

DOWNLOAD_TIMEOUT = 15
DEFAULT_MAX_IMAGES = 2
DEFAULT_MAX_IMAGE_MB = 5

# 常见图片格式的魔数；猜不出就当 jpeg，协议端一般给的就是它
_MAGIC_MIMES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"RIFF", "image/webp"),
    (b"BM", "image/bmp"),
)


def collect_image_segments(message: list[Any]) -> list[Any]:
    """按消息顺序取图片段。"""
    return [seg for seg in message if getattr(seg, "type", "") == "image"]


def _sniff_mime(data: bytes) -> str:
    for magic, mime in _MAGIC_MIMES:
        if data.startswith(magic):
            return mime
    return "image/jpeg"


def to_data_url(data: bytes) -> str:
    return f"data:{_sniff_mime(data)};base64,{base64.b64encode(data).decode()}"


def _max_bytes(cfg: dict) -> int:
    try:
        mb = float(cfg.get("vision.max_image_mb", DEFAULT_MAX_IMAGE_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_IMAGE_MB
    return max(1, min(int(mb), 10)) * 1024 * 1024


async def _download(session: aiohttp.ClientSession, url: str, max_bytes: int) -> bytes | None:
    """流式下载，超限即中止；URL 来自协议端而非用户文本，SSRF 风险低，
    但仍只放行 http(s)。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)) as resp:
            if resp.status != 200:
                return None
            buffer = bytearray()
            async for chunk in resp.content.iter_chunked(64 * 1024):
                buffer.extend(chunk)
                if len(buffer) > max_bytes:
                    return None
            return bytes(buffer)
    except (aiohttp.ClientError, TimeoutError):
        return None


def _decode_base64(value: str, max_bytes: int) -> bytes | None:
    try:
        data = base64.b64decode(value, validate=False)
    except (binascii.Error, ValueError):
        return None
    return data if 0 < len(data) <= max_bytes else None


def _read_local(value: str, max_bytes: int) -> bytes | None:
    """file:// 形态：本地路径。解析失败或超限返回 None。"""
    path = Path(value)
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _extract_url(payload: Any) -> str | None:
    """从 get_image 的返回里抠出可下载的地址/数据。

    OneBot 返回 {file, url, base64?...}；Milky 的 get_resource_temp_url
    返回 {temp_url}——字段名不统一，按常见键逐个试。
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        return None
    for key in ("url", "temp_url", "file", "base64"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


async def resolve_image(
    session: aiohttp.ClientSession, bot, file_value: str, max_bytes: int
) -> bytes | None:
    """按 file 形态分派，返回图片字节；失败返回 None。"""
    value = (file_value or "").strip()
    if not value:
        return None

    if value.startswith("base64://"):
        return _decode_base64(value[len("base64://") :], max_bytes)
    if value.startswith("file://"):
        return _read_local(value[len("file://") :], max_bytes)
    if value.startswith(("http://", "https://")):
        return await _download(session, value, max_bytes)

    # 纯资源 ID：调协议端换临时地址（返回值可能是 URL，也可能是 base64）
    try:
        payload = await bot.diy.get_image(file=value)
    except Exception:
        return None
    resolved = _extract_url(payload)
    if not resolved:
        return None
    if resolved.startswith("base64://"):
        return _decode_base64(resolved[len("base64://") :], max_bytes)
    if resolved.startswith(("http://", "https://")):
        return await _download(session, resolved, max_bytes)
    return _decode_base64(resolved, max_bytes)


async def build_image_parts(
    session: aiohttp.ClientSession, bot, event: Any, cfg: dict
) -> list[dict[str, Any]]:
    """把消息里的图片转成 vision content 片段；失败的图直接丢弃。

    返回 [] 表示没有可用图片，调用方降级为纯文本请求。
    """
    try:
        max_images = max(1, min(int(cfg.get("vision.max_images", DEFAULT_MAX_IMAGES)), 4))
    except (TypeError, ValueError):
        max_images = DEFAULT_MAX_IMAGES
    max_bytes = _max_bytes(cfg)

    parts: list[dict[str, Any]] = []
    for seg in collect_image_segments(getattr(event, "message", []))[:max_images]:
        file_value = str(getattr(seg, "data", {}).get("file", "") or "")
        data = await resolve_image(session, bot, file_value, max_bytes)
        if data:
            parts.append({"type": "image_url", "image_url": {"url": to_data_url(data)}})
    return parts
