import os
import json
import hashlib
import secrets
import time
import requests
import subprocess
import shutil
import zipfile
import logging
from datetime import datetime, date, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, stream_with_context, Response, session, redirect, url_for
from flask.logging import default_handler
import sys
import re
import psutil

# 与 bot 端共用同一套插件配置读写逻辑，避免两边实现漂移
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import plugin_config as pc

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['PLUGINS_DIR'] = 'plugins'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['PLUGINS_DIR'], exist_ok=True)

APPSETTINGS_PATH = os.path.abspath('appsettings.json')
CONFIG_JSON_PATH = os.path.abspath('config.json')
ADMIN_JSON_PATH = os.path.abspath('admin114.json')
WEBUI_JSON_PATH = os.path.abspath('webui.json')
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'lunarx.log')


_PLUGIN_NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$')


def _safe_plugin_name(name: str) -> bool:
    """插件名白名单校验。

    插件名会被直接拼进文件路径，不校验的话 `..%2f..%2fconfig.json`
    这类输入能让删除/读写接口跑出 plugins 目录。
    只允许字母数字下划线点横线，且不能以点开头、不能含路径分隔符。
    """
    if not name or not _PLUGIN_NAME_RE.match(name):
        return False
    return '..' not in name and '/' not in name and '\\' not in name


