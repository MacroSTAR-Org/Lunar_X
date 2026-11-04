"""生产环境 WSGI 入口。

开发时直接 `python webui.py`（Flask 内置服务器，单进程，仅本机）。
生产部署请用本文件配合真正的 WSGI 服务器：

    # Linux
    gunicorn -c deploy/gunicorn.conf.py wsgi:application
    # 任意平台（含 Windows）
    waitress-serve --listen=127.0.0.1:5000 wsgi:application

为什么单独一个文件：WebUI 的启动副作用（生成 config.json / webui.json、
随机初始密码）都在 webui.py 的 import 期完成，WSGI 服务器导入本模块时
就会跑一遍，不需要额外的初始化命令。

反向代理信任
------------
放在 Nginx / Caddy 后面时，必须设置 LUNARX_TRUST_PROXY，否则：

  * request.remote_addr 永远是反代地址（127.0.0.1），
    「设置 → 账户与安全 → IP 白名单」形同虚设，登录日志也全是同一个 IP；
  * Flask 认为自己在 HTTP 上，session cookie 不会带 Secure 标记。

LUNARX_TRUST_PROXY 的值是"你前面有几层反代"（通常是 1）。
**只在确实有可信反代时才设置**：客户端可以自己伪造 X-Forwarded-For，
无脑信任等于把 IP 白名单的钥匙交给用户。

若直接暴露到公网且没有反代，请改用 LUNARX_ALLOWED_HOSTS 限制 Host 头，
并保持 LUNARX_TRUST_PROXY 为空。
"""

import os

from webui import app as application

# ---------------------------------------------------------------------------
# 反代信任
# ---------------------------------------------------------------------------

_TRUST_PROXY = os.getenv("LUNARX_TRUST_PROXY", "").strip()

if _TRUST_PROXY:
    try:
        proxy_count = int(_TRUST_PROXY)
    except ValueError:
        raise SystemExit(
            f"LUNARX_TRUST_PROXY 必须是正整数（反代层数），收到 {_TRUST_PROXY!r}"
        ) from None
    if proxy_count < 1:
        raise SystemExit("LUNARX_TRUST_PROXY 至少为 1；没有反代时请不要设置它")

    from werkzeug.middleware.proxy_fix import ProxyFix

    # x_for=1：X-Forwarded-For 最右侧一个才是反代写入的真实客户端 IP；
    # 左侧的是客户端自己伪造的部分，不该采信。
    # x_proto：让 url_for / redirect 知道外层是 https。
    # x_host：反代可能把 Host 改写成内部域名。
    application.wsgi_app = ProxyFix(  # type: ignore[method-assign]
        application.wsgi_app,
        x_for=proxy_count,
        x_proto=proxy_count,
        x_host=proxy_count,
    )

# ---------------------------------------------------------------------------
# Host 白名单（防 Host 头注入）
# ---------------------------------------------------------------------------

_ALLOWED_HOSTS = os.getenv("LUNARX_ALLOWED_HOSTS", "").strip()
if _ALLOWED_HOSTS:
    hosts = [h.strip() for h in _ALLOWED_HOSTS.split(",") if h.strip()]
    application.config["TRUSTED_HOSTS"] = hosts

# ---------------------------------------------------------------------------
# Cookie 安全标记
# ---------------------------------------------------------------------------
# 反代终止 TLS 时，前端访问的是 https，cookie 必须带 Secure，
# 否则它会在明文 HTTP 链路上裸奔。开了反代却忘了这一项是常见事故。
if os.getenv("LUNARX_COOKIE_SECURE", "0") == "1" and not application.config.get(
    "SESSION_COOKIE_SECURE"
):
    application.config["SESSION_COOKIE_SECURE"] = True

if __name__ == "__main__":
    raise SystemExit(
        "wsgi.py 不是可执行入口。请用 WSGI 服务器加载它：\n"
        "  gunicorn -c deploy/gunicorn.conf.py wsgi:application\n"
        "  waitress-serve --listen=127.0.0.1:5000 wsgi:application"
    )
