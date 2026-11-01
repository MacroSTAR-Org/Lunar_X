import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse

import psutil
import requests
from flask import (
    Flask,
    Response,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
    url_for,
)
from flask.logging import default_handler

from core import plugin_config as pc
from core.security import (
    DEFAULT_DOWNLOAD_HOSTS,
    MAX_PLUGIN_ARCHIVE_BYTES,
    SecurityError,
    atomic_write_json,
    host_from_url,
    is_safe_plugin_name,
    plugin_install_path,
    resolve_within,
    safe_extract_zip,
    sanitize_log_text,
    sha256_file,
    validate_download_url,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config.update(
    UPLOAD_FOLDER=os.path.join(BASE_DIR, "uploads"),
    PLUGINS_DIR=os.path.join(BASE_DIR, "plugins"),
    TEMPLATES_AUTO_RELOAD=True,
    SEND_FILE_MAX_AGE_DEFAULT=300,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("LUNARX_COOKIE_SECURE", "0") == "1",
    SESSION_COOKIE_NAME="lunarx_session",
)

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["PLUGINS_DIR"], exist_ok=True)

APPSETTINGS_PATH = os.path.join(BASE_DIR, "appsettings.json")
CONFIG_JSON_PATH = os.path.join(BASE_DIR, "config.json")
ADMIN_JSON_PATH = os.path.join(BASE_DIR, "admin114.json")
WEBUI_JSON_PATH = os.path.join(BASE_DIR, "webui.json")
INITIAL_PASSWORD_PATH = os.path.join(BASE_DIR, "webui.initial_password")
LOG_FILE = os.path.join(BASE_DIR, "logs", "lunarx.log")

# 只允许通过“设置”接口读写的 webui.json 字段。密码哈希、session_secret
# 与 GitHub PAT 永远不经 GET 返回给浏览器。
WEBUI_PUBLIC_KEYS = (
    "use_pypi_mirror",
    "pypi_mirror",
    "github_mirror",
    "plugins_index_repo",
    "market_source_order",
    "plugin_download_hosts",
    "session_ttl_minutes",
    "allowed_ips",
    "login_fail_max",
    "login_lock_minutes",
)

SECRET_PLACEHOLDER = "__LUNARX_KEEP_SECRET__"


def _safe_plugin_name(name: str) -> bool:
    """插件名白名单校验，统一走 core.security。"""
    return is_safe_plugin_name(name)



def load_webui_config() -> dict:
    try:
        with open(WEBUI_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_webui_config(config: dict) -> None:
    atomic_write_json(WEBUI_JSON_PATH, config, mode=0o600)


def get_session_secret() -> str:
    """会话密钥：从 webui.json 读取，不存在则生成并持久化（重启不失效）"""
    config = load_webui_config()
    secret = config.get("session_secret")
    if isinstance(secret, str) and secret:
        return secret
    secret = secrets.token_hex(32)
    config["session_secret"] = secret
    save_webui_config(config)
    return secret


def _session_epoch(config: dict | None = None) -> int:
    cfg = config if config is not None else load_webui_config()
    try:
        return int(cfg.get("session_epoch", 0))
    except (TypeError, ValueError):
        return 0


def _is_default_credentials(config: dict | None = None) -> bool:
    cfg = config if config is not None else load_webui_config()
    return cfg.get("username") == "lunarx" and verify_password(
        "lunarx", cfg.get("password_hash", "")
    )


# ---------- 密码 ----------

PBKDF2_ITERATIONS = 600_000


def hash_password(
    password: str, salt: str | None = None, iterations: int = PBKDF2_ITERATIONS
) -> str:
    """PBKDF2-SHA256 密码哈希，格式：pbkdf2$iterations$salt$digest。"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        int(iterations),
    ).hex()
    return f"pbkdf2${int(iterations)}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    """兼容旧的 pbkdf2$salt$digest 与新的带迭代次数格式，常量时间比较。"""
    if not isinstance(stored, str):
        return False
    try:
        parts = stored.split("$")
        if len(parts) == 3:
            prefix, salt, digest = parts
            iterations = 100_000
        elif len(parts) == 4:
            prefix, iterations_text, salt, digest = parts
            iterations = int(iterations_text)
        else:
            return False
        if prefix != "pbkdf2" or iterations < 10_000 or iterations > 2_000_000:
            return False
        expected = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
        ).hex()
        return hmac.compare_digest(expected, digest)
    except (ValueError, TypeError):
        return False


# 用户不存在时也跑一次同样成本的哈希，避免用响应时间枚举用户名
_DUMMY_HASH = hash_password(secrets.token_hex(16))


# ---------- 登录安全（会话有效期 / IP 白名单 / 失败锁定 / 登录日志） ----------

LOGIN_LOG_PATH = os.path.join(BASE_DIR, "logs", "webui_login.log")
LOGIN_LOG_MAX_BYTES = 2 * 1024 * 1024

# 与安全相关的 webui.json 字段白名单（只允许这些键经接口读写）
SECURITY_KEYS = ("session_ttl_minutes", "allowed_ips", "login_fail_max", "login_lock_minutes")

# IP → 失败计数与锁定状态（进程内，重启清零；锁定信息同时写入登录日志）
_login_failures: dict[str, dict[str, float]] = {}

CSRF_EXEMPT_PATHS = {"/login", "/api/login", "/api/auth_status", "/api/csrf"}
PASSWORD_CHANGE_ALLOWED = {
    "/api/auth_status",
    "/api/check_default_credentials",
    "/api/csrf",
    "/api/change_password",
    "/api/logout",
    "/api/version",
    "/login",
}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# ---------------------------------------------------------------------------
# CSP（内容安全策略）
# ---------------------------------------------------------------------------
# 前端是 Vue 3 全局构建版，模板写在 JS 字符串里，运行时用 Function()
# 编译成渲染函数 —— 这就要求 script-src 里带 'unsafe-eval'，
# 否则整个面板白屏。除这一项外，其余全部按最严收紧：
#
#   * inline script 用 per-request nonce 白名单，不给 'unsafe-inline'
#   * 所有资源都来自本站，connect-src 'self' 挡住外连
#   * object-src / base-uri / form-action 全封死，堵掉注入型攻击的落地手段
#
# 'unsafe-eval' 的正确解法是换成 runtime-only 构建并预编译模板，
# 但那需要引入打包工具链，收益不抵成本，暂不处理。
# ---------------------------------------------------------------------------

_CSP_TEMPLATE = (
    "default-src 'self'; "
    "script-src 'self' 'nonce-{nonce}' 'unsafe-eval'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


def _current_nonce() -> str:
    """取当前请求的 CSP nonce；没有就临时生成一个。"""
    nonce = getattr(g, "_csp_nonce", None)
    if not nonce:
        nonce = secrets.token_urlsafe(16)
        g._csp_nonce = nonce
    return nonce


def _build_csp(nonce: str) -> str:
    policy = _CSP_TEMPLATE.format(nonce=nonce)
    # upgrade-insecure-requests 只在确实是 https 时才加。
    # 局域网 http://192.168.x.x:5000 这类访问会被浏览器把所有子资源
    # 升级成 https 从而整页白屏，所以不能无条件开。
    # request.is_secure 依赖 X-Forwarded-Proto，反代场景需配 LUNARX_TRUST_PROXY。
    if request.is_secure:
        policy += "; upgrade-insecure-requests"
    return policy


@app.before_request
def _assign_csp_nonce():
    """每个 HTML 请求生成一个 nonce，模板里的 inline script 带上它。"""
    g._csp_nonce = secrets.token_urlsafe(16)
    return None


def _int_setting(value, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _security_settings() -> dict:
    """读取安全相关配置，缺省给保守默认值；坏值不会让所有请求 500。"""
    cfg = load_webui_config()
    allowed_ips = cfg.get("allowed_ips")
    if not isinstance(allowed_ips, list):
        allowed_ips = []
    return {
        "session_ttl_minutes": _int_setting(cfg.get("session_ttl_minutes"), 720),
        "allowed_ips": [str(ip).strip() for ip in allowed_ips if str(ip).strip()],
        "login_fail_max": _int_setting(cfg.get("login_fail_max"), 5, 1),
        "login_lock_minutes": _int_setting(cfg.get("login_lock_minutes"), 15, 1),
    }


def _ip_allowed(client_ip: str, allowed: list[str]) -> bool:
    """支持单个 IP 与 CIDR；配置非法项直接忽略。"""
    if not allowed:
        return True
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for item in allowed:
        try:
            if "/" in item:
                if address in ipaddress.ip_network(item, strict=False):
                    return True
            elif address == ipaddress.ip_address(item):
                return True
        except ValueError:
            continue
    return False


def _append_login_log(ip: str, username: str, ok: bool, reason: str = "") -> None:
    """登录成功/失败都落一行日志，供「设置 → 账户与安全 → 登录日志」查看。"""
    try:
        os.makedirs(os.path.dirname(LOGIN_LOG_PATH), exist_ok=True)
        if os.path.exists(LOGIN_LOG_PATH) and os.path.getsize(LOGIN_LOG_PATH) > LOGIN_LOG_MAX_BYTES:
            os.replace(LOGIN_LOG_PATH, LOGIN_LOG_PATH + ".1")

        existed = os.path.exists(LOGIN_LOG_PATH)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        result = "成功" if ok else "失败"
        safe_ip = sanitize_log_text(ip, 64)
        safe_user = sanitize_log_text(username or "-", 64)
        safe_reason = sanitize_log_text(reason, 120)
        line = f"{stamp} | {safe_ip} | {safe_user} | {result} | {safe_reason}"
        with open(LOGIN_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if not existed:
            os.chmod(LOGIN_LOG_PATH, 0o600)
    except Exception:
        pass


@app.after_request
def add_security_headers(response):
    """安全响应头 + 静态资源缓存策略。

    以前所有响应都带 no-store，30MB 字体和 vendor 每次刷新都重新下载。
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
    )
    if request.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=300"
        response.headers.pop("Pragma", None)
    else:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"

    # CSP 只对 HTML 页面有意义：JSON / SSE / 静态资源的 CSP 没有作用，
    # 加了反而干扰浏览器对它们的处理。
    if response.mimetype == "text/html":
        response.headers["Content-Security-Policy"] = _build_csp(_current_nonce())
    return response


