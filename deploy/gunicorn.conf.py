"""Gunicorn 配置：生产环境跑 WebUI。

用法：
    gunicorn -c deploy/gunicorn.conf.py wsgi:application

关键取舍：
  * 只监听 127.0.0.1：公网访问交给 Nginx / Caddy，TLS、限流、压缩都在那边做。
  * 用 gthread 线程 worker 而不是 gevent：WebUI 有文件下载和日志流（SSE），
    gevent 的 monkey patch 会和 aiohttp / requests 打架。
  * timeout 放宽到 120s：插件安装要跑 pip，慢网络下 30s 默认值会误杀。
"""

import multiprocessing
import os

# 从项目根目录算（deploy/ 的上一级），不依赖启动时的 cwd
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

bind = os.getenv("LUNARX_WEB_BIND", "127.0.0.1:5000")
workers = int(os.getenv("LUNARX_WEB_WORKERS", max(2, multiprocessing.cpu_count())))
worker_class = "gthread"
threads = int(os.getenv("LUNARX_WEB_THREADS", "4"))
timeout = int(os.getenv("LUNARX_WEB_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# 面板会话存在客户端 cookie 里，服务端无状态，
# 多 worker 不会互相踢下线（改密时靠 session_epoch 统一失效）。
preload_app = False

accesslog = os.path.join(BASE_DIR, "logs", "gunicorn_access.log")
errorlog = os.path.join(BASE_DIR, "logs", "gunicorn_error.log")
loglevel = os.getenv("LUNARX_WEB_LOGLEVEL", "info")

# 面板会下载插件 ZIP（上限 50MB），反代侧也要同步放宽。
# 这里给的是兜底，实际限制由 Nginx 的 client_max_body_size 控制。
max_request_size = 64 * 1024 * 1024
