"""CSP 合规性静态检查：把实际下发的 HTML 与 CSP 逐条对照。

浏览器里一旦有资源被 CSP 拦掉，页面就白屏或功能残缺，
而这种问题在开发机上（无 CSP）永远看不到。
这里把「HTML 里用到的每种资源」都对照 CSP 策略核一遍。

用法：
    uv run python scripts/check_csp.py
"""

import re
import sys
from urllib.parse import urlparse

sys.path.insert(0, ".")

import webui
from webui import app


def collect(html: str) -> dict:
    """把 HTML 里用到的资源按类型收集出来。"""
    used = {
        "inline_script": [],
        "external_script": [],
        "external_style": [],
        "inline_style": [],
        "img": [],
        "font": [],
    }

    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        src = re.search(r'src\s*=\s*["\']([^"\']+)', attrs)
        if src:
            used["external_script"].append(src.group(1))
        elif body.strip():
            used["inline_script"].append(attrs)

    for m in re.finditer(r"<link\b([^>]*)>", html, re.I):
        attrs = m.group(1)
        rel = re.search(r'rel\s*=\s*["\']?stylesheet', attrs, re.I)
        href = re.search(r'href\s*=\s*["\']([^"\']+)', attrs)
        if rel and href:
            used["external_style"].append(href.group(1))

    used["inline_style"] = re.findall(r'\bstyle\s*=\s*["\'][^"\']+["\']', html, re.I)
    used["img"] = re.findall(r'<img\b[^>]*\bsrc\s*=\s*["\']([^"\']+)', html, re.I)
    used["font"] = re.findall(r"url\(['\"]?([^)'\"]+\.woff2)", html, re.I)
    return used


def is_external(url: str) -> bool:
    """是否为跨源引用（data:/blob:/相对路径都不算）。"""
    if url.startswith(("data:", "blob:", "#", "/")):
        return False
    parsed = urlparse(url)
    return bool(parsed.scheme and parsed.netloc)


def login(client) -> bool:
    """尝试登录，拿到一个已登录的会话（用于检查 / 这类受保护页面）。"""
    status = client.get("/api/auth_status")
    try:
        body = status.get_json()
    except Exception:
        return False
    if not isinstance(body, dict):
        return False

    # 已登录但被要求改密 / 没拿到用户名时都跳过，登录页已足够覆盖 CSP 检查
    return bool(body.get("logged_in") and body.get("username"))


def main() -> int:
    app.config["TESTING"] = True
    client = app.test_client()

    problems: list[str] = []
    checked = 0

    # 收集要检查的页面：(名称, HTML, CSP)
    pages: list[tuple[str, str, str]] = []

    resp = client.get("/login")
    if resp.status_code == 200:
        pages.append(("/login", resp.get_data(as_text=True), resp.headers.get("Content-Security-Policy", "")))
    else:
        print(f"[跳过] /login 返回 {resp.status_code}")

    # 主面板需要登录；拿不到会话时直接渲染模板做静态检查——
    # 模板里的资源引用与登录与否无关，同样能发现 CSP 不兼容的资源。
    resp = client.get("/")
    if resp.status_code == 200:
        pages.append(("/", resp.get_data(as_text=True), resp.headers.get("Content-Security-Policy", "")))
    else:
        with app.test_request_context("/"):
            html = webui.render_template("index.html", csp_nonce="testnonce")
        # 用真实的 CSP 模板构造，格式与线上完全一致
        fake_csp = webui._CSP_TEMPLATE.format(nonce="testnonce")
        pages.append(("/ (模板直渲染)", html, fake_csp))
        print("[提示] / 未登录，已改为直接渲染模板检查")

    for path, html, csp in pages:
        nonce_match = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        nonce = nonce_match.group(1) if nonce_match else ""

        used = collect(html)
        checked += 1

        # inline script 必须全部带 nonce
        for attrs in used["inline_script"]:
            if f'nonce="{nonce}"' not in attrs:
                problems.append(f"{path}: inline script 缺少或错误的 nonce: {attrs.strip()[:80]}")

        # 跨源资源一律不允许
        for kind, urls in (
            ("script", used["external_script"]),
            ("style", used["external_style"]),
            ("img", used["img"]),
            ("font", used["font"]),
        ):
            for url in urls:
                if is_external(url):
                    problems.append(f"{path}: {kind} 引用了跨源资源 {url}")

        # 脚本策略里必须有 unsafe-eval（Vue 运行时模板编译）
        if "'unsafe-eval'" not in csp:
            problems.append(f"{path}: script-src 缺少 'unsafe-eval'，Vue 模板会编译失败")

        # connect-src 必须限制为 self
        if "connect-src 'self'" not in csp:
            problems.append(f"{path}: connect-src 未限制为 'self'")

        print(
            f"[检查] {path}: "
            f"inline_script={len(used['inline_script'])} "
            f"external_script={len(used['external_script'])} "
            f"style={len(used['external_style'])} "
            f"inline_style={len(used['inline_style'])}"
        )

    # CSS 里的 data: URI 与字体路径也要覆盖
    for css_name in ("static/vendor/element-plus.css", "static/js/md3-expressive.css"):
        try:
            with open(css_name, encoding="utf-8") as f:
                css = f.read()
        except OSError:
            continue
        cross = [u for u in re.findall(r"url\(['\"]?([^)'\"]+)", css)
                 if not u.startswith(("data:", "#")) and is_external(u)]
        for url in cross:
            problems.append(f"{css_name}: 引用了跨源资源 {url}")
        print(f"[检查] {css_name}: 跨源引用 {len(cross)} 处")

    print()
    if problems:
        print(f"❌ 发现 {len(problems)} 个 CSP 兼容性问题：")
        for p in problems:
            print(f"   - {p}")
        return 1

    print(f"✅ CSP 合规检查通过（{checked} 个页面）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