@app.before_request
def require_login():
    """登录、CSRF、会话有效期、IP 白名单与默认凭据强制改密的统一入口。"""
    if request.path.startswith("/static/") or request.path in CSRF_EXEMPT_PATHS:
        return None

    sec = _security_settings()
    client_ip = request.remote_addr or ""
    is_api = request.path.startswith("/api/")

    if not _ip_allowed(client_ip, sec["allowed_ips"]):
        if is_api:
            return jsonify({"error": "来源 IP 不在白名单内"}), 403
        return "Forbidden", 403

    if not session.get("logged_in"):
        if is_api:
            return jsonify({"error": "未登录"}), 401
        return redirect(url_for("login_page"))

    config = load_webui_config()
    if session.get("epoch") != _session_epoch(config):
        session.clear()
        if is_api:
            return jsonify({"error": "会话已失效，请重新登录"}), 401
        return redirect(url_for("login_page"))

    ttl = sec["session_ttl_minutes"]
    if ttl > 0:
        login_at = session.get("login_time") or 0
        if time.time() - login_at > ttl * 60:
            session.clear()
            if is_api:
                return jsonify({"error": "会话已过期，请重新登录"}), 401
            return redirect(url_for("login_page"))

    if request.method in MUTATING_METHODS:
        expected = session.get("csrf_token") or ""
        actual = request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(str(expected), str(actual)):
            return jsonify({"error": "CSRF token 无效，请刷新页面后重试"}), 403

    if session.get("must_change_password") and request.path not in PASSWORD_CHANGE_ALLOWED:
        return (
            jsonify(
                {
                    "error": "当前仍在使用默认凭据，请先修改密码",
                    "code": "password_change_required",
                }
            ),
            403,
        )

    return None


class CustomFormatter(logging.Formatter):
    def format(self, record):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        level = record.levelname
        if level == "INFO":
            level = "\033[94mINFO\033[0m"
        elif level == "WARNING":
            level = "\033[93mWARNING\033[0m"
        elif level == "ERROR":
            level = "\033[91mERROR\033[0m"
        return f"[{timestamp}] [Lunar_WebUI] ℹ️ {level} {record.getMessage()}"


handler = logging.StreamHandler()
handler.setFormatter(CustomFormatter())
app.logger.removeHandler(default_handler)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)


def _write_initial_password(password: str) -> None:
    """首次生成的随机密码写入 0600 文件，用户登录后改密即删除。"""
    with open(INITIAL_PASSWORD_PATH, "w", encoding="utf-8") as f:
        f.write(password + "\n")
    with contextlib.suppress(OSError):
        os.chmod(INITIAL_PASSWORD_PATH, 0o600)


def _new_webui_config(initial_password: str) -> dict:
    return {
        "use_pypi_mirror": False,
        "pypi_mirror": "https://pypi.tuna.tsinghua.edu.cn/simple",
        "github_mirror": "",
        "github_pat": "",
        "plugins_index_repo": "MacroSTAR-Org/Unisphere",
        "market_source_order": ["unisphere", "github"],
        "plugin_download_hosts": [],
        "username": "lunarx",
        "password_hash": hash_password(initial_password),
        "session_secret": secrets.token_hex(32),
        "session_epoch": 0,
        "session_ttl_minutes": 720,
        "allowed_ips": [],
        "login_fail_max": 5,
        "login_lock_minutes": 15,
    }


def init_default_configs():
    protocol_token = secrets.token_hex(16)

    if not os.path.exists(APPSETTINGS_PATH):
        default_appsettings = {
            "$schema": "https://raw.githubusercontent.com/LagrangeDev/Lagrange.Core/master/Lagrange.OneBot/Resources/appsettings_schema.json",
            "Logging": {
                "LogLevel": {
                    "Default": "Information",
                    "Microsoft": "Warning",
                    "Microsoft.Hosting.Lifetime": "Information",
                }
            },
            "SignServerUrl": "https://sign.lagrangecore.org/api/sign/39038",
            "SignProxyUrl": "",
            "MusicSignServerUrl": "",
            "Account": {
                "Uin": 0,
                "Protocol": "Linux",
                "AutoReconnect": True,
                "GetOptimumServer": True,
            },
            "Message": {"IgnoreSelf": True, "StringPost": False},
            "QrCode": {"ConsoleCompatibilityMode": False},
            "Implementations": [
                {
                    "Type": "ForwardWebSocket",
                    "Host": "127.0.0.1",
                    "Port": 3803,
                    "HeartBeatInterval": 5000,
                    "AccessToken": protocol_token,
                }
            ],
        }
        atomic_write_json(APPSETTINGS_PATH, default_appsettings, mode=0o600)

    if not os.path.exists(CONFIG_JSON_PATH):
        default_config = {
            "protocol": "milky",
            "milky_server": "http://127.0.0.1:3010",
            "milky_token": "",
            "ws_server": "ws://127.0.0.1:3803",
            "token": protocol_token,
            "bot_qq": 0,
            # 0 表示没有默认超级用户：请在 WebUI「用户」页把自己加进 super_users
            "root_user": 0,
            "log_level": "INFO",
            "trigger_keyword": "$",
            "auto_reload_plugins": True,
            "bot_name": "Lunar X",
            "bot_name_en": "Lunar",
        }
        atomic_write_json(CONFIG_JSON_PATH, default_config, mode=0o600)

    if not os.path.exists(ADMIN_JSON_PATH):
        # 不再硬编码作者或其他人的 QQ 号作为默认管理员
        atomic_write_json(
            ADMIN_JSON_PATH,
            {"super_users": [], "manager_users": []},
            mode=0o600,
        )

    if not os.path.exists(WEBUI_JSON_PATH):
        initial_password = secrets.token_urlsafe(12)
        save_webui_config(_new_webui_config(initial_password))
        _write_initial_password(initial_password)
        print(
            "\n[WebUI] 已生成初始登录凭据\n"
            f"  用户名: lunarx\n"
            f"  密码:   {initial_password}\n"
            f"  密码也保存在: {INITIAL_PASSWORD_PATH}\n"
            "  首次登录后请立即修改密码。\n",
            flush=True,
        )
        return

    try:
        with open(WEBUI_JSON_PATH, encoding="utf-8") as f:
            webui_config = json.load(f)
        if not isinstance(webui_config, dict):
            raise ValueError("webui.json 不是 JSON 对象")
    except (OSError, ValueError) as e:
        if os.getenv("LUNARX_WEBUI_RESET") != "1":
            raise RuntimeError(
                f"webui.json 无法解析（{e}）。为避免静默重置管理员密码，WebUI 拒绝启动。"
                "确认要重置时，请备份并设置 LUNARX_WEBUI_RESET=1 后重新启动。"
            ) from e

        backup = f"{WEBUI_JSON_PATH}.corrupt.{int(time.time())}"
        os.replace(WEBUI_JSON_PATH, backup)
        app.logger.warning(f"webui.json 已损坏，已备份到 {backup}，将生成新的登录凭据")
        initial_password = secrets.token_urlsafe(12)
        save_webui_config(_new_webui_config(initial_password))
        _write_initial_password(initial_password)
        print(f"\n[WebUI] 已重置登录凭据，新密码: {initial_password}\n", flush=True)
        return

    updated = False
    defaults = {
        "github_pat": "",
        "plugins_index_repo": "MacroSTAR-Org/Unisphere",
        "market_source_order": ["unisphere", "github"],
        "plugin_download_hosts": [],
        "session_ttl_minutes": 720,
        "session_epoch": 0,
        "allowed_ips": [],
        "login_fail_max": 5,
        "login_lock_minutes": 15,
    }
    for key, value in defaults.items():
        if key not in webui_config:
            webui_config[key] = value
            updated = True

    if not webui_config.get("username") or not webui_config.get("password_hash"):
        # 缺凭据时生成随机密码，绝不回落到 lunarx/lunarx
        initial_password = secrets.token_urlsafe(12)
        webui_config["username"] = "lunarx"
        webui_config["password_hash"] = hash_password(initial_password)
        _write_initial_password(initial_password)
        app.logger.warning(f"webui.json 缺少登录凭据，已生成新密码并写入 {INITIAL_PASSWORD_PATH}")
        updated = True

    if not webui_config.get("session_secret"):
        webui_config["session_secret"] = secrets.token_hex(32)
        updated = True

    if updated:
        save_webui_config(webui_config)


init_default_configs()
app.secret_key = get_session_secret()


@app.route("/")
def index():
    return render_template("index.html", csp_nonce=_current_nonce())


@app.route("/login", endpoint="login_page")
def login_page():
    if session.get("logged_in"):
        return redirect(url_for("index"))
    return render_template("login.html", csp_nonce=_current_nonce())