def load_webui_config():
    try:
        with open(WEBUI_JSON_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def save_webui_config(config):
    with open(WEBUI_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def get_session_secret():
    """会话密钥：从 webui.json 读取，不存在则生成并持久化（重启不失效）"""
    config = load_webui_config()
    secret = config.get('session_secret')
    if not secret:
        secret = secrets.token_hex(32)
        config['session_secret'] = secret
        save_webui_config(config)
    return secret


app.secret_key = get_session_secret()


# ---------- 登录鉴权 ----------

def hash_password(password: str, salt: str = None) -> str:
    """PBKDF2-SHA256 密码哈希（标准库实现，无需额外依赖）"""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                 bytes.fromhex(salt), 100_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split('$')
        return hash_password(password, salt) == stored
    except Exception:
        return False


# ---------- 登录安全（会话有效期 / IP 白名单 / 失败锁定 / 登录日志） ----------

LOGIN_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs', 'webui_login.log')

# 与安全相关的 webui.json 字段白名单（只允许这些键经接口读写）
SECURITY_KEYS = ('session_ttl_minutes', 'allowed_ips', 'login_fail_max', 'login_lock_minutes')

# IP → 失败计数与锁定状态（进程内，重启清零；锁定信息同时写入登录日志）
_login_failures = {}


def _security_settings():
    """读取安全相关配置，缺省给保守默认值"""
    cfg = load_webui_config()
    return {
        'session_ttl_minutes': int(cfg.get('session_ttl_minutes') or 0),
        'allowed_ips': cfg.get('allowed_ips') or [],
        'login_fail_max': int(cfg.get('login_fail_max') or 5),
        'login_lock_minutes': int(cfg.get('login_lock_minutes') or 15),
    }


def _append_login_log(ip, username, ok, reason=''):
    """登录成功/失败都落一行日志，供「设置 → 账户与安全 → 登录日志」查看"""
    try:
        os.makedirs(os.path.dirname(LOGIN_LOG_PATH), exist_ok=True)
        stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        result = '成功' if ok else '失败'
        line = f"{stamp} | {ip} | {username or '-'} | {result} | {reason}"
        with open(LOGIN_LOG_PATH, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


@app.after_request
def add_no_cache(response):
    """页面/API 不缓存，避免浏览器加载旧版 HTML/JS"""
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.before_request
def require_login():
    """除登录页/登录接口/静态资源外，所有页面与 API 均需登录。

    登录会话有效期内允许访问；无操作超过 session_ttl_minutes（0 = 永不过期）
    自动失效。配置了 allowed_ips（非空数组）时校验来源 IP。
    """
    if request.path == '/login' or request.path == '/api/login' or request.path == '/api/auth_status':
        return None
    if request.path.startswith('/static/'):
        return None

    sec = _security_settings()
    client_ip = request.remote_addr or ''

    if sec['allowed_ips'] and client_ip not in sec['allowed_ips']:
        if request.path.startswith('/api/'):
            return jsonify({'error': '来源 IP 不在白名单内'}), 403
        return 'Forbidden', 403

    if session.get('logged_in'):
        ttl = sec['session_ttl_minutes']
        if ttl > 0:
            login_at = session.get('login_time') or 0
            if time.time() - login_at > ttl * 60:
                session.clear()
                if request.path.startswith('/api/'):
                    return jsonify({'error': '会话已过期，请重新登录'}), 401
                return redirect(url_for('login_page'))
        return None

    if request.path.startswith('/api/'):
        return jsonify({'error': '未登录'}), 401
    return redirect(url_for('login_page'))

class CustomFormatter(logging.Formatter):
    def format(self, record):
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        level = record.levelname
        if level == 'INFO':
            level = '\033[94mINFO\033[0m'
        elif level == 'WARNING':
            level = '\033[93mWARNING\033[0m'
        elif level == 'ERROR':
            level = '\033[91mERROR\033[0m'
        return f"[{timestamp}] [Lunar_WebUI] ℹ️ {level} {record.getMessage()}"

handler = logging.StreamHandler()
handler.setFormatter(CustomFormatter())
app.logger.removeHandler(default_handler)
app.logger.addHandler(handler)
app.logger.setLevel(logging.INFO)

def init_default_configs():
    if not os.path.exists(APPSETTINGS_PATH):
        default_appsettings = {
            "$schema": "https://raw.githubusercontent.com/LagrangeDev/Lagrange.Core/master/Lagrange.OneBot/Resources/appsettings_schema.json",
            "Logging": {
                "LogLevel": {
                    "Default": "Information",
                    "Microsoft": "Warning",
                    "Microsoft.Hosting.Lifetime": "Information"
                }
            },
            "SignServerUrl": "https://sign.lagrangecore.org/api/sign/39038",
            "SignProxyUrl": "",
            "MusicSignServerUrl": "",
            "Account": {
                "Uin": 0,
                "Protocol": "Linux",
                "AutoReconnect": True,
                "GetOptimumServer": True
            },
            "Message": {
                "IgnoreSelf": True,
                "StringPost": False
            },
            "QrCode": {
                "ConsoleCompatibilityMode": False
            },
            "Implementations": [
                {
                    "Type": "ForwardWebSocket",
                    "Host": "127.0.0.1",
                    "Port": 3803,
                    "HeartBeatInterval": 5000,
                    "AccessToken": "114514"
                }
            ]
        }
        with open(APPSETTINGS_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_appsettings, f, indent=2, ensure_ascii=False)
    
    if not os.path.exists(CONFIG_JSON_PATH):
        default_config = {
            "ws_server": "ws://127.0.0.1:3803",
            "token": "114514",
            "bot_qq": 123456789,
            "root_user": 1348472639,
            "log_level": "INFO",
            "trigger_keyword": "$",
            "auto_reload_plugins": True,
            "bot_name": "Lunar X",
            "bot_name_en": "Lunar",
            "answer": [114, 3803, 114514],
            "gemini_key": "",
            "openai_key": "",
            "deepseek_key": ""
        }
        with open(CONFIG_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_config, f, indent=2, ensure_ascii=False)
    
    if not os.path.exists(ADMIN_JSON_PATH):
        default_admin = {
            "super_users": [987654321],
            "manager_users": [123456789, 987654321, 2473768771]
        }
        with open(ADMIN_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_admin, f, indent=2, ensure_ascii=False)
    
    if not os.path.exists(WEBUI_JSON_PATH):
        default_webui = {
            "use_pypi_mirror": False,
            "pypi_mirror": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "github_mirror": "",
            "github_pat": "",
            "plugins_index_repo": "MacroSTAR-Org/Unisphere",
            "username": "lunarx",
            "password_hash": hash_password("lunarx"),
            "session_secret": secrets.token_hex(32),
            "session_ttl_minutes": 0,
            "allowed_ips": [],
            "login_fail_max": 5,
            "login_lock_minutes": 15
        }
        with open(WEBUI_JSON_PATH, 'w', encoding='utf-8') as f:
            json.dump(default_webui, f, indent=2, ensure_ascii=False)
    else:
        with open(WEBUI_JSON_PATH, 'r+', encoding='utf-8') as f:
            webui_config = json.load(f)
            updated = False
            if "github_pat" not in webui_config:
                webui_config["github_pat"] = ""
                updated = True
            if "plugins_index_repo" not in webui_config:
                webui_config["plugins_index_repo"] = "MacroSTAR-Org/Unisphere"
                updated = True
            if "username" not in webui_config:
                webui_config["username"] = "lunarx"
                webui_config["password_hash"] = hash_password("lunarx")
                updated = True
            if "session_secret" not in webui_config:
                webui_config["session_secret"] = secrets.token_hex(32)
                updated = True
            if "session_ttl_minutes" not in webui_config:
                webui_config["session_ttl_minutes"] = 0
                updated = True
            if "allowed_ips" not in webui_config:
                webui_config["allowed_ips"] = []
                updated = True
            if "login_fail_max" not in webui_config:
                webui_config["login_fail_max"] = 5
                updated = True
            if "login_lock_minutes" not in webui_config:
                webui_config["login_lock_minutes"] = 15
                updated = True
            if updated:
                f.seek(0)
                json.dump(webui_config, f, indent=2, ensure_ascii=False)
                f.truncate()

init_default_configs()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', endpoint='login_page')
def login_page():
    if session.get('logged_in'):
        return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/api/auth_status', methods=['GET'])
def auth_status():
    config = load_webui_config()
    return jsonify({
        'logged_in': bool(session.get('logged_in')),
        'username': session.get('username') or config.get('username', 'lunarx')
    })

@app.route('/api/check_default_credentials', methods=['GET'])
def check_default_credentials():
    config = load_webui_config()
    username = config.get('username', 'lunarx')
    password_hash = config.get('password_hash', '')
    is_default = username == 'lunarx' and verify_password('lunarx', password_hash)
    return jsonify({'is_default': is_default})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username', '')
    password = data.get('password', '')
    client_ip = request.remote_addr or ''

    sec = _security_settings()
    fail_max = max(1, sec['login_fail_max'])
    lock_min = max(1, sec['login_lock_minutes'])

    # 连续失败锁定：IP 维度，超过阈值后临时拒绝登录
    now = time.time()
    state = _login_failures.get(client_ip, {'count': 0, 'until': 0})
    if state['until'] > now:
        remain = int(state['until'] - now) // 60 + 1
        _append_login_log(client_ip, username, False, f'IP 被锁定，剩余 {remain} 分钟')
        return jsonify({'error': f'失败次数过多，请 {remain} 分钟后再试'}), 403

    config = load_webui_config()
    stored_username = config.get('username', 'lunarx')
    stored_hash = config.get('password_hash', '')

    if username == stored_username and verify_password(password, stored_hash):
        _login_failures.pop(client_ip, None)
        session['logged_in'] = True
        session['username'] = username
        session['login_time'] = now
        app.logger.info(f"WebUI 登录成功: {username}")
        _append_login_log(client_ip, username, True)
        return jsonify({'message': '登录成功', 'username': username})

    # 失败：计数并记录日志，达到阈值则锁定该 IP
    state['count'] += 1
    if state['count'] >= fail_max:
        state['until'] = now + lock_min * 60
        state['count'] = 0
        reason = f'已达 {fail_max} 次失败，锁定 {lock_min} 分钟'
    else:
        reason = f'剩余 {fail_max - state["count"]} 次机会'
    _login_failures[client_ip] = state
    app.logger.warning(f"WebUI 登录失败: {username} ({reason})")
    _append_login_log(client_ip, username, False, reason)
    return jsonify({'error': '用户名或密码错误'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': '已退出登录'})

@app.route('/api/login_logs', methods=['GET'])
def login_logs():
    """读取登录日志（最近的，倒序返回），不存在时返回空列表"""
    limit = max(1, min(500, request.args.get('limit', default=100, type=int)))
    lines = []
    try:
        with open(LOGIN_LOG_PATH, 'r', encoding='utf-8') as f:
            lines = [ln.rstrip('\n') for ln in f if ln.strip()]
    except OSError:
        pass
    return jsonify(lines[-limit:][::-1])

@app.route('/api/webui_security', methods=['GET'])
def get_webui_security():
    """返回当前安全设置（会话有效期 / IP 白名单 / 失败锁定阈值），不含敏感凭据"""
    cfg = load_webui_config()
    out = {k: cfg.get(k) for k in SECURITY_KEYS}
    sec = _security_settings()
    out['login_fail_max'] = sec['login_fail_max']
    out['login_lock_minutes'] = sec['login_lock_minutes']
    return jsonify(out)

@app.route('/api/webui_security', methods=['POST'])
def set_webui_security():
    """更新安全设置，只接受白名单内的键，防止误覆盖凭据"""
    data = request.get_json() or {}
    cfg = load_webui_config()
    changed = {}

    if 'session_ttl_minutes' in data:
        try:
            cfg['session_ttl_minutes'] = max(0, int(data['session_ttl_minutes']))
            changed['session_ttl_minutes'] = cfg['session_ttl_minutes']
        except (TypeError, ValueError):
            pass

    if 'allowed_ips' in data:
        raw = data['allowed_ips']
        ips = [x.strip() for x in raw.split(',')] if isinstance(raw, str) else []
        ips = [x for x in ips if x]
        cfg['allowed_ips'] = ips
        changed['allowed_ips'] = ips

    if 'login_fail_max' in data:
        try:
            cfg['login_fail_max'] = max(1, int(data['login_fail_max']))
            changed['login_fail_max'] = cfg['login_fail_max']
        except (TypeError, ValueError):
            pass

    if 'login_lock_minutes' in data:
        try:
            cfg['login_lock_minutes'] = max(1, int(data['login_lock_minutes']))
            changed['login_lock_minutes'] = cfg['login_lock_minutes']
        except (TypeError, ValueError):
            pass

    if changed:
        save_webui_config(cfg)
        app.logger.info(f"WebUI 安全设置已更新: {sorted(changed)}")
    return jsonify(changed)

@app.route('/api/webui_security/force_logout', methods=['POST'])
def force_logout_all():
    """强制所有会话下线：轮换 session_secret，旧 cookie 全部失效"""
    new_secret = secrets.token_hex(32)
    cfg = load_webui_config()
    cfg['session_secret'] = new_secret
    save_webui_config(cfg)
    app.secret_key = new_secret
    session.clear()
    app.logger.info("WebUI 已强制所有会话下线（session_secret 已轮换）")
    return jsonify({'message': '已强制所有会话下线，请重新登录'})


def _dir_size(path):
    """递归计算目录大小（字节）"""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _find_bot_process():
    """查找 Lunar X 主进程（python main.py），跨平台。

    Windows venv 的 python.exe 是启动器（会再拉起真解释器），
    优先返回非 venv 路径的真身；Linux 的 venv/bin/python 即解释器本身。
    """
    candidates = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                cmdline = proc.info.get('cmdline') or []
                if any('main.py' in c for c in cmdline):
                    candidates.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    if not candidates:
        return None
    # 排除 venv 启动器（Windows）与诊断脚本（-c），Linux venv 无双进程问题
    for proc in candidates:
        cl = ' '.join(proc.info.get('cmdline') or [])
        if '-c' in cl:
            continue
        if os.name == 'nt' and ('.venv' in cl or '\\venv\\' in cl):
            continue
        return proc
    return candidates[0]


def _count_messages_by_day(days=7):
    """统计日志中近 N 天每日消息数（接收+发送）"""
    result = {}
    today = date.today()
    for i in range(days - 1, -1, -1):
        result[(today - timedelta(days=i)).isoformat()] = {'received': 0, 'sent': 0}
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                m = re.match(r'\[(\d{4}-\d{2}-\d{2})', line)
                if not m:
                    continue
                day = m.group(1)
                if day in result:
                    if '收到来自' in line:
                        result[day]['received'] += 1
                    elif '发送消息' in line:
                        result[day]['sent'] += 1
    except OSError:
        pass
    return result


def _check_qq_online():
    """通过 Milky API 探测 QQ 协议端在线状态"""
    try:
        with open(CONFIG_JSON_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        server = cfg.get('milky_server', '')
        token = cfg.get('milky_token', '')
        if not server:
            return {'online': False, 'detail': '未配置 Milky 协议端 (milky_server)'}
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        resp = requests.post(f"{server}/api/get_login_info", json={}, headers=headers, timeout=2)
        if resp.status_code == 200:
            data = resp.json()
            if data.get('status') == 'ok':
                uin = data.get('data', {}).get('uin')
                nickname = data.get('data', {}).get('nickname')
                return {'online': True, 'detail': f'QQ {uin} ({nickname}) 在线', 'uin': uin}
            return {'online': False, 'detail': f"协议端响应异常: {data.get('message', '未知')}"}
        if resp.status_code == 401:
            return {'online': False, 'detail': '协议端鉴权失败（milky_token 不匹配）'}
        return {'online': False, 'detail': f'协议端 HTTP {resp.status_code}'}
    except requests.exceptions.ConnectionError:
        return {'online': False, 'detail': '协议端未启动或无法连接'}
    except Exception as e:
        return {'online': False, 'detail': str(e)}


def get_framework_version():
    """从 core/bot.py 注释读取框架版本号"""
    try:
        bot_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'core', 'bot.py')
        with open(bot_py, 'r', encoding='utf-8') as f:
            for line in f:
                if '版本' in line:
                    m = re.search(r'BETA\s+([\d.]+)', line)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return '0.1.0.122'


def _find_bot_processes():
    """查找所有 Lunar X 主进程（含 venv 启动器与真解释器）"""
    result = []
    try:
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline') or []
                if any('main.py' in c for c in cmdline):
                    result.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return result


@app.route('/api/version', methods=['GET'])
def get_version():
    return jsonify({'version': get_framework_version()})

@app.route('/api/restart_bot', methods=['POST'])
def restart_bot():
    """重启 Lunar X 机器人进程（先杀后启）"""
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        main_py = os.path.join(base_dir, 'main.py')

        # Linux + systemd：走服务管理器重启，避免绕过 systemd 造成双实例竞争
        if os.name != 'nt' and shutil.which('systemctl'):
            subprocess.Popen(['systemctl', 'restart', 'lunarx'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            app.logger.info("机器人重启中（systemctl restart lunarx）")
            return jsonify({'message': '机器人重启中（systemd），请稍候约 5 秒'})

        # 1. 终止现有 bot 进程（启动器 + 真解释器）
        killed = []
        for proc in _find_bot_processes():
            try:
                proc.kill()
                killed.append(proc.pid)
            except Exception as e:
                app.logger.warning(f"终止 bot 进程 {proc.pid} 失败: {e}")
        time.sleep(1.5)

        # 2. 重新启动（独立进程，脱离 WebUI）
        flags = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        subprocess.Popen(
            [sys.executable, main_py],
            cwd=base_dir,
            creationflags=flags,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        app.logger.info(f"机器人重启完成（旧进程: {killed or '无'}，已启动新进程）")
        return jsonify({'message': '机器人重启中，请稍候约 5 秒', 'killed': killed})
    except Exception as e:
        app.logger.error(f"重启机器人失败: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/dashboard', methods=['GET'])
def get_dashboard():
    """数据看板聚合数据"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data = {}

    # 1. 数据占用量（Lunar_X 相关目录）
    dirs = {}
    for name in ('logs', 'plugins', 'uploads', 'templates', 'core'):
        path = os.path.join(base_dir, name)
        dirs[name] = _dir_size(path) if os.path.exists(path) else 0
    dirs['config_files'] = sum(
        os.path.getsize(p) if os.path.exists(p) else 0
        for p in (CONFIG_JSON_PATH, ADMIN_JSON_PATH, WEBUI_JSON_PATH, APPSETTINGS_PATH))
    data['storage'] = {
        'dirs': dirs,
        'total': sum(dirs.values()),
        'log_file': os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0,
    }

    # 2. 系统运行情况
    try:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage(base_dir[:3] if os.name == 'nt' else '/')
        data['system'] = {
            'cpu_percent': psutil.cpu_percent(interval=0.5),
            'memory_total': vm.total,
            'memory_used': vm.used,
            'memory_percent': vm.percent,
            'disk_total': disk.total,
            'disk_used': disk.used,
            'disk_percent': disk.percent,
            'boot_time': psutil.boot_time(),
        }
    except Exception as e:
        data['system'] = {'error': str(e)}

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
            data['bot'] = {
                'running': True,
                'pid': bot_proc.pid,
                'cpu_seconds': cpu_seconds,
                'cpu_seconds_today': cpu_seconds_today,
                'memory_bytes': mem.rss,
                'uptime': uptime,
                'create_time': bot_proc.create_time(),
            }
        except Exception:
            data['bot'] = {'running': True, 'pid': bot_proc.pid}
    else:
        data['bot'] = {'running': False}

    # 4. 在线情况
    data['qq'] = _check_qq_online()

    # 5. 7 天消息统计
    msg_by_day = _count_messages_by_day(7)
    data['messages'] = {
        'days': [{'date': d, **msg_by_day[d]} for d in sorted(msg_by_day)],
        'total_7d': sum(v['received'] + v['sent'] for v in msg_by_day.values()),
    }

    return jsonify(data)

# 日志读取统一用二进制 + 字节偏移量。
# 不能用文本模式的 seek(字节数)：文本流的 seek 只接受 tell() 返回的不透明游标，
# 传字节数在换行转换（Windows CRLF）和多字节 UTF-8 下会错位，导致重复行或乱码。
LOG_HISTORY_SCAN = 512 * 1024      # 取历史时最多回扫的字节数，避免整文件读进内存


def _read_log_tail(max_bytes=LOG_HISTORY_SCAN):
    """读取日志尾部，返回 (行列表, 文件当前字节大小)。文件不存在返回 (None, 0)。"""
    try:
        size = os.path.getsize(LOG_FILE)
        with open(LOG_FILE, 'rb') as f:
            start = max(0, size - max_bytes)
            f.seek(start)
            data = f.read()
    except FileNotFoundError:
        return None, 0
    if start > 0:
        # 回扫起点大概率落在某行中间，丢掉这半行
        nl = data.find(b'\n')
        data = data[nl + 1:] if nl >= 0 else b''
    text = data.decode('utf-8', errors='replace')
    return text.splitlines(), size


@app.route('/api/logs', methods=['GET'])
def get_logs():
    """日志历史（最后 N 行）。

    同时返回 offset（当前文件字节大小），客户端应拿它作为 /api/logs/tail 的起点，
    这样历史与增量之间不会漏行也不会重复。
    """
    tail = request.args.get('tail', 200, type=int)
    tail = max(1, min(tail, 5000))
    try:
        lines, size = _read_log_tail()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if lines is None:
        return jsonify({'lines': [], 'exists': False, 'offset': 0})
    return jsonify({'lines': lines[-tail:], 'exists': True, 'offset': size})


@app.route('/api/logs/tail', methods=['GET'])
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
        offset = int(request.args.get('offset', -1))
    except (TypeError, ValueError):
        offset = -1

    deadline = time.time() + 20.0      # 单轮最长挂 20 秒，之后空返回让客户端续接
    interval = 0.05

    while True:
        try:
            size = os.path.getsize(LOG_FILE)
        except OSError:
            # 日志文件还没建出来，等它出现
            if time.time() >= deadline:
                return jsonify({'offset': 0, 'lines': [], 'exists': False, 'rotated': False})
            time.sleep(interval)
            continue

        rotated = False
        if offset < 0:
            offset = size          # 首次连接：只要增量，不回放历史
        elif offset > size:
            offset, rotated = 0, True   # 文件被轮转/清空，从头再来

        if size > offset:
            with open(LOG_FILE, 'rb') as f:
                f.seek(offset)
                data = f.read(size - offset)
            cut = data.rfind(b'\n')
            if cut >= 0:
                # 只消费完整行，剩下的半行留到下一轮，避免把写了一半的日志推出去
                consumed = data[:cut + 1]
                text = consumed.decode('utf-8', errors='replace')
                return jsonify({
                    'offset': offset + len(consumed),
                    'lines': text.splitlines(),
                    'exists': True,
                    'rotated': rotated,
                })

        if time.time() >= deadline:
            return jsonify({'offset': offset, 'lines': [], 'exists': True, 'rotated': rotated})
        time.sleep(interval)

@app.route('/api/change_password', methods=['POST'])
def change_password():
    data = request.get_json() or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')
    new_username = data.get('new_username', '')

    config = load_webui_config()
    stored_hash = config.get('password_hash', '')

    if not verify_password(old_password, stored_hash):
        return jsonify({'error': '旧密码错误'}), 401
    if len(new_password) < 4:
        return jsonify({'error': '新密码长度至少 4 位'}), 400
    if new_username is not None:
        new_username = new_username.strip()
        if not new_username:
            return jsonify({'error': '用户名不能为空'}), 400
        if len(new_username) < 3:
            return jsonify({'error': '用户名长度至少 3 位'}), 400

    config['password_hash'] = hash_password(new_password)
    if new_username is not None:
        config['username'] = new_username
    save_webui_config(config)
    if new_username is not None:
        session['username'] = new_username
    app.logger.info("WebUI 凭据已修改")
    return jsonify({'message': '用户名与密码已修改' if new_username is not None else '密码修改成功'})

@app.route('/api/config/<config_type>', methods=['GET'])
def get_config(config_type):
    try:
        if config_type == 'appsettings':
            with open(APPSETTINGS_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        elif config_type == 'config':
            with open(CONFIG_JSON_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        elif config_type == 'admin':
            with open(ADMIN_JSON_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        elif config_type == 'webui':
            with open(WEBUI_JSON_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)
        else:
            return jsonify({'error': 'Invalid config type'}), 400
        
        return jsonify(config)
    except FileNotFoundError:
        app.logger.error(f"Config file not found for {config_type}.")
        return jsonify({'error': f'Config file not found for {config_type}'}), 404
    except json.JSONDecodeError:
        app.logger.error(f"Invalid JSON in config file for {config_type}.")
        return jsonify({'error': f'Invalid JSON in config file for {config_type}'}), 500
    except Exception as e:
        app.logger.error(f"Error loading config {config_type}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/config/<config_type>', methods=['POST'])
def update_config(config_type):
    try:
        data = request.get_json()
        
        if config_type == 'appsettings':
            with open(APPSETTINGS_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        elif config_type == 'config':
            with open(CONFIG_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        elif config_type == 'admin':
            with open(ADMIN_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        elif config_type == 'webui':
            with open(WEBUI_JSON_PATH, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            return jsonify({'error': 'Invalid config type'}), 400
        
        app.logger.info(f"Config {config_type} updated successfully")
        return jsonify({'message': 'Config updated successfully'})
    except Exception as e:
        app.logger.error(f"Error updating config {config_type}: {str(e)}")
        return jsonify({'error': str(e)}), 500

def get_plugins_list():
    """扫描 plugins/ 目录。

    判定规则与 core/plugin_manager.py 保持一致：
      · 目录插件必须含 setup.py，否则不算插件（以前任意目录都会被列出来）
      · 只有 .py 结尾的文件才算单文件插件（以前 xxx.md 会被当成目录插件列出）
    带 plugin.json 的插件会附上清单信息，供前端渲染配置表单。
    """
    plugins = []
    plugins_dir = app.config['PLUGINS_DIR']

    if not os.path.exists(plugins_dir):
        return []

    for item in sorted(os.listdir(plugins_dir)):
        if item == '__pycache__' or item.startswith('_'):
            continue

        item_path = os.path.join(plugins_dir, item)
        is_disabled = item.startswith('d_')
        base_name = item[2:] if is_disabled else item

        if os.path.isdir(item_path):
            # 目录里没有 setup.py 就不是插件（可能只是资源目录）
            if not os.path.exists(os.path.join(item_path, 'setup.py')):
                continue
            plugin_type = 'directory'
        elif base_name.endswith('.py'):
            plugin_type = 'file'
            base_name = base_name[:-3]
        else:
            continue                      # .md / .txt 等一律不是插件

        plugin_info = {
            'name': base_name,
            'full_name': item,
            'enabled': not is_disabled,
            'type': plugin_type,
        }

        if plugin_type == 'directory':
            readme_path = os.path.join(item_path, 'README.md')
        else:
            readme_path = os.path.join(plugins_dir, f'{base_name}.md')
        plugin_info['has_help'] = bool(readme_path and os.path.exists(readme_path))

        # 清单（可选）：有 config 声明才在前端显示「配置」按钮
        manifest = pc.load_manifest(plugins_dir, base_name)
        plugin_info['display_name'] = manifest.get('display_name') or base_name
        plugin_info['version'] = manifest.get('version') or ''
        plugin_info['description'] = manifest.get('description') or ''
        plugin_info['has_config'] = bool(pc.get_schema(manifest))

        plugins.append(plugin_info)
    return plugins


@app.route('/api/plugins/<plugin_name>/manifest', methods=['GET'])
def get_plugin_manifest(plugin_name):
    """插件清单（含配置项 schema），前端据此动态渲染表单"""
    if not _safe_plugin_name(plugin_name):
        return jsonify({'error': '非法插件名'}), 400
    plugins_dir = app.config['PLUGINS_DIR']
    manifest = pc.load_manifest(plugins_dir, plugin_name)
    if not manifest:
        return jsonify({'error': '该插件没有 plugin.json 清单'}), 404
    return jsonify({
        'name': manifest.get('name') or plugin_name,
        'display_name': manifest.get('display_name') or plugin_name,
        'version': manifest.get('version') or '',
        'author': manifest.get('author') or '',
        'description': manifest.get('description') or '',
        'config': pc.get_schema(manifest),
    })


@app.route('/api/plugins/<plugin_name>/config', methods=['GET'])
def get_plugin_config(plugin_name):
    """插件当前配置值（schema 的 default 已合并进来），拍平成 {点号key: 值}"""
    if not _safe_plugin_name(plugin_name):
        return jsonify({'error': '非法插件名'}), 400
    plugins_dir = app.config['PLUGINS_DIR']
    if not pc.plugin_dir(plugins_dir, plugin_name):
        return jsonify({'error': '插件不存在或不是目录形态'}), 404
    schema = pc.get_schema(pc.load_manifest(plugins_dir, plugin_name))
    merged = pc.load_config(plugins_dir, plugin_name)
    return jsonify({'values': pc.flatten_for_form(schema, merged)})


@app.route('/api/plugins/<plugin_name>/config', methods=['POST'])
def update_plugin_config(plugin_name):
    """保存插件配置。

    只接受 schema 里声明过的 key，并按声明的类型校验——
    否则这个接口等于允许往插件目录里写任意 JSON。
    """
    if not _safe_plugin_name(plugin_name):
        return jsonify({'error': '非法插件名'}), 400
    plugins_dir = app.config['PLUGINS_DIR']
    if not pc.plugin_dir(plugins_dir, plugin_name):
        return jsonify({'error': '插件不存在或不是目录形态'}), 404

    schema = pc.get_schema(pc.load_manifest(plugins_dir, plugin_name))
    if not schema:
        return jsonify({'error': '该插件没有声明可配置项'}), 400

    incoming = request.get_json(silent=True) or {}
    if not isinstance(incoming, dict):
        return jsonify({'error': '请求体必须是 JSON 对象'}), 400

    existing = pc.load_raw_config(plugins_dir, plugin_name)
    ok, built, errors = pc.validate_and_build(schema, incoming, existing)
    if not ok:
        return jsonify({'error': '；'.join(errors)}), 400
    try:
        pc.save_config(plugins_dir, plugin_name, built)
    except Exception as e:
        return jsonify({'error': f'保存失败: {e}'}), 500
    # bot 端按 config.json 的 mtime 缓存，写完下一条消息就会读到新值
    return jsonify({'message': '配置已保存，立即生效'})

@app.route('/api/plugins', methods=['GET'])
def get_plugins():
    try:
        plugins = get_plugins_list()
        return jsonify(plugins)
    except Exception as e:
        app.logger.error(f"Error getting plugins: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/plugins/<plugin_name>', methods=['GET'])
def get_plugin_details(plugin_name):
    if not _safe_plugin_name(plugin_name):
        return jsonify({'error': '非法插件名'}), 400
    try:
        plugins_dir = app.config['PLUGINS_DIR']
        
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
            return jsonify({'error': 'Plugin not found'}), 404
        
        details = {
            'name': plugin_name,
            'type': 'directory' if os.path.isdir(target_path) else 'file'
        }
        
        readme_content = 'No help available'
        if details['type'] == 'directory':
            readme_path = os.path.join(target_path, 'README.md')
            if os.path.exists(readme_path):
                with open(readme_path, 'r', encoding='utf-8') as f:
                    readme_content = f.read()
        elif details['type'] == 'file':
            readme_path = os.path.join(plugins_dir, f"{plugin_name}.md")
            if os.path.exists(readme_path):
                with open(readme_path, 'r', encoding='utf-8') as f:
                    readme_content = f.read()
        
        details['help'] = readme_content
        
        return jsonify(details)
    except Exception as e:
        app.logger.error(f"Error getting plugin details for {plugin_name}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/plugins/<plugin_name>', methods=['PUT'])
def toggle_plugin(plugin_name):
    if not _safe_plugin_name(plugin_name):
        return jsonify({'error': '非法插件名'}), 400
    try:
        plugins_dir = app.config['PLUGINS_DIR']
        
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
            return jsonify({'error': 'Plugin not found'}), 404

        is_currently_enabled = not os.path.basename(current_path).startswith('d_')

        if is_currently_enabled:
            new_name = f"d_{os.path.basename(current_path)}"
            new_path = os.path.join(plugins_dir, new_name)
            os.rename(current_path, new_path)
            app.logger.info(f"Plugin {plugin_name} disabled")
            return jsonify({'message': 'Plugin disabled successfully'})
        else:
            # 只去掉开头的 d_ 前缀。原来用 replace() 会把名字中间的 d_ 也替换掉，
            # 比如 d_word_count 会变成 wordcount，插件从此再也找不到
            original_name = os.path.basename(current_path)[2:]
            original_path = os.path.join(plugins_dir, original_name)
            os.rename(current_path, original_path)
            app.logger.info(f"Plugin {plugin_name} enabled")
            return jsonify({'message': 'Plugin enabled successfully'})
    except Exception as e:
        app.logger.error(f"Error toggling plugin {plugin_name}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/plugins/<plugin_name>', methods=['DELETE'])
def uninstall_plugin(plugin_name):
    if not _safe_plugin_name(plugin_name):
        return jsonify({'error': '非法插件名'}), 400
    try:
        plugins_dir = app.config['PLUGINS_DIR']
        
        possible_paths = [
            os.path.join(plugins_dir, plugin_name),
            os.path.join(plugins_dir, f"{plugin_name}.py"),
            os.path.join(plugins_dir, f"d_{plugin_name}"),
            os.path.join(plugins_dir, f"d_{plugin_name}.py")
        ]
        
        found_path = None
        for p in possible_paths:
            if os.path.exists(p):
                found_path = p
                break
        
        if not found_path:
            return jsonify({'error': 'Plugin not found'}), 404

        if found_path.endswith('.py') or found_path.endswith('.md'):
            md_path = os.path.join(plugins_dir, f"{plugin_name}.md")
            if os.path.exists(md_path):
                os.remove(md_path)
                app.logger.info(f"Removed associated markdown file: {md_path}")

        if os.path.isdir(found_path):
            shutil.rmtree(found_path)
        else:
            os.remove(found_path)
            
        app.logger.info(f"Plugin {plugin_name} uninstalled")
        return jsonify({'message': 'Plugin uninstalled successfully'})
    except Exception as e:
        app.logger.error(f"Error uninstalling plugin {plugin_name}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/available_plugins', methods=['GET'])
def get_available_plugins():
    try:
        with open(WEBUI_JSON_PATH, 'r', encoding='utf-8') as f:
            webui_config = json.load(f)
        
        github_mirror = webui_config.get('github_mirror', '').strip()
        github_pat = webui_config.get('github_pat', '').strip()
        plugins_index_repo = webui_config.get('plugins_index_repo', 'MacroSTAR-Org/Unisphere')
        
        headers = {}
        if github_pat:
            headers['Authorization'] = f'token {github_pat}'

        github_api_url = f"https://api.github.com/repos/{plugins_index_repo}/contents/"
        
        app.logger.info(f"Fetching available plugins from GitHub API: {github_api_url}")
        response = requests.get(github_api_url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            if response.status_code == 403 and "rate limit exceeded" in response.text.lower():
                error_msg = "GitHub API 速率限制已超出。请在WebUI配置中填写GitHub个人访问令牌 (PAT) 以提高速率限制。"
                app.logger.error(error_msg)
                return jsonify({'error': error_msg}), 403
            else:
                app.logger.error(f"GitHub API returned status {response.status_code}: {response.text}")
                return jsonify({'error': f'Failed to fetch plugins from GitHub: {response.status_code} - {response.text}'}), 500
        
        items = response.json()
        available_plugins = []
        
        installed_plugins_raw = get_plugins_list() 
        installed_plugin_names = {p['name'] for p in installed_plugins_raw}
        
        for item in items:
            if item['type'] == 'dir':
                plugin_name = item['name']
                
                if plugin_name in installed_plugin_names:
                    continue
                
                raw_zip_url_base = f"https://github.com/{plugins_index_repo}/archive/refs/heads/main.zip"
                plugin_download_url = f"{github_mirror}{raw_zip_url_base}" if github_mirror else raw_zip_url_base

                raw_readme_url_base = f"https://raw.githubusercontent.com/{plugins_index_repo}/main/{plugin_name}/README.md"
                readme_fetch_url = f"{github_mirror}{raw_readme_url_base}" if github_mirror else raw_readme_url_base

                description = "No description available"
                try:
                    readme_response = requests.get(readme_fetch_url, timeout=5)
                    if readme_response.status_code == 200:
                        desc_text = readme_response.text
                        desc_text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', desc_text)
                        desc_text = re.sub(r'#+\s*', '', desc_text)
                        desc_text = ' '.join(desc_text.split()).strip()
                        description = desc_text[:200] + "..." if len(desc_text) > 200 else desc_text
                    else:
                        app.logger.warning(f"Could not fetch README for {plugin_name} from {readme_fetch_url}: {readme_response.status_code}")
                except requests.exceptions.RequestException as req_e:
                    app.logger.warning(f"Error fetching README for {plugin_name}: {req_e}")
                except Exception as ex:
                    app.logger.warning(f"Error processing README for {plugin_name}: {ex}")
                
                available_plugins.append({
                    'name': plugin_name,
                    'description': description,
                    'url': plugin_download_url,
                    'path': plugin_name
                })
        
        app.logger.info(f"Found {len(available_plugins)} available plugins")
        return jsonify(available_plugins)
    except Exception as e:
        app.logger.error(f"Error getting available plugins: {str(e)}")
        return jsonify({'error': str(e)}), 500

def _process_plugin_structure(plugin_name, extracted_plugin_root_path, log_callback):
    plugins_dir = app.config['PLUGINS_DIR']
    
    contents_at_root_path = os.listdir(extracted_plugin_root_path)
    if len(contents_at_root_path) == 1 and \
       os.path.isdir(os.path.join(extracted_plugin_root_path, contents_at_root_path[0])) and \
       contents_at_root_path[0] == plugin_name:
        
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
    setup_path = os.path.join(extracted_plugin_root_path, 'setup.py')
    legacy_entry = os.path.join(extracted_plugin_root_path, f"{plugin_name}.py")

    if not os.path.exists(setup_path):
        if os.path.exists(legacy_entry):
            shutil.move(legacy_entry, setup_path)
            log_callback(f"已将旧式入口 '{plugin_name}.py' 重命名为 'setup.py'（目录插件的入口约定）。")
        else:
            # 没有 setup.py 也没有同名 .py，退而求其次找唯一的顶层 .py 当入口
            py_files = [f for f in os.listdir(extracted_plugin_root_path)
                        if f.endswith('.py') and f != '__init__.py']
            if len(py_files) == 1:
                shutil.move(os.path.join(extracted_plugin_root_path, py_files[0]), setup_path)
                log_callback(f"已将唯一入口 '{py_files[0]}' 重命名为 'setup.py'。")
            else:
                log_callback(f"⚠️ 未找到 setup.py，插件可能无法被加载（目录插件入口必须叫 setup.py）。")

@app.route('/api/plugins', methods=['POST'])
def install_plugin():
    data = request.get_json()
    plugin_url = data.get('url')
    plugin_name = data.get('name')
    plugin_path_in_repo = data.get('path')
    use_pypi_mirror = data.get('use_pypi_mirror', False)
    pypi_mirror = data.get('pypi_mirror', '')
    plugins_index_repo_name_only = data.get('plugins_index_repo_name_only', 'Unisphere')

    def generate_install_logs():
        def log_progress(msg):
            app.logger.info(f"[Install Progress] {msg}")
            yield f"data: {msg}\n\n"

        zip_path = None
        temp_extract_root = None
        final_plugin_target_dir = None
        
        try:
            if not plugin_url or not plugin_name or not plugin_path_in_repo:
                yield f"data: Error: Missing plugin URL, name, or path\n\n"
                yield f"data: INSTALL_FAILED\n\n"
                return

            yield from log_progress(f"开始下载插件: {plugin_name} from {plugin_url}")
            response = requests.get(plugin_url, stream=True, timeout=60)
            response.raise_for_status()

            zip_path = os.path.join(app.config['UPLOAD_FOLDER'], f'{plugin_name}_repo.zip')
            total_size = int(response.headers.get('content-length', 0))
            downloaded_size = 0
            with open(zip_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if total_size > 0:
                            percent = (downloaded_size / total_size) * 100
                            yield from log_progress(f"下载中: {downloaded_size}/{total_size} ({percent:.2f}%)")
                        else:
                            yield from log_progress(f"下载中: {downloaded_size} bytes")
            yield from log_progress(f"插件 {plugin_name} 下载完成。")

            plugins_dir = app.config['PLUGINS_DIR']
            final_plugin_target_dir = os.path.join(plugins_dir, plugin_name) 
            temp_extract_root = os.path.join(app.config['UPLOAD_FOLDER'], 'temp_plugin_extract')
            
            os.makedirs(temp_extract_root, exist_ok=True)

            yield from log_progress(f"开始解压插件: {plugin_name}...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(temp_extract_root)
            yield from log_progress(f"插件 {plugin_name} 解压完成。")
            
            extracted_repo_root = None
            for item in os.listdir(temp_extract_root):
                if item.startswith(f"{plugins_index_repo_name_only}-"):
                    extracted_repo_root = os.path.join(temp_extract_root, item)
                    break
            
            if not extracted_repo_root:
                dirs_in_temp = [d for d in os.listdir(temp_extract_root) if os.path.isdir(os.path.join(temp_extract_root, d))]
                if len(dirs_in_temp) == 1:
                    extracted_repo_root = os.path.join(temp_extract_root, dirs_in_temp[0])
                else:
                    raise Exception("Could not find the extracted repository root directory.")

            source_plugin_content_dir = os.path.join(extracted_repo_root, plugin_path_in_repo)

            if not os.path.exists(source_plugin_content_dir):
                raise Exception(f"Plugin content directory '{source_plugin_content_dir}' not found in the extracted repository.")

            if os.path.exists(final_plugin_target_dir):
                shutil.rmtree(final_plugin_target_dir)
            os.makedirs(final_plugin_target_dir)

            for item in os.listdir(source_plugin_content_dir):
                shutil.move(os.path.join(source_plugin_content_dir, item), final_plugin_target_dir)
            
            yield from log_progress(f"插件 '{plugin_name}' 内容已移动到临时安装位置 '{final_plugin_target_dir}'。")

            _process_plugin_structure(plugin_name, final_plugin_target_dir, log_progress)

            if os.path.exists(final_plugin_target_dir) and os.path.isdir(final_plugin_target_dir):
                requirements_path = os.path.join(final_plugin_target_dir, 'requirements.txt')
                if os.path.exists(requirements_path):
                    yield from log_progress(f"开始安装插件 {plugin_name} 的依赖...")
                    mirror_cmd = []
                    if use_pypi_mirror and pypi_mirror:
                        mirror_cmd = ['-i', pypi_mirror]
                    
                    process = subprocess.Popen([sys.executable, '-m', 'pip', 'install', '-r', requirements_path] + mirror_cmd, 
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                    
                    for line in process.stdout:
                        yield from log_progress(f"PIP: {line.strip()}")
                    for line in process.stderr:
                        yield from log_progress(f"PIP ERROR: {line.strip()}")

                    process.wait()

                    if process.returncode != 0:
                        yield from log_progress(f"安装依赖失败，退出码: {process.returncode}")
                        yield f"data: Error: 插件已安装，但依赖安装失败。详情请查看日志。\n\n"
                        yield f"data: INSTALL_FAILED\n\n"
                        return
                    
                    # 保留 requirements.txt：它是插件的一部分，删掉之后
                    # 插件更新或换环境时就没法重新装依赖了
                    yield from log_progress(f"插件 {plugin_name} 的依赖安装成功。")
                else:
                    yield from log_progress(f"插件 {plugin_name} (目录插件) 没有找到 requirements.txt，跳过依赖安装。")
            else:
                yield from log_progress(f"插件 {plugin_name} (单文件插件) 没有找到 requirements.txt，跳过依赖安装。")
            
            yield from log_progress(f"插件 {plugin_name} 安装成功。")
            yield f"data: INSTALL_SUCCESS\n\n"

        except requests.exceptions.RequestException as req_e:
            error_msg = f"下载插件时发生网络错误: {req_e}"
            app.logger.error(error_msg)
            yield f"data: Error: {error_msg}\n\n"
            yield f"data: INSTALL_FAILED\n\n"
        except Exception as e:
            error_msg = f"安装插件 {plugin_name} 时发生错误: {str(e)}"
            app.logger.error(error_msg)
            yield f"data: Error: {error_msg}\n\n"
            yield f"data: INSTALL_FAILED\n\n"
        finally:
            if zip_path and os.path.exists(zip_path):
                os.remove(zip_path)
            if temp_extract_root and os.path.exists(temp_extract_root):
                shutil.rmtree(temp_extract_root, ignore_errors=True)

    return Response(stream_with_context(generate_install_logs()), mimetype='text/event-stream')

if __name__ == '__main__':
    app.run(debug=False, threaded=True, host='0.0.0.0', port=5000)

