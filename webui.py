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


@app.after_request
def add_no_cache(response):
    """页面/API 不缓存，避免浏览器加载旧版 HTML/JS"""
    if request.path.startswith('/api/logs/stream'):
        return response
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response


@app.before_request
def require_login():
    """除登录页/登录接口外，所有页面与 API 均需登录"""
    if request.path == '/login' or request.path == '/api/login' or request.path == '/api/auth_status':
        return None
    if session.get('logged_in'):
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
            "plugins_index_repo": "IntelliMarkets/Jianer_Plugins_Index",
            "username": "lunarx",
            "password_hash": hash_password("lunarx"),
            "session_secret": secrets.token_hex(32)
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
                webui_config["plugins_index_repo"] = "IntelliMarkets/Jianer_Plugins_Index"
                updated = True
            if "username" not in webui_config:
                webui_config["username"] = "lunarx"
                webui_config["password_hash"] = hash_password("lunarx")
                updated = True
            if "session_secret" not in webui_config:
                webui_config["session_secret"] = secrets.token_hex(32)
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

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json() or {}
    username = data.get('username', '')
    password = data.get('password', '')

    config = load_webui_config()
    stored_username = config.get('username', 'lunarx')
    stored_hash = config.get('password_hash', '')

    if username == stored_username and verify_password(password, stored_hash):
        session['logged_in'] = True
        session['username'] = username
        app.logger.info(f"WebUI 登录成功: {username}")
        return jsonify({'message': '登录成功', 'username': username})
    app.logger.warning(f"WebUI 登录失败: {username}")
    return jsonify({'error': '用户名或密码错误'}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': '已退出登录'})


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
    """查找 Lunar X 主进程（python main.py）。

    Windows venv 的 python.exe 是启动器（会再拉起真解释器），
    优先返回非 venv 路径的真身，避免读到启动器的空数据。
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
    # 非 venv 启动器的优先（真解释器进程）
    for proc in candidates:
        cl = ' '.join(proc.info.get('cmdline') or [])
        if '.venv' not in cl and '\\venv\\' not in cl:
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
        resp = requests.post(f"{server}/api/get_login_info", json={}, headers=headers, timeout=4)
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

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """获取框架日志历史（最后 N 行）"""
    tail = request.args.get('tail', 200, type=int)
    tail = max(1, min(tail, 5000))
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except FileNotFoundError:
        return jsonify({'lines': [], 'exists': False})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'lines': lines[-tail:], 'exists': True})

@app.route('/api/logs/stream')
def stream_logs():
    """SSE 实时推送框架日志新行"""
    def generate():
        last_size = 0
        try:
            last_size = os.path.getsize(LOG_FILE)
        except OSError:
            pass
        heartbeat = 0
        while True:
            try:
                size = os.path.getsize(LOG_FILE)
                if size < last_size:
                    # 日志文件轮转，从头开始读
                    last_size = 0
                if size > last_size:
                    with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
                        f.seek(last_size)
                        new_data = f.read()
                    last_size = size
                    for line in new_data.splitlines():
                        yield f"data: {line}\n\n"
            except Exception:
                pass
            heartbeat += 1
            if heartbeat % 15 == 0:
                yield ": ping\n\n"
            time.sleep(1)
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/api/change_password', methods=['POST'])
def change_password():
    data = request.get_json() or {}
    old_password = data.get('old_password', '')
    new_password = data.get('new_password', '')

    config = load_webui_config()
    stored_hash = config.get('password_hash', '')

    if not verify_password(old_password, stored_hash):
        return jsonify({'error': '旧密码错误'}), 401
    if len(new_password) < 4:
        return jsonify({'error': '新密码长度至少 4 位'}), 400

    config['password_hash'] = hash_password(new_password)
    save_webui_config(config)
    app.logger.info("WebUI 密码已修改")
    return jsonify({'message': '密码修改成功'})

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
    plugins = []
    plugins_dir = app.config['PLUGINS_DIR']
    
    if not os.path.exists(plugins_dir):
        return []

    for item in os.listdir(plugins_dir):
        if item == '__pycache__':
            continue
            
        item_path = os.path.join(plugins_dir, item)
        
        is_disabled = item.startswith('d_')
        base_name = item[2:] if is_disabled else item
        
        plugin_type = 'directory'
        if base_name.endswith('.py'):
            plugin_type = 'file'
            base_name = base_name[:-3]

        plugin_info = {
            'name': base_name,
            'full_name': item,
            'enabled': not is_disabled,
            'type': plugin_type
        }
        
        readme_path = None
        if plugin_info['type'] == 'directory':
            readme_path = os.path.join(item_path, 'README.md')
        elif plugin_info['type'] == 'file':
            readme_path = os.path.join(plugins_dir, f"{plugin_info['name']}.md")

        if readme_path and os.path.exists(readme_path):
            plugin_info['has_help'] = True
        else:
            plugin_info['has_help'] = False
        
        plugins.append(plugin_info)
    return plugins

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
            original_name = os.path.basename(current_path).replace('d_', '')
            original_path = os.path.join(plugins_dir, original_name)
            os.rename(current_path, original_path)
            app.logger.info(f"Plugin {plugin_name} enabled")
            return jsonify({'message': 'Plugin enabled successfully'})
    except Exception as e:
        app.logger.error(f"Error toggling plugin {plugin_name}: {str(e)}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/plugins/<plugin_name>', methods=['DELETE'])
def uninstall_plugin(plugin_name):
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
        plugins_index_repo = webui_config.get('plugins_index_repo', 'IntelliMarkets/Jianer_Plugins_Index')
        
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

    plugin_py_file_in_dir = f"{plugin_name}.py"
    plugin_py_path_in_dir = os.path.join(extracted_plugin_root_path, plugin_py_file_in_dir)
    
    if os.path.exists(plugin_py_path_in_dir):
        current_contents = os.listdir(extracted_plugin_root_path)
        
        significant_contents = [
            item for item in current_contents 
            if item != '__pycache__' and item != plugin_py_file_in_dir
        ]
        
        has_other_py_files = any(f.endswith('.py') for f in significant_contents)
        has_subdirectories = any(os.path.isdir(os.path.join(extracted_plugin_root_path, d)) for d in significant_contents)

        if not has_other_py_files and not has_subdirectories:
            log_callback(f"检测到单文件插件 '{plugin_name}.py'，正在将其移动到插件根目录并处理README。")
            
            shutil.move(plugin_py_path_in_dir, os.path.join(plugins_dir, f"{plugin_name}.py"))
            
            readme_path_in_dir = os.path.join(extracted_plugin_root_path, 'README.md')
            if os.path.exists(readme_path_in_dir):
                shutil.move(readme_path_in_dir, os.path.join(plugins_dir, f"{plugin_name}.md"))
                log_callback(f"重命名并移动 'README.md' 到 '{plugins_dir}/{plugin_name}.md'。")
            
            shutil.rmtree(extracted_plugin_root_path)
            log_callback(f"删除空目录 '{extracted_plugin_root_path}'。")
            
            return

@app.route('/api/plugins', methods=['POST'])
def install_plugin():
    data = request.get_json()
    plugin_url = data.get('url')
    plugin_name = data.get('name')
    plugin_path_in_repo = data.get('path')
    use_pypi_mirror = data.get('use_pypi_mirror', False)
    pypi_mirror = data.get('pypi_mirror', '')
    plugins_index_repo_name_only = data.get('plugins_index_repo_name_only', 'Jianer_Plugins_Index')

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
                    
                    os.remove(requirements_path) 
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