def _get_csrf_token() -> str:
    token = session.get("csrf_token")
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.route("/api/auth_status", methods=["GET"])
def auth_status():
    config = load_webui_config()
    logged_in = bool(session.get("logged_in"))
    data = {
        "logged_in": logged_in,
        "username": session.get("username") or config.get("username", "lunarx"),
        "must_change_password": bool(session.get("must_change_password")),
    }
    if logged_in:
        data["csrf_token"] = _get_csrf_token()
    return jsonify(data)


@app.route("/api/csrf", methods=["GET"])
def csrf_token():
    if not session.get("logged_in"):
        return jsonify({"error": "未登录"}), 401
    return jsonify({"csrf_token": _get_csrf_token()})


@app.route("/api/check_default_credentials", methods=["GET"])
def check_default_credentials():
    return jsonify({"is_default": _is_default_credentials()})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    username = str(data.get("username", ""))[:64]
    password = str(data.get("password", ""))[:256]
    client_ip = request.remote_addr or ""

    sec = _security_settings()
    fail_max = sec["login_fail_max"]
    lock_min = sec["login_lock_minutes"]

    # 连续失败锁定：IP 维度，超过阈值后临时拒绝登录
    now = time.time()
    state = _login_failures.get(client_ip, {"count": 0, "until": 0})
    if state["until"] > now:
        remain = int(state["until"] - now) // 60 + 1
        _append_login_log(client_ip, username, False, f"IP 被锁定，剩余 {remain} 分钟")
        return jsonify({"error": f"失败次数过多，请 {remain} 分钟后再试"}), 403

    config = load_webui_config()
    stored_username = config.get("username", "lunarx")
    stored_hash = config.get("password_hash", "")

    # 无论用户名是否存在都跑一次 PBKDF2，避免时间侧信道枚举用户名
    user_ok = isinstance(stored_username, str) and hmac.compare_digest(
        username, stored_username
    )
    pass_ok = verify_password(
        password, stored_hash if isinstance(stored_hash, str) and stored_hash else _DUMMY_HASH
    )

    if user_ok and pass_ok:
        _login_failures.pop(client_ip, None)
        session.clear()
        session["logged_in"] = True
        session["username"] = username
        session["login_time"] = now
        session["epoch"] = _session_epoch(config)
        session["csrf_token"] = secrets.token_urlsafe(32)
        session["must_change_password"] = _is_default_credentials(config)
        safe_user = sanitize_log_text(username, 64)
        app.logger.info(f"WebUI 登录成功: {safe_user}")
        _append_login_log(client_ip, username, True)
        return jsonify(
            {
                "message": "登录成功",
                "username": username,
                "csrf_token": session["csrf_token"],
                "must_change_password": session["must_change_password"],
            }
        )

    # 失败：计数并记录日志，达到阈值则锁定该 IP
    state["count"] += 1
    if state["count"] >= fail_max:
        state["until"] = now + lock_min * 60
        state["count"] = 0
        reason = f"已达 {fail_max} 次失败，锁定 {lock_min} 分钟"
    else:
        reason = f"剩余 {fail_max - int(state['count'])} 次机会"
    _login_failures[client_ip] = state
    app.logger.warning(
        f"WebUI 登录失败: {sanitize_log_text(username, 64)} ({reason})"
    )
    _append_login_log(client_ip, username, False, reason)
    return jsonify({"error": "用户名或密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"message": "已退出登录"})


@app.route("/api/login_logs", methods=["GET"])
def login_logs():
    """读取登录日志（最近的，倒序返回），不存在时返回空列表"""
    limit = max(1, min(500, request.args.get("limit", default=100, type=int)))
    lines = []
    try:
        with open(LOGIN_LOG_PATH, encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f if ln.strip()]
    except OSError:
        pass
    return jsonify(lines[-limit:][::-1])


@app.route("/api/webui_security", methods=["GET"])
def get_webui_security():
    """返回当前安全设置（会话有效期 / IP 白名单 / 失败锁定阈值），不含敏感凭据"""
    cfg = load_webui_config()
    out = {k: cfg.get(k) for k in SECURITY_KEYS}
    sec = _security_settings()
    out["session_ttl_minutes"] = sec["session_ttl_minutes"]
    out["allowed_ips"] = sec["allowed_ips"]
    out["login_fail_max"] = sec["login_fail_max"]
    out["login_lock_minutes"] = sec["login_lock_minutes"]
    return jsonify(out)


def _normalize_allowed_ips(raw) -> list[str]:
    if isinstance(raw, str):
        parts = [item for item in re.split(r"[,，\s]+", raw) if item]
    elif isinstance(raw, list):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        raise ValueError("allowed_ips 必须是字符串或数组")

    normalized: list[str] = []
    for item in parts:
        try:
            if "/" in item:
                value = str(ipaddress.ip_network(item, strict=False))
            else:
                value = str(ipaddress.ip_address(item))
        except ValueError as e:
            raise ValueError(f"非法 IP/CIDR: {item}") from e
        normalized.append(value)
    return normalized


@app.route("/api/webui_security", methods=["POST"])
def set_webui_security():
    """更新安全设置，只接受白名单内的键，防止误覆盖凭据。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    cfg = load_webui_config()
    changed = {}

    if "session_ttl_minutes" in data:
        try:
            cfg["session_ttl_minutes"] = max(0, int(data["session_ttl_minutes"]))
            changed["session_ttl_minutes"] = cfg["session_ttl_minutes"]
        except (TypeError, ValueError):
            return jsonify({"error": "session_ttl_minutes 必须是整数"}), 400

    if "allowed_ips" in data:
        try:
            ips = _normalize_allowed_ips(data["allowed_ips"])
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        cfg["allowed_ips"] = ips
        changed["allowed_ips"] = ips

    if "login_fail_max" in data:
        try:
            cfg["login_fail_max"] = max(1, int(data["login_fail_max"]))
            changed["login_fail_max"] = cfg["login_fail_max"]
        except (TypeError, ValueError):
            return jsonify({"error": "login_fail_max 必须是整数"}), 400

    if "login_lock_minutes" in data:
        try:
            cfg["login_lock_minutes"] = max(1, int(data["login_lock_minutes"]))
            changed["login_lock_minutes"] = cfg["login_lock_minutes"]
        except (TypeError, ValueError):
            return jsonify({"error": "login_lock_minutes 必须是整数"}), 400

    if changed:
        save_webui_config(cfg)
        app.logger.info(f"WebUI 安全设置已更新: {sorted(changed)}")
    return jsonify(changed)


@app.route("/api/webui_security/force_logout", methods=["POST"])
def force_logout_all():
    """强制所有会话下线：轮换 session_secret 并递增会话代际。"""
    new_secret = secrets.token_hex(32)
    cfg = load_webui_config()
    cfg["session_secret"] = new_secret
    cfg["session_epoch"] = _session_epoch(cfg) + 1
    save_webui_config(cfg)
    app.secret_key = new_secret
    session.clear()
    app.logger.info("WebUI 已强制所有会话下线（session_secret 已轮换）")
    return jsonify({"message": "已强制所有会话下线，请重新登录"})


def _dir_size(path):
    """递归计算目录大小（字节）"""
    import contextlib

    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                with contextlib.suppress(OSError):
                    total += os.path.getsize(os.path.join(root, f))
    except OSError:
        pass
    return total


def _is_lunar_main_process(proc) -> bool:
    """只认本项目 main.py，避免误杀其他项目的同名入口。"""
    expected = os.path.abspath(os.path.join(BASE_DIR, "main.py"))
    try:
        cmdline = proc.info.get("cmdline") or []
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

    for arg in cmdline:
        if not isinstance(arg, str) or not arg.endswith("main.py"):
            continue
        if os.path.abspath(arg) == expected:
            return True
        try:
            if os.path.abspath(os.path.join(proc.cwd(), arg)) == expected:
                return True
        except (psutil.Error, OSError):
            continue
    return False


def _find_bot_process():
    """查找本项目主进程（python main.py），跨平台。"""
    candidates = []
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
            if _is_lunar_main_process(proc):
                candidates.append(proc)
    except Exception:
        pass
    if not candidates:
        return None
    # 排除 venv 启动器（Windows）与诊断脚本（-c）
    for proc in candidates:
        cl = " ".join(proc.info.get("cmdline") or [])
        if "-c" in cl:
            continue
        if os.name == "nt" and (".venv" in cl or "\\venv\\" in cl):
            continue
        return proc
    return candidates[0]


def _count_messages_by_day(days=7):
    """统计日志中近 N 天每日消息数（接收+发送）"""
    result = {}
    today = date.today()
    for i in range(days - 1, -1, -1):
        result[(today - timedelta(days=i)).isoformat()] = {"received": 0, "sent": 0}
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\[(\d{4}-\d{2}-\d{2})", line)
                if not m:
                    continue
                day = m.group(1)
                if day in result:
                    if "收到来自" in line:
                        result[day]["received"] += 1
                    elif "发送消息" in line:
                        result[day]["sent"] += 1
    except OSError:
        pass
    return result


def _check_qq_online():
    """通过 Milky API 探测 QQ 协议端在线状态。"""
    try:
        with open(CONFIG_JSON_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("protocol", "onebot_v11") != "milky":
            return {"online": False, "detail": "OneBot 模式暂不支持在线探测"}
        server = cfg.get("milky_server", "")
        # 不再回退到 OneBot 的 token：两套凭据混用会探测失败并泄露凭据
        token = cfg.get("milky_token", "")
        if not server:
            return {"online": False, "detail": "未配置 Milky 协议端 (milky_server)"}
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.post(f"{server}/api/get_login_info", json={}, headers=headers, timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "ok":
                uin = data.get("data", {}).get("uin")
                nickname = data.get("data", {}).get("nickname")
                return {"online": True, "detail": f"QQ {uin} ({nickname}) 在线", "uin": uin}
            return {"online": False, "detail": f"协议端响应异常: {data.get('message', '未知')}"}
        if resp.status_code == 401:
            return {"online": False, "detail": "协议端鉴权失败（milky_token 不匹配）"}
        return {"online": False, "detail": f"协议端 HTTP {resp.status_code}"}
    except requests.exceptions.ConnectionError:
        return {"online": False, "detail": "协议端未启动或无法连接"}
    except Exception as e:
        return {"online": False, "detail": str(e)}


def get_framework_version():
    """从 core/bot.py 注释读取框架版本号"""
    try:
        bot_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "core", "bot.py")
        with open(bot_py, encoding="utf-8") as f:
            for line in f:
                if "版本" in line:
                    m = re.search(r"BETA\s+([\d.]+)", line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return "0.1.0.122"


def _find_bot_processes():
    """查找所有本项目主进程（含 venv 启动器与真解释器）"""
    result = []
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            if _is_lunar_main_process(proc):
                result.append(proc)
    except Exception:
        pass
    return result


@app.route("/api/version", methods=["GET"])
def get_version():
    return jsonify({"version": get_framework_version()})


@app.route("/api/restart_bot", methods=["POST"])
def restart_bot():
    """重启 Lunar X 机器人进程（只处理本项目入口，先 TERM 后 KILL）。"""
    try:
        base_dir = BASE_DIR
        main_py = os.path.join(base_dir, "main.py")

        # Linux + systemd：走服务管理器重启，避免绕过 systemd 造成双实例竞争
        if os.name != "nt" and shutil.which("systemctl"):
            unit = os.getenv("LUNARX_SYSTEMD_UNIT", "lunarx")
            try:
                result = subprocess.run(
                    ["systemctl", "restart", unit],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                return jsonify({"error": f"systemctl 调用失败: {e}"}), 500

            if result.returncode != 0:
                detail = sanitize_log_text(result.stderr or result.stdout or "", 300)
                return (
                    jsonify({"error": f"systemctl restart {unit} 失败: {detail}"}),
                    500,
                )
            app.logger.info(f"机器人重启中（systemctl restart {unit}）")
            return jsonify({"message": "机器人重启中（systemd），请稍候约 5 秒"})

        # 1. 先 SIGTERM 优雅退出，超时才 KILL
        procs = _find_bot_processes()
        for proc in procs:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
                app.logger.warning(f"终止 bot 进程 {proc.pid} 失败: {e}")

        gone, alive = psutil.wait_procs(procs, timeout=10)
        for proc in alive:
            with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                proc.kill()
        killed = [proc.pid for proc in gone] + [proc.pid for proc in alive]

        # 2. 重新启动（独立进程，脱离 WebUI）
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        subprocess.Popen(
            [sys.executable, main_py],
            cwd=base_dir,
            creationflags=flags,
            start_new_session=os.name != "nt",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.logger.info(f"机器人重启完成（旧进程: {killed or '无'}，已启动新进程）")
        return jsonify({"message": "机器人重启中，请稍候约 5 秒", "killed": killed})
    except Exception as e:
        app.logger.error(f"重启机器人失败: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/dashboard", methods=["GET"])
def get_dashboard():
    """数据看板聚合数据"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data = {}

    # 1. 数据占用量（Lunar_X 相关目录）
    dirs = {}
    for name in ("logs", "plugins", "uploads", "templates", "core"):
        path = os.path.join(base_dir, name)
        dirs[name] = _dir_size(path) if os.path.exists(path) else 0
    dirs["config_files"] = sum(
        os.path.getsize(p) if os.path.exists(p) else 0
        for p in (CONFIG_JSON_PATH, ADMIN_JSON_PATH, WEBUI_JSON_PATH, APPSETTINGS_PATH)
    )
    data["storage"] = {
        "dirs": dirs,
        "total": sum(dirs.values()),
        "log_file": os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0,
    }

    # 2. 系统运行情况
    try:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(base_dir[:3] if os.name == "nt" else "/")
        data["system"] = {
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory_total": vm.total,
            "memory_used": vm.used,
            "memory_percent": vm.percent,
            "disk_total": disk.total,
            "disk_used": disk.used,
            "disk_percent": disk.percent,
            "boot_time": psutil.boot_time(),
        }
    except Exception as e:
        data["system"] = {"error": str(e)}

    # 3. Bot 进程 + CPU
    bot_proc = _find_bot_process()
    if bot_proc:
        try:
            cpu_times = bot_proc.cpu_times()
            cpu_seconds = cpu_times.user + cpu_times.system
            mem = bot_proc.memory_info()
            uptime = time.time() - bot_proc.create_time()
            day_start = time.mktime(date.today().timetuple())
            if bot_proc.create_time() < day_start:
                # 进程今日 0 点前启动：按今日经过时间比例估算今日 CPU 时长
                elapsed_today = max(time.time() - day_start, 0)
                cpu_seconds_today = cpu_seconds * elapsed_today / max(uptime, 1)
            else:
                cpu_seconds_today = cpu_seconds
            data["bot"] = {
                "running": True,
                "pid": bot_proc.pid,
                "cpu_seconds": cpu_seconds,
                "cpu_seconds_today": cpu_seconds_today,
                "memory_bytes": mem.rss,
                "uptime": uptime,
                "create_time": bot_proc.create_time(),
            }
        except Exception:
            data["bot"] = {"running": True, "pid": bot_proc.pid}
    else:
        data["bot"] = {"running": False}

    # 4. 在线情况
    data["qq"] = _check_qq_online()

    # 5. 7 天消息统计
    msg_by_day = _count_messages_by_day(7)
    data["messages"] = {
        "days": [{"date": d, **msg_by_day[d]} for d in sorted(msg_by_day)],
        "total_7d": sum(v["received"] + v["sent"] for v in msg_by_day.values()),
    }

    return jsonify(data)


# 日志读取统一用二进制 + 字节偏移量。
# 不能用文本模式的 seek(字节数)：文本流的 seek 只接受 tell() 返回的不透明游标，
# 传字节数在换行转换（Windows CRLF）和多字节 UTF-8 下会错位，导致重复行或乱码。
LOG_HISTORY_SCAN = 512 * 1024  # 取历史时最多回扫的字节数，避免整文件读进内存


def _read_log_tail(max_bytes=LOG_HISTORY_SCAN):
    """读取日志尾部，返回 (行列表, 文件当前字节大小)。文件不存在返回 (None, 0)。"""
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, "rb") as f:
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except FileNotFoundError:
        return None, 0
    if start > 0:
        # 回扫起点大概率落在某行中间，丢掉这半行
        nl = data.find(b"\n")
        data = data[nl + 1 :] if nl >= 0 else b""
    text = data.decode("utf-8", errors="replace")
    return text.splitlines(), size


@app.route("/api/logs", methods=["GET"])
def get_logs():
    """日志历史（最后 N 行）。

    同时返回 offset（当前文件字节大小），客户端应拿它作为 /api/logs/tail 的起点，
    这样历史与增量之间不会漏行也不会重复。
    """
    tail = request.args.get("tail", 200, type=int)
    tail = max(1, min(tail, 5000))
    try:
        lines, size = _read_log_tail()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    if lines is None:
        return jsonify({"lines": [], "exists": False, "offset": 0})
    return jsonify({"lines": lines[-tail:], "exists": True, "offset": size})


@app.route("/api/logs/tail", methods=["GET"])
def tail_logs():
    """长轮询增量日志。

    取代原先的 SSE 实现，解决三个实时性问题：
      1. 原来固定 time.sleep(1)，每行最多延迟 1 秒；这里 50ms 探测一次，
         一有新内容立即返回，端到端延迟约 50ms。
      2. 原来空闲时不吐任何字节，浏览器要等 15 秒心跳才认为连接建立，
         界面上会挂着「连接中」；长轮询每轮都会正常收尾，不存在这个空窗。
      3. 原来 last_size 与实际读到的长度可能不一致，重叠部分会重复推送。
         这里用「只消费到最后一个换行符」的方式对齐，偏移量由客户端回传。

    参数 offset：上次拿到的字节偏移量；传 -1（或不传）表示只要今后的新内容。
    返回 {offset, lines, exists, rotated}
    """
    try:
        offset = int(request.args.get("offset", -1))
    except (TypeError, ValueError):
        offset = -1

    deadline = time.time() + 20.0  # 单轮最长挂 20 秒，之后空返回让客户端续接
    interval = 0.05

    while True:
        try:
            size = os.path.getsize(LOG_FILE)
        except OSError:
            # 日志文件还没建出来，等它出现
            if time.time() >= deadline:
                return jsonify({"offset": 0, "lines": [], "exists": False, "rotated": False})
            time.sleep(interval)
            continue

        rotated = False
        if offset < 0:
            offset = size  # 首次连接：只要增量，不回放历史
        elif offset > size:
            offset, rotated = 0, True  # 文件被轮转/清空，从头再来

        if size > offset:
            with open(LOG_FILE, "rb") as f:
                f.seek(offset)
                data = f.read(size - offset)
            cut = data.rfind(b"\n")
            if cut >= 0:
                # 只消费完整行，剩下的半行留到下一轮，避免把写了一半的日志推出去
                consumed = data[: cut + 1]
                text = consumed.decode("utf-8", errors="replace")
                return jsonify(
                    {
                        "offset": offset + len(consumed),
                        "lines": text.splitlines(),
                        "exists": True,
                        "rotated": rotated,
                    }
                )

        if time.time() >= deadline:
            return jsonify({"offset": offset, "lines": [], "exists": True, "rotated": rotated})
        time.sleep(interval)


@app.route("/api/change_password", methods=["POST"])
def change_password():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    old_password = str(data.get("old_password", ""))[:256]
    new_password = str(data.get("new_password", ""))[:256]
    raw_username = data.get("new_username")

    config = load_webui_config()
    stored_hash = config.get("password_hash", "")

    if not verify_password(old_password, stored_hash):
        _append_login_log(
            request.remote_addr or "",
            session.get("username", ""),
            False,
            "改密时旧密码错误",
        )
        return jsonify({"error": "旧密码错误"}), 401

    password_changed = bool(new_password)
    if password_changed:
        if len(new_password) < 8:
            return jsonify({"error": "新密码长度至少 8 位"}), 400
        if len(new_password) > 128:
            return jsonify({"error": "新密码不能超过 128 位"}), 400
        if hmac.compare_digest(new_password, old_password):
            return jsonify({"error": "新密码不能与旧密码相同"}), 400
    elif _is_default_credentials(config) or session.get("must_change_password"):
        return jsonify({"error": "当前为默认凭据，必须同时设置新密码"}), 400

    new_username: str | None = None
    if raw_username is not None:
        new_username = str(raw_username).strip()
        if len(new_username) < 3 or len(new_username) > 64:
            return jsonify({"error": "用户名长度需在 3-64 位之间"}), 400
        if any(ch.isspace() or ord(ch) < 32 for ch in new_username):
            return jsonify({"error": "用户名不能包含空白或控制字符"}), 400

    if not password_changed and new_username is None:
        return jsonify({"error": "没有可修改的内容"}), 400

    if password_changed:
        config["password_hash"] = hash_password(new_password)
    if new_username is not None:
        config["username"] = new_username
        session["username"] = new_username

    # 递增会话代际：改密/改用户名后其他浏览器与旧会话立即失效
    config["session_epoch"] = _session_epoch(config) + 1
    save_webui_config(config)

    session["epoch"] = config["session_epoch"]
    session["must_change_password"] = _is_default_credentials(config)
    session["csrf_token"] = secrets.token_urlsafe(32)

    # 初始密码文件已完成使命，删除，避免它长期作为明文凭据留在磁盘上
    if password_changed:
        with contextlib.suppress(OSError):
            os.remove(INITIAL_PASSWORD_PATH)

    safe_user = sanitize_log_text(session.get("username", ""), 64)
    app.logger.info(f"WebUI 凭据已修改: {safe_user}")
    if password_changed and new_username is not None:
        message = "用户名与密码已修改"
    elif password_changed:
        message = "密码修改成功"
    else:
        message = "用户名已修改"
    return jsonify({"message": message, "csrf_token": session["csrf_token"]})


def _public_webui_config(config: dict) -> dict:
    """返回给浏览器的 webui 配置：不含密码哈希、session secret、PAT。"""
    out = {key: config.get(key) for key in WEBUI_PUBLIC_KEYS}
    out["github_pat_configured"] = bool(config.get("github_pat"))
    return out


def _validate_https_url_or_empty(value, field: str) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是字符串")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{field} 必须是 https URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{field} 不允许携带用户名或密码")
    return value.rstrip("/")


