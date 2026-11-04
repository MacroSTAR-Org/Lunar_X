"""安全相关的通用工具：路径校验、ZIP 解压、URL 校验与原子写文件。

这一层刻意不依赖 Flask / aiohttp / 项目里的其他模块，方便单测，也避免
WebUI 为了用一个校验函数而把整个 bot 运行时拖起来。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import zipfile
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlparse

PLUGIN_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")

# 这些名字进 sys.modules 会顶掉标准库、框架包或启动入口，插件一律不许用
RESERVED_PLUGIN_NAMES = frozenset(
    {
        "asyncio",
        "builtins",
        "core",
        "flask",
        "http",
        "importlib",
        "json",
        "logging",
        "main",
        "multiprocessing",
        "os",
        "pathlib",
        "plugins",
        "re",
        "socket",
        "sqlite3",
        "ssl",
        "sys",
        "threading",
        "time",
        "typing",
        "unittest",
        "urllib",
        "webui",
    }
)

# 插件市场的默认可信下载域名。自建镜像可以通过 webui.json 的
# plugin_download_hosts 追加，仍然只允许 https。
DEFAULT_DOWNLOAD_HOSTS = frozenset(
    {
        "codeload.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "unisphere.macrostar.top",
    }
)

MAX_PLUGIN_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_PLUGIN_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_PLUGIN_ARCHIVE_FILES = 2000

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class SecurityError(ValueError):
    """输入被安全策略拒绝时抛出。"""


# ---------------------------------------------------------------------------
# 名称与路径
# ---------------------------------------------------------------------------


def is_safe_plugin_name(name: object) -> bool:
    """插件名白名单。

    插件名会直接参与目录名、模块名和 URL 路径，不能只靠 ``..`` 黑名单。
    这里只允许字母、数字、下划线、点和横线，且不允许点 / 横线开头。
    """
    if not isinstance(name, str):
        return False
    if not name or not PLUGIN_NAME_PATTERN.fullmatch(name):
        return False
    if ".." in name or name.endswith((".", " ")):
        return False
    return name.lower() not in RESERVED_PLUGIN_NAMES


def resolve_within(root: str | os.PathLike[str], *parts: str) -> Path:
    """把 ``parts`` 拼到 ``root`` 下，并保证结果没有跑出 root。

    绝对路径、``..``、符号链接最终都会被 ``resolve()`` 归一化后再检查，
    所以 ``Path(root) / "/etc"`` 这种“拼接丢弃 root”的情况也会被拒绝。
    """
    root_path = Path(root).resolve()
    target = root_path.joinpath(*parts).resolve()
    if target != root_path and root_path not in target.parents:
        raise SecurityError(f"路径越界: {target} 不在 {root_path} 内")
    return target


def plugin_install_path(plugins_root: str | os.PathLike[str], name: str) -> Path:
    """校验插件名并返回 ``plugins/<name>`` 的最终路径。"""
    if not is_safe_plugin_name(name):
        raise SecurityError(f"非法插件名: {name!r}")
    return resolve_within(plugins_root, name)


# ---------------------------------------------------------------------------
# ZIP
# ---------------------------------------------------------------------------


def _check_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or "\x00" in name:
        raise SecurityError("ZIP 中存在非法文件名")

    posix_name = PurePosixPath(name.replace("\\", "/"))
    windows_name = PureWindowsPath(name)

    if posix_name.is_absolute() or windows_name.drive or windows_name.is_absolute():
        raise SecurityError(f"ZIP 中存在绝对路径: {name}")
    if any(part == ".." for part in posix_name.parts):
        raise SecurityError(f"ZIP 中存在路径穿越: {name}")

    mode = info.external_attr >> 16
    if mode and stat.S_ISLNK(mode):
        raise SecurityError(f"ZIP 中存在符号链接: {name}")

    if info.file_size > MAX_PLUGIN_EXTRACTED_BYTES:
        raise SecurityError(f"ZIP 单文件过大: {name}")
    # 压缩比异常通常意味着 zip bomb
    if (
        info.file_size > 1024 * 1024
        and info.compress_size
        and info.file_size / info.compress_size > 1000
    ):
        raise SecurityError(f"ZIP 压缩比异常，疑似 zip bomb: {name}")


def safe_extract_zip(
    zip_path: str | os.PathLike[str],
    target_dir: str | os.PathLike[str],
    *,
    max_total_size: int = MAX_PLUGIN_EXTRACTED_BYTES,
    max_files: int = MAX_PLUGIN_ARCHIVE_FILES,
) -> tuple[int, int]:
    """安全解压 ZIP，返回 ``(文件数, 解压后总字节数)``。

    先完整校验所有条目再解压：绝对路径、``..``、Windows 盘符、符号链接、
    超大文件、zip bomb 比率都会被拒绝。
    """
    target = Path(target_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)

    total_size = 0
    file_count = 0

    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        if len(infos) > max_files:
            raise SecurityError(f"ZIP 文件数超过限制: {len(infos)} > {max_files}")

        for info in infos:
            _check_zip_member(info)
            if info.is_dir():
                continue
            file_count += 1
            total_size += info.file_size
            if total_size > max_total_size:
                raise SecurityError("ZIP 解压后体积超过限制")

        archive.extractall(target)

    return file_count, total_size


# ---------------------------------------------------------------------------
# 下载 URL
# ---------------------------------------------------------------------------


def is_private_host(host: str) -> bool:
    """判断 host 是否指向本机、内网或保留地址。"""
    normalized = host.rstrip(".").lower()
    if not normalized:
        return True
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    if normalized.endswith((".localhost", ".local", ".internal", ".home.arpa")):
        return True

    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global


def validate_download_url(url: object, allowed_hosts: set[str] | frozenset[str]) -> str:
    """校验插件下载 URL。

    只接受 HTTPS、无凭据、默认端口的公网地址，并且域名必须命中白名单。
    这样即使 WebUI 账号泄露，也不能拿安装接口去打内网或云元数据服务。
    """
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise SecurityError("下载地址为空或过长")

    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as e:
        raise SecurityError(f"下载地址格式错误: {e}") from None

    if parsed.scheme != "https":
        raise SecurityError("插件下载地址必须使用 https")
    if parsed.username or parsed.password:
        raise SecurityError("下载地址不允许携带用户名或密码")
    if port not in (None, 443):
        raise SecurityError("插件下载地址只允许 443 端口")

    host = (parsed.hostname or "").rstrip(".").lower()
    if not host or is_private_host(host):
        raise SecurityError("插件下载地址不能指向本机或内网")

    normalized_allowed = {h.rstrip(".").lower() for h in allowed_hosts}
    if host not in normalized_allowed:
        raise SecurityError(f"插件下载域名不在白名单内: {host}")

    return url


def host_from_url(url: object) -> str | None:
    """取出 https URL 的 host，供镜像域名自动加入白名单。"""
    if not isinstance(url, str) or not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "https":
        return None
    return (parsed.hostname or "").rstrip(".").lower() or None


# ---------------------------------------------------------------------------
# 摘要与日志
# ---------------------------------------------------------------------------


def sha256_file(path: str | os.PathLike[str], chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sanitize_log_text(value: object, limit: int = 64) -> str:
    """把用户输入压成单行安全文本，避免日志注入与 ANSI 控制字符。"""
    text = _CONTROL_CHARS.sub("?", str(value))
    return text[:limit]


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
    mode: int = 0o600,
) -> None:
    """写临时文件 + fsync + os.replace，避免断电留下半截 JSON。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        with suppress(OSError):
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, target)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_json(
    path: str | os.PathLike[str],
    data: Any,
    *,
    mode: int = 0o600,
) -> None:
    atomic_write_text(
        path,
        json.dumps(data, indent=2, ensure_ascii=False),
        mode=mode,
    )