def _normalize_download_hosts(raw) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        parts = [item for item in re.split(r"[,，\s]+", raw) if item]
    elif isinstance(raw, list):
        parts = [str(item).strip() for item in raw if str(item).strip()]
    else:
        raise ValueError("plugin_download_hosts 必须是数组或字符串")

    result: list[str] = []
    for item in parts:
        host = item.strip().lower().rstrip(".")
        if not host or "/" in host or "://" in host or ":" in host:
            raise ValueError(f"非法下载域名: {item}")
        if not re.fullmatch(r"[a-z0-9.-]+", host):
            raise ValueError(f"非法下载域名: {item}")
        result.append(host)
    return sorted(set(result))


def _apply_webui_settings(data: dict) -> tuple[dict, str | None]:
    """应用设置白名单并原子落盘，返回 (变更字段, 错误)。"""
    cfg = load_webui_config()
    changed: dict = {}

    if "use_pypi_mirror" in data:
        cfg["use_pypi_mirror"] = bool(data["use_pypi_mirror"])
        changed["use_pypi_mirror"] = cfg["use_pypi_mirror"]

    if "pypi_mirror" in data:
        cfg["pypi_mirror"] = _validate_https_url_or_empty(data["pypi_mirror"], "pypi_mirror")
        changed["pypi_mirror"] = cfg["pypi_mirror"]

    if "github_mirror" in data:
        cfg["github_mirror"] = _validate_https_url_or_empty(
            data["github_mirror"], "github_mirror"
        )
        changed["github_mirror"] = cfg["github_mirror"]

    if "plugins_index_repo" in data:
        repo = str(data["plugins_index_repo"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            return changed, "plugins_index_repo 必须是 owner/repo 格式"
        cfg["plugins_index_repo"] = repo
        changed["plugins_index_repo"] = repo

    if "market_source_order" in data:
        order = data["market_source_order"]
        if not isinstance(order, list) or any(source not in {"unisphere", "github"} for source in order):
            return changed, "market_source_order 只能是 unisphere / github"
        cfg["market_source_order"] = list(dict.fromkeys(order))
        changed["market_source_order"] = cfg["market_source_order"]

    if "plugin_download_hosts" in data:
        try:
            hosts = _normalize_download_hosts(data["plugin_download_hosts"])
        except ValueError as e:
            return changed, str(e)
        cfg["plugin_download_hosts"] = hosts
        changed["plugin_download_hosts"] = hosts

    # 安全设置
    if "session_ttl_minutes" in data:
        try:
            cfg["session_ttl_minutes"] = max(0, int(data["session_ttl_minutes"]))
        except (TypeError, ValueError):
            return changed, "session_ttl_minutes 必须是整数"
        changed["session_ttl_minutes"] = cfg["session_ttl_minutes"]

    if "allowed_ips" in data:
        try:
            cfg["allowed_ips"] = _normalize_allowed_ips(data["allowed_ips"])
        except ValueError as e:
            return changed, str(e)
        changed["allowed_ips"] = cfg["allowed_ips"]

    if "login_fail_max" in data:
        try:
            cfg["login_fail_max"] = max(1, int(data["login_fail_max"]))
        except (TypeError, ValueError):
            return changed, "login_fail_max 必须是整数"
        changed["login_fail_max"] = cfg["login_fail_max"]

    if "login_lock_minutes" in data:
        try:
            cfg["login_lock_minutes"] = max(1, int(data["login_lock_minutes"]))
        except (TypeError, ValueError):
            return changed, "login_lock_minutes 必须是整数"
        changed["login_lock_minutes"] = cfg["login_lock_minutes"]

    # PAT：留空 / 占位符表示保持原值；clear_github_pat 显式清空
    if data.get("clear_github_pat"):
        cfg["github_pat"] = ""
        changed["github_pat_configured"] = False
    elif "github_pat" in data:
        pat = data["github_pat"]
        if not isinstance(pat, str):
            return changed, "github_pat 必须是字符串"
        if pat and pat != SECRET_PLACEHOLDER:
            cfg["github_pat"] = pat
            changed["github_pat_configured"] = True

    if changed:
        save_webui_config(cfg)
    return changed, None


def _redact_config(config: dict) -> dict:
    out = dict(config)
    for key in ("token", "milky_token"):
        if out.get(key):
            out[key] = SECRET_PLACEHOLDER
    return out


def _merge_config_secrets(incoming: dict, existing: dict) -> dict:
    """合并主配置：incoming 覆盖 existing，未出现的键保留原值。

    不能写成 `out = dict(incoming)`：那是整文件替换，
    任何一次只带部分字段的写入（脚本调用、老版本前端、字段被漏传）
    都会把 protocol / milky_server / ws_server 这些没传的键**直接抹掉**，
    机器人下次启动就连不上协议端了。
    """
    out = dict(existing)
    out.update(incoming)
    for key in ("token", "milky_token"):
        if out.get(key) == SECRET_PLACEHOLDER:
            out[key] = existing.get(key, "")
    return out


def _redact_appsettings(config: dict) -> dict:
    out = json.loads(json.dumps(config))
    implementations = out.get("Implementations")
    if isinstance(implementations, list):
        for item in implementations:
            if isinstance(item, dict) and item.get("AccessToken"):
                item["AccessToken"] = SECRET_PLACEHOLDER
    return out


def _merge_appsettings_secrets(incoming: dict, existing: dict) -> dict:
    """合并协议端配置：未出现的键/字段保留原值。

    同 _merge_config_secrets：这里原本是整文件替换，
    一次只带部分字段的写入就会把 Host / Port / Account 之类抹掉。
    """
    out = dict(existing)
    out.update(json.loads(json.dumps(incoming)))

    new_impls = out.get("Implementations")
    old_impls = existing.get("Implementations")
    if isinstance(new_impls, list) and isinstance(old_impls, list):
        merged = []
        for index, item in enumerate(new_impls):
            if not isinstance(item, dict):
                continue
            old = old_impls[index] if index < len(old_impls) else {}
            if not isinstance(old, dict):
                old = {}
            # 逐字段合并：只传了 AccessToken 时不该把 Host / Port 冲掉
            combined = dict(old)
            combined.update(item)
            if combined.get("AccessToken") == SECRET_PLACEHOLDER:
                combined["AccessToken"] = old.get("AccessToken", "")
            merged.append(combined)
        out["Implementations"] = merged
    return out


def _validate_main_config(data: dict) -> str | None:
    trigger = data.get("trigger_keyword")
    if trigger is not None:
        if not isinstance(trigger, str) or not trigger or len(trigger) > 3:
            return "trigger_keyword 必须是 1-3 个字符"
        if any(ch.isspace() for ch in trigger):
            return "trigger_keyword 不能包含空白"

    protocol = data.get("protocol")
    if protocol is not None and protocol not in {"milky", "onebot", "onebot_v11"}:
        return "protocol 只能是 milky 或 onebot"

    root_user = data.get("root_user")
    if root_user is not None and (isinstance(root_user, bool) or not isinstance(root_user, int)):
        return "root_user 必须是整数 QQ 号"

    auto_reload = data.get("auto_reload_plugins")
    if auto_reload is not None and not isinstance(auto_reload, bool):
        return "auto_reload_plugins 必须是布尔值"

    log_level = data.get("log_level")
    if log_level is not None and log_level not in {
        "DEBUG",
        "INFO",
        "SUCCESS",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        return "log_level 取值不合法"

    save_levels = data.get("log_save_levels")
    if save_levels is not None:
        allowed = {"DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}
        if not isinstance(save_levels, list) or any(level not in allowed for level in save_levels):
            return "log_save_levels 取值不合法"

    return None


def _normalize_admin_config(data: dict) -> tuple[dict | None, str | None]:
    result = {}
    for key in ("super_users", "manager_users"):
        values = data.get(key, [])
        if not isinstance(values, list):
            return None, f"{key} 必须是数组"
        normalized = []
        for uid in values:
            if isinstance(uid, bool) or not isinstance(uid, int) or uid <= 0:
                return None, f"{key} 只能包含正整数 QQ 号"
            if uid not in normalized:
                normalized.append(uid)
        result[key] = normalized
    return result, None


@app.route("/api/config/<config_type>", methods=["GET"])
def get_config(config_type):
    try:
        if config_type == "appsettings":
            with open(APPSETTINGS_PATH, encoding="utf-8") as f:
                config = _redact_appsettings(json.load(f))
        elif config_type == "config":
            with open(CONFIG_JSON_PATH, encoding="utf-8") as f:
                config = _redact_config(json.load(f))
        elif config_type == "admin":
            with open(ADMIN_JSON_PATH, encoding="utf-8") as f:
                config = json.load(f)
        elif config_type == "webui":
            config = _public_webui_config(load_webui_config())
        else:
            return jsonify({"error": "Invalid config type"}), 400

        return jsonify(config)
    except FileNotFoundError:
        app.logger.error(f"Config file not found for {config_type}.")
        return jsonify({"error": f"Config file not found for {config_type}"}), 404
    except json.JSONDecodeError:
        app.logger.error(f"Invalid JSON in config file for {config_type}.")
        return jsonify({"error": f"Invalid JSON in config file for {config_type}"}), 500
    except Exception as e:
        app.logger.error(f"Error loading config {config_type}: {e!s}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/config/<config_type>", methods=["POST"])
def update_config(config_type):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    try:
        if config_type == "appsettings":
            existing = {}
            if os.path.exists(APPSETTINGS_PATH):
                with open(APPSETTINGS_PATH, encoding="utf-8") as f:
                    existing = json.load(f)
            merged = _merge_appsettings_secrets(data, existing)
            atomic_write_json(APPSETTINGS_PATH, merged, mode=0o600)

        elif config_type == "config":
            existing = {}
            if os.path.exists(CONFIG_JSON_PATH):
                with open(CONFIG_JSON_PATH, encoding="utf-8") as f:
                    existing = json.load(f)
            merged = _merge_config_secrets(data, existing)
            error = _validate_main_config(merged)
            if error:
                return jsonify({"error": error}), 400
            atomic_write_json(CONFIG_JSON_PATH, merged, mode=0o600)

        elif config_type == "admin":
            normalized, error = _normalize_admin_config(data)
            if error:
                return jsonify({"error": error}), 400
            atomic_write_json(ADMIN_JSON_PATH, normalized, mode=0o600)

        elif config_type == "webui":
            changed, error = _apply_webui_settings(data)
            if error:
                return jsonify({"error": error}), 400
            return jsonify(
                {
                    "message": "配置已保存" if changed else "没有可更新的字段",
                    "settings": _public_webui_config(load_webui_config()),
                }
            )

        else:
            return jsonify({"error": "Invalid config type"}), 400

        app.logger.info(f"Config {config_type} updated successfully")
        return jsonify({"message": "Config updated successfully"})
    except Exception as e:
        app.logger.error(f"Error updating config {config_type}: {e!s}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/webui/settings", methods=["GET"])
def get_webui_settings():
    return jsonify(_public_webui_config(load_webui_config()))


@app.route("/api/webui/settings", methods=["POST"])
def update_webui_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400
    changed, error = _apply_webui_settings(data)
    if error:
        return jsonify({"error": error}), 400
    return jsonify(
        {
            "message": "设置已保存" if changed else "没有可更新的字段",
            "settings": _public_webui_config(load_webui_config()),
        }
    )


def get_plugins_list():
    """扫描 plugins/ 目录。

    判定规则与 core/plugin_manager.py 保持一致：
      · 目录插件必须含 setup.py，否则不算插件（以前任意目录都会被列出来）
      · 只有 .py 结尾的文件才算单文件插件（以前 xxx.md 会被当成目录插件列出）
    带 plugin.json 的插件会附上清单信息，供前端渲染配置表单。
    """
    plugins = []
    plugins_dir = app.config["PLUGINS_DIR"]

    if not os.path.exists(plugins_dir):
        return []

    for item in sorted(os.listdir(plugins_dir)):
        if item == "__pycache__" or item.startswith("_"):
            continue

        item_path = os.path.join(plugins_dir, item)
        is_disabled = item.startswith("d_")
        base_name = item[2:] if is_disabled else item

        if os.path.isdir(item_path):
            # 目录里没有 setup.py 就不是插件（可能只是资源目录）
            if not os.path.exists(os.path.join(item_path, "setup.py")):
                continue
            plugin_type = "directory"
        elif base_name.endswith(".py"):
            plugin_type = "file"
            base_name = base_name[:-3]
        else:
            continue  # .md / .txt 等一律不是插件

        plugin_info = {
            "name": base_name,
            "full_name": item,
            "enabled": not is_disabled,
            "type": plugin_type,
        }

        if plugin_type == "directory":
            readme_path = os.path.join(item_path, "README.md")
        else:
            readme_path = os.path.join(plugins_dir, f"{base_name}.md")
        plugin_info["has_help"] = bool(readme_path and os.path.exists(readme_path))

        # 清单（可选）：有 config 声明才在前端显示「配置」按钮
        manifest = pc.load_manifest(plugins_dir, base_name)
        plugin_info["display_name"] = manifest.get("display_name") or base_name
        plugin_info["version"] = manifest.get("version") or ""
        plugin_info["description"] = manifest.get("description") or ""
        plugin_info["has_config"] = bool(pc.get_schema(manifest))

        plugins.append(plugin_info)
    return plugins


@app.route("/api/plugins/<plugin_name>/manifest", methods=["GET"])
def get_plugin_manifest(plugin_name):
    """插件清单（含配置项 schema），前端据此动态渲染表单"""
    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400
    plugins_dir = app.config["PLUGINS_DIR"]
    manifest = pc.load_manifest(plugins_dir, plugin_name)
    if not manifest:
        return jsonify({"error": "该插件没有 plugin.json 清单"}), 404
    return jsonify(
        {
            "name": manifest.get("name") or plugin_name,
            "display_name": manifest.get("display_name") or plugin_name,
            "version": manifest.get("version") or "",
            "author": manifest.get("author") or "",
            "description": manifest.get("description") or "",
            "config": pc.get_schema(manifest),
        }
    )


@app.route("/api/plugins/<plugin_name>/config", methods=["GET"])
def get_plugin_config(plugin_name):
    """插件当前配置值（schema 的 default 已合并进来），拍平成 {点号key: 值}"""
    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400
    plugins_dir = app.config["PLUGINS_DIR"]
    if not pc.plugin_dir(plugins_dir, plugin_name):
        return jsonify({"error": "插件不存在或不是目录形态"}), 404
    schema = pc.get_schema(pc.load_manifest(plugins_dir, plugin_name))
    merged = pc.load_config(plugins_dir, plugin_name)
    values = pc.flatten_for_form(schema, merged)
    # 密钥类配置不回传明文，前端拿到占位符；保存时原样回传即保留旧值
    for item in schema:
        if item.get("type") == "secret" and values.get(item["key"]):
            values[item["key"]] = SECRET_PLACEHOLDER
    return jsonify({"values": values})


@app.route("/api/plugins/<plugin_name>/config", methods=["POST"])
def update_plugin_config(plugin_name):
    """保存插件配置。

    只接受 schema 里声明过的 key，并按声明的类型校验——
    否则这个接口等于允许往插件目录里写任意 JSON。
    """
    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400
    plugins_dir = app.config["PLUGINS_DIR"]
    if not pc.plugin_dir(plugins_dir, plugin_name):
        return jsonify({"error": "插件不存在或不是目录形态"}), 404

    schema = pc.get_schema(pc.load_manifest(plugins_dir, plugin_name))
    if not schema:
        return jsonify({"error": "该插件没有声明可配置项"}), 400

    incoming = request.get_json(silent=True)
    if not isinstance(incoming, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    # 占位符表示“保持原密钥”，不要把它写进 config.json
    for item in schema:
        key = item["key"]
        if item.get("type") == "secret" and incoming.get(key) == SECRET_PLACEHOLDER:
            incoming.pop(key, None)

    existing = pc.load_raw_config(plugins_dir, plugin_name)
    ok, built, errors = pc.validate_and_build(schema, incoming, existing)
    if not ok:
        return jsonify({"error": "；".join(errors)}), 400
    try:
        pc.save_config(plugins_dir, plugin_name, built)
    except Exception as e:
        return jsonify({"error": f"保存失败: {e}"}), 500
    # bot 端按 config.json 的 mtime 缓存，写完下一条消息就会读到新值
    return jsonify({"message": "配置已保存，立即生效"})


@app.route("/api/plugins", methods=["GET"])
def get_plugins():
    try:
        plugins = get_plugins_list()
        return jsonify(plugins)
    except Exception as e:
        app.logger.error(f"Error getting plugins: {e!s}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plugins/<plugin_name>", methods=["GET"])
def get_plugin_details(plugin_name):
    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400
    try:
        plugins_dir = app.config["PLUGINS_DIR"]

        target_path = None
        if os.path.exists(os.path.join(plugins_dir, plugin_name)):
            target_path = os.path.join(plugins_dir, plugin_name)
        elif os.path.exists(os.path.join(plugins_dir, f"{plugin_name}.py")):
            target_path = os.path.join(plugins_dir, f"{plugin_name}.py")
        elif os.path.exists(os.path.join(plugins_dir, f"d_{plugin_name}")):
            target_path = os.path.join(plugins_dir, f"d_{plugin_name}")
        elif os.path.exists(os.path.join(plugins_dir, f"d_{plugin_name}.py")):
            target_path = os.path.join(plugins_dir, f"d_{plugin_name}.py")

        if not target_path:
            return jsonify({"error": "Plugin not found"}), 404

        details = {
            "name": plugin_name,
            "type": "directory" if os.path.isdir(target_path) else "file",
        }

        readme_content = "No help available"
        if details["type"] == "directory":
            readme_path = os.path.join(target_path, "README.md")
            if os.path.exists(readme_path):
                with open(readme_path, encoding="utf-8") as f:
                    readme_content = f.read()
        elif details["type"] == "file":
            readme_path = os.path.join(plugins_dir, f"{plugin_name}.md")
            if os.path.exists(readme_path):
                with open(readme_path, encoding="utf-8") as f:
                    readme_content = f.read()

        details["help"] = readme_content

        return jsonify(details)
    except Exception as e:
        app.logger.error(f"Error getting plugin details for {plugin_name}: {e!s}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plugins/<plugin_name>", methods=["PUT"])
def toggle_plugin(plugin_name):
    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400
    try:
        plugins_dir = app.config["PLUGINS_DIR"]

        current_path = None
        if os.path.exists(os.path.join(plugins_dir, plugin_name)):
            current_path = os.path.join(plugins_dir, plugin_name)
        elif os.path.exists(os.path.join(plugins_dir, f"{plugin_name}.py")):
            current_path = os.path.join(plugins_dir, f"{plugin_name}.py")
        elif os.path.exists(os.path.join(plugins_dir, f"d_{plugin_name}")):
            current_path = os.path.join(plugins_dir, f"d_{plugin_name}")
        elif os.path.exists(os.path.join(plugins_dir, f"d_{plugin_name}.py")):
            current_path = os.path.join(plugins_dir, f"d_{plugin_name}.py")

        if not current_path:
            return jsonify({"error": "Plugin not found"}), 404

        is_currently_enabled = not os.path.basename(current_path).startswith("d_")

        if is_currently_enabled:
            new_name = f"d_{os.path.basename(current_path)}"
            new_path = os.path.join(plugins_dir, new_name)
            os.rename(current_path, new_path)
            app.logger.info(f"Plugin {plugin_name} disabled")
            return jsonify({"message": "Plugin disabled successfully"})
        else:
            # 只去掉开头的 d_ 前缀。原来用 replace() 会把名字中间的 d_ 也替换掉，
            # 比如 d_word_count 会变成 wordcount，插件从此再也找不到
            original_name = os.path.basename(current_path)[2:]
            original_path = os.path.join(plugins_dir, original_name)
            os.rename(current_path, original_path)
            app.logger.info(f"Plugin {plugin_name} enabled")
            return jsonify({"message": "Plugin enabled successfully"})
    except Exception as e:
        app.logger.error(f"Error toggling plugin {plugin_name}: {e!s}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/plugins/<plugin_name>", methods=["DELETE"])
def uninstall_plugin(plugin_name):
    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400
    try:
        plugins_dir = app.config["PLUGINS_DIR"]

        possible_paths = [
            os.path.join(plugins_dir, plugin_name),
            os.path.join(plugins_dir, f"{plugin_name}.py"),
            os.path.join(plugins_dir, f"d_{plugin_name}"),
            os.path.join(plugins_dir, f"d_{plugin_name}.py"),
        ]

        found_path = None
        for p in possible_paths:
            if os.path.exists(p):
                found_path = p
                break

        if not found_path:
            return jsonify({"error": "Plugin not found"}), 404

        if found_path.endswith(".py") or found_path.endswith(".md"):
            md_path = os.path.join(plugins_dir, f"{plugin_name}.md")
            if os.path.exists(md_path):
                os.remove(md_path)
                app.logger.info(f"Removed associated markdown file: {md_path}")

        if os.path.isdir(found_path):
            shutil.rmtree(found_path)
        else:
            os.remove(found_path)

        app.logger.info(f"Plugin {plugin_name} uninstalled")
        return jsonify({"message": "Plugin uninstalled successfully"})
    except Exception as e:
        app.logger.error(f"Error uninstalling plugin {plugin_name}: {e!s}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/available_plugins", methods=["GET"])
def get_available_plugins():
    try:
        from core.market_client import PluginMarketClient

        webui_config = load_webui_config()
        app.logger.info("Initializing multi-source plugin market client")
        market_client = PluginMarketClient(webui_config)

        installed_plugins_raw = get_plugins_list()
        installed_plugin_names = {p["name"] for p in installed_plugins_raw}

        app.logger.info(f"Checking installed plugins: {len(installed_plugin_names)}")

        available_plugins = market_client.get_available_plugins(installed_plugin_names)

        app.logger.info(f"Found {len(available_plugins)} available plugins from all sources")
        return jsonify(available_plugins)
    except Exception as e:
        app.logger.error(f"Error getting available plugins: {e!s}")
        return jsonify({"error": str(e)}), 500


# 注意：不能再用 /api/plugins/<plugin_id>，否则会和上面的
# /api/plugins/<plugin_name>（帮助文档）冲突，实际永远命中不到。
@app.route("/api/market/plugins/<plugin_id>", methods=["GET"])
def get_plugin_detail(plugin_id: str):
    """获取单个市场插件的详细信息（支持多源查找）。"""
    if not _safe_plugin_name(plugin_id):
        return jsonify({"error": "非法插件 ID"}), 400
    try:
        from core.market_client import PluginMarketClient

        webui_config = load_webui_config()
        app.logger.info(f"Getting plugin details: {plugin_id}")
        market_client = PluginMarketClient(webui_config)

        plugin_url = market_client.get_plugin_download_url(plugin_id)
        checksum = market_client.resolve_checksum(plugin_id)

        plugin_info = {
            "id": plugin_id,
            "name": plugin_id,
            "version": "0.1.0",
            "description": "No description available",
            "downloadUrl": plugin_url,
            "sha256": checksum or "",
        }

        app.logger.info(f"Got plugin info: {plugin_info}")
        return jsonify(plugin_info)
    except Exception as e:
        app.logger.error(f"Error getting plugin detail {plugin_id}: {e!s}")
        return jsonify({"error": str(e)}), 500


def _process_plugin_structure(plugin_name, extracted_plugin_root_path, log_callback):
    contents_at_root_path = os.listdir(extracted_plugin_root_path)
    if (
        len(contents_at_root_path) == 1
        and os.path.isdir(os.path.join(extracted_plugin_root_path, contents_at_root_path[0]))
        and contents_at_root_path[0] == plugin_name
    ):
        nested_dir = os.path.join(extracted_plugin_root_path, plugin_name)
        log_callback(f"检测到嵌套目录 '{nested_dir}'，正在解包...")

        for item in os.listdir(nested_dir):
            shutil.move(os.path.join(nested_dir, item), extracted_plugin_root_path)
        shutil.rmtree(nested_dir)
        log_callback(f"嵌套目录 '{nested_dir}' 解包完成。")

    # 统一成"每个插件一个文件夹、入口是 setup.py"。
    #
    # 旧实现在这里做的是反向操作：如果目录里只有一个 <name>.py，就把它拽到
    # plugins/ 根目录、README.md 改名成 <name>.md、再把文件夹删掉。那会让插件
    # 失去自己的 plugin.json / config.json，与现在的私有配置机制直接冲突，
    # 所以改成保留文件夹形态，并把老式入口补齐为 setup.py。
    setup_path = os.path.join(extracted_plugin_root_path, "setup.py")
    legacy_entry = os.path.join(extracted_plugin_root_path, f"{plugin_name}.py")

    if not os.path.exists(setup_path):
        if os.path.exists(legacy_entry):
            shutil.move(legacy_entry, setup_path)
            log_callback(
                f"已将旧式入口 '{plugin_name}.py' 重命名为 'setup.py'（目录插件的入口约定）。"
            )
        else:
            # 没有 setup.py 也没有同名 .py，退而求其次找唯一的顶层 .py 当入口
            py_files = [
                f
                for f in os.listdir(extracted_plugin_root_path)
                if f.endswith(".py") and f != "__init__.py"
            ]
            if len(py_files) == 1:
                shutil.move(os.path.join(extracted_plugin_root_path, py_files[0]), setup_path)
                log_callback(f"已将唯一入口 '{py_files[0]}' 重命名为 'setup.py'。")
            else:
                log_callback(
                    "⚠️ 未找到 setup.py，插件可能无法被加载（目录插件入口必须叫 setup.py）。"
                )


_PLUGIN_INSTALL_LOCK = threading.Lock()


def _allowed_download_hosts() -> set[str]:
    hosts = set(DEFAULT_DOWNLOAD_HOSTS)
    cfg = load_webui_config()
    for key in ("github_mirror", "pypi_mirror"):
        host = host_from_url(cfg.get(key))
        if host:
            hosts.add(host)
    extra = cfg.get("plugin_download_hosts")
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str) and item.strip():
                hosts.add(item.strip().lower().rstrip("."))
    return hosts


def _download_plugin_archive(url: str, dest: str, allowed_hosts: set[str]):
    """下载插件 ZIP（生成器，边下边报进度），手动校验每一跳重定向。"""
    current = url
    response = None
    for _ in range(4):
        response = requests.get(
            current,
            stream=True,
            timeout=(5, 30),
            allow_redirects=False,
        )
        if response.is_redirect:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise SecurityError("下载地址重定向缺少 Location")
            current = urljoin(current, location)
            validate_download_url(current, allowed_hosts)
            continue
        break
    else:
        raise SecurityError("插件下载重定向次数过多")

    try:
        response.raise_for_status()
        total_size = int(response.headers.get("content-length") or 0)
        if total_size > MAX_PLUGIN_ARCHIVE_BYTES:
            raise SecurityError("插件压缩包超过 50MB 限制")

        downloaded = 0
        with open(dest, "wb") as f:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > MAX_PLUGIN_ARCHIVE_BYTES:
                    raise SecurityError("插件压缩包超过 50MB 限制")
                f.write(chunk)
                if total_size > 0:
                    yield f"下载中: {downloaded}/{total_size} ({downloaded / total_size * 100:.1f}%)"
                else:
                    yield f"下载中: {downloaded} bytes"
        yield f"下载完成: {downloaded} bytes"
    finally:
        response.close()


def _run_pip_requirements(requirements_path: str, mirror: str) -> tuple[bool, list[str]]:
    """安装插件依赖：合并 stderr，设置总超时与工作目录，避免管道死锁。"""
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-input",
        "--disable-pip-version-check",
        "-r",
        requirements_path,
    ]
    if mirror:
        command += ["-i", mirror]

    env = os.environ.copy()
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    try:
        result = subprocess.run(
            command,
            cwd=str(Path(requirements_path).parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, ["pip 安装超时（300 秒）"]
    lines = (result.stdout or "").splitlines()
    return result.returncode == 0, lines[-500:]


def _select_repo_root(extract_root: Path, prefix: str) -> Path:
    entries = [entry for entry in extract_root.iterdir() if entry.is_dir()]
    preferred = [entry for entry in entries if entry.name.startswith(f"{prefix}-")]
    if len(preferred) == 1:
        return resolve_within(extract_root, preferred[0].name)
    if len(entries) == 1:
        return resolve_within(extract_root, entries[0].name)
    raise SecurityError("无法确定解压后的仓库根目录")


@app.route("/api/plugins", methods=["POST"])
def install_plugin():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求体必须是 JSON 对象"}), 400

    plugin_url = data.get("url")
    plugin_name = data.get("name")
    plugin_path_in_repo = data.get("path")
    use_pypi_mirror = data.get("use_pypi_mirror", False)
    pypi_mirror_raw = data.get("pypi_mirror", "")
    expected_sha256 = str(data.get("sha256") or "").strip().lower()

    if (
        not isinstance(plugin_url, str)
        or not isinstance(plugin_name, str)
        or not isinstance(plugin_path_in_repo, str)
        or not plugin_url
        or not plugin_path_in_repo
    ):
        return jsonify({"error": "缺少插件名称、下载地址或安装路径"}), 400

    if not _safe_plugin_name(plugin_name):
        return jsonify({"error": "非法插件名"}), 400

    relative_parts = [
        part for part in plugin_path_in_repo.replace("\\", "/").split("/") if part not in ("", ".")
    ]
    if not relative_parts or any(part == ".." for part in relative_parts):
        return jsonify({"error": "安装路径非法"}), 400

    if expected_sha256 and not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return jsonify({"error": "sha256 必须是 64 位十六进制字符串"}), 400

    try:
        validate_download_url(plugin_url, _allowed_download_hosts())
    except SecurityError as e:
        return jsonify({"error": str(e)}), 400

    try:
        final_plugin_target_dir = plugin_install_path(
            app.config["PLUGINS_DIR"], plugin_name
        )
    except SecurityError as e:
        return jsonify({"error": str(e)}), 400

    raw_target = os.path.join(app.config["PLUGINS_DIR"], plugin_name)
    if os.path.islink(raw_target) or os.path.islink(final_plugin_target_dir):
        return jsonify({"error": "插件目录不能是符号链接"}), 400

    mirror = ""
    if use_pypi_mirror and pypi_mirror_raw:
        try:
            mirror = _validate_https_url_or_empty(pypi_mirror_raw, "pypi_mirror")
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

    repo_prefix = re.sub(
        r"[^A-Za-z0-9_.-]", "", str(data.get("plugins_index_repo_name_only") or "Unisphere")
    )
    repo_prefix = repo_prefix or "Unisphere"

    def generate_install_logs():
        if not _PLUGIN_INSTALL_LOCK.acquire(blocking=False):
            yield "data: Error: 已有插件安装任务正在运行，请稍后再试\n\n"
            yield "data: INSTALL_FAILED\n\n"
            return

        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(
                prefix="lunarx-plugin-", dir=app.config["UPLOAD_FOLDER"]
            )
            zip_path = os.path.join(temp_dir, "plugin.zip")

            yield f"data: 开始下载插件: {plugin_name}\n\n"
            yield from (
                f"data: {line}\n\n"
                for line in _download_plugin_archive(
                    plugin_url, zip_path, _allowed_download_hosts()
                )
            )

            if expected_sha256:
                actual = sha256_file(zip_path)
                if not hmac.compare_digest(actual, expected_sha256):
                    raise SecurityError("插件 SHA256 校验失败，文件可能被篡改")
                yield "data: SHA256 校验通过\n\n"

            extract_root = Path(temp_dir) / "extract"
            extract_root.mkdir()
            file_count, total_size = safe_extract_zip(zip_path, extract_root)
            yield f"data: 解压完成（{file_count} 个文件，{total_size} 字节）\n\n"

            repo_root = _select_repo_root(extract_root, repo_prefix)
            source_dir = resolve_within(repo_root, *relative_parts)
            if not source_dir.is_dir():
                raise SecurityError("压缩包中找不到插件目录")

            if final_plugin_target_dir.exists():
                shutil.rmtree(final_plugin_target_dir)
            final_plugin_target_dir.mkdir(parents=True, exist_ok=True)

            for item in source_dir.iterdir():
                shutil.move(str(item), str(final_plugin_target_dir / item.name))

            messages: list[str] = []

            def log_callback(message: str) -> None:
                messages.append(message)

            _process_plugin_structure(
                plugin_name, str(final_plugin_target_dir), log_callback
            )
            for message in messages:
                yield f"data: {message}\n\n"

            requirements_path = final_plugin_target_dir / "requirements.txt"
            if requirements_path.exists():
                yield "data: 开始安装插件依赖（插件依赖会执行安装脚本，请确认来源可信）\n\n"
                ok, lines = _run_pip_requirements(str(requirements_path), mirror)
                for line in lines:
                    yield f"data: PIP: {sanitize_log_text(line, 500)}\n\n"
                if not ok:
                    raise RuntimeError("插件已安装，但依赖安装失败")

            yield f"data: 插件 {plugin_name} 安装成功\n\n"
            if load_webui_config().get("auto_reload_plugins") is False:
                yield "data: 提示: 自动重载已关闭，请在「插件管理」手动重载或重启 Bot\n\n"
            yield "data: INSTALL_SUCCESS\n\n"

        except SecurityError as e:
            app.logger.error(f"插件安装被安全策略拒绝: {e}")
            yield f"data: Error: {sanitize_log_text(e, 500)}\n\n"
            yield "data: INSTALL_FAILED\n\n"
        except requests.exceptions.RequestException as e:
            app.logger.error(f"下载插件失败: {e}")
            yield f"data: Error: 下载插件时发生网络错误: {sanitize_log_text(e, 300)}\n\n"
            yield "data: INSTALL_FAILED\n\n"
        except Exception as e:
            app.logger.error(f"安装插件 {plugin_name} 时发生错误: {e}")
            yield f"data: Error: 安装插件时发生错误: {sanitize_log_text(e, 300)}\n\n"
            yield "data: INSTALL_FAILED\n\n"
        finally:
            if temp_dir:
                shutil.rmtree(temp_dir, ignore_errors=True)
            _PLUGIN_INSTALL_LOCK.release()

    return Response(stream_with_context(generate_install_logs()), mimetype="text/event-stream")


if __name__ == "__main__":
    host = os.getenv("LUNARX_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("LUNARX_WEB_PORT", "5000"))
    app.run(debug=False, threaded=True, host=host, port=port)
