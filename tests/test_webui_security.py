"""WebUI 安全响应头与 CSP 的回归测试。

这些用例守的是"加了安全头但把页面搞挂"这类事故：
CSP 一旦写错（比如漏了 nonce），前端会直接白屏，
而这种问题在开发时（localhost、无 CSP）根本看不出来。
"""

import re
import unittest

import webui
from webui import app


class SecurityHeaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_basic_headers_present(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("camera=()", resp.headers.get("Permissions-Policy", ""))

    def test_static_assets_cached(self):
        with app.test_client() as client:
            resp = client.get("/static/js/api.js")
            self.assertEqual(resp.status_code, 200)
            self.assertIn("max-age=300", resp.headers.get("Cache-Control", ""))

    def test_html_not_cached(self):
        resp = self.client.get("/login")
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))


class HealthcheckTests(unittest.TestCase):
    def test_healthz_is_public(self):
        """容器/反代探针不能要求登录会话。"""
        app.config["TESTING"] = True
        with app.test_client() as client:
            resp = client.get("/healthz")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json()["status"], "ok")
            self.assertTrue(resp.get_json().get("version"))


class ContentSecurityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def _csp(self, path: str = "/login") -> str:
        resp = self.client.get(path)
        self.assertEqual(resp.status_code, 200)
        csp = resp.headers.get("Content-Security-Policy")
        self.assertIsNotNone(csp, "HTML 响应必须带 CSP")
        return csp

    def test_csp_present_on_html(self):
        csp = self._csp()
        self.assertIn("default-src 'self'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("connect-src 'self'", csp)

    def test_no_unsafe_inline_for_script(self):
        """script-src 不能有 unsafe-inline，inline script 靠 nonce 放行。"""
        csp = self._csp()
        script_src = re.search(r"script-src ([^;]+);", csp)
        self.assertIsNotNone(script_src)
        self.assertNotIn("'unsafe-inline'", script_src.group(1))

    def test_unsafe_eval_allowed_only_for_vue_runtime(self):
        """Vue 3 全局版运行时用 Function() 编译模板，必须留 unsafe-eval。"""
        csp = self._csp()
        script_src = re.search(r"script-src ([^;]+);", csp)
        self.assertIn("'unsafe-eval'", script_src.group(1))

    def test_nonce_matches_template(self):
        """CSP 里的 nonce 必须和 HTML 里 inline script 的 nonce 一致，否则页面白屏。"""
        resp = self.client.get("/login")
        csp = resp.headers.get("Content-Security-Policy", "")
        html = resp.get_data(as_text=True)

        csp_nonce = re.search(r"'nonce-([A-Za-z0-9_-]+)'", csp)
        self.assertIsNotNone(csp_nonce, "CSP 必须含 nonce")
        html_nonces = set(re.findall(r'<script nonce="([^"]+)"', html))
        self.assertTrue(html_nonces, "页面里的 inline script 必须带 nonce 属性")
        self.assertIn(csp_nonce.group(1), html_nonces)

    def test_nonce_differs_between_requests(self):
        """nonce 必须每请求随机，否则可被复用绕过。"""
        first = self.client.get("/login").headers.get("Content-Security-Policy", "")
        second = self.client.get("/login").headers.get("Content-Security-Policy", "")
        self.assertNotEqual(first, second)

    def test_upgrade_insecure_requests_only_when_secure(self):
        """明文访问时不能加 upgrade-insecure-requests，否则局域网 HTTP 会白屏。"""
        plain = self.client.get("/login").headers.get("Content-Security-Policy", "")
        self.assertNotIn("upgrade-insecure-requests", plain)

        secure = self.client.get(
            "/login", base_url="https://lunarx.example.com", headers={"X-Forwarded-Proto": "https"}
        )
        csp = secure.headers.get("Content-Security-Policy", "")
        self.assertIn("upgrade-insecure-requests", csp)

    def test_csp_not_applied_to_json(self):
        """JSON / 静态资源加 CSP 没有意义，也不该让浏览器误处理。"""
        resp = self.client.get("/api/auth_status")
        self.assertNotIn("Content-Security-Policy", resp.headers)


class WebuiConfigExposureTests(unittest.TestCase):
    """敏感信息不能经接口回流到浏览器。"""

    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_webui_settings_hide_secrets(self):
        resp = self.client.get("/api/webui/settings")
        # 未登录时会被 401/403 拦下，不会泄露内容
        if resp.status_code == 200:
            body = resp.get_json()
            for key in ("password_hash", "session_secret", "github_pat"):
                self.assertNotIn(key, body)

    def test_login_page_has_no_secret_in_html(self):
        resp = self.client.get("/login")
        html = resp.get_data(as_text=True)
        self.assertNotIn("password_hash", html)
        self.assertNotIn("session_secret", html)


class CsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_write_without_csrf_rejected(self):
        resp = self.client.post("/api/change_password", json={"old_password": "x"})
        self.assertIn(resp.status_code, (401, 403))


class ConfigMergeTests(unittest.TestCase):
    """配置保存必须是"合并"而不是"整文件替换"。

    整文件替换的后果：任何一次只带部分字段的写入（脚本调用、
    字段漏传、老版本前端）都会把没传的键直接从磁盘上抹掉——
    protocol / milky_server / ws_server 一旦丢失，机器人下次启动就连不上协议端。
    """

    def test_partial_write_keeps_other_keys(self):
        existing = {
            "protocol": "milky",
            "milky_server": "http://127.0.0.1:3010",
            "ws_server": "ws://127.0.0.1:3803",
            "token": "real-token",
            "milky_token": "real-milky-token",
            "log_level": "INFO",
        }
        merged = webui._merge_config_secrets({"log_level": "DEBUG"}, existing)

        self.assertEqual(merged["log_level"], "DEBUG")
        # 没传的键必须还在
        self.assertEqual(merged["protocol"], "milky")
        self.assertEqual(merged["milky_server"], "http://127.0.0.1:3010")
        self.assertEqual(merged["ws_server"], "ws://127.0.0.1:3803")

    def test_placeholder_restores_original_secret(self):
        existing = {"token": "real-token", "milky_token": "real-milky"}
        merged = webui._merge_config_secrets(
            {"token": webui.SECRET_PLACEHOLDER, "milky_token": webui.SECRET_PLACEHOLDER},
            existing,
        )
        self.assertEqual(merged["token"], "real-token")
        self.assertEqual(merged["milky_token"], "real-milky")

    def test_placeholder_does_not_leak_into_new_key(self):
        """占位符出现在没有原值的键上时，不该被写进磁盘。"""
        merged = webui._merge_config_secrets({"token": webui.SECRET_PLACEHOLDER}, {})
        self.assertEqual(merged["token"], "")

    def test_empty_write_keeps_everything(self):
        existing = {"protocol": "milky", "log_level": "INFO"}
        merged = webui._merge_config_secrets({}, existing)
        self.assertEqual(merged, existing)

    def test_appsettings_partial_write_keeps_other_fields(self):
        existing = {
            "Account": {"Uin": 123, "Protocol": "Linux"},
            "Implementations": [
                {"Host": "127.0.0.1", "Port": 3803, "AccessToken": "real", "Type": "ForwardWebSocket"}
            ],
            "SignServerUrl": "https://example.com/sign",
        }
        merged = webui._merge_appsettings_secrets(
            {"Implementations": [{"AccessToken": webui.SECRET_PLACEHOLDER}]}, existing
        )

        impl = merged["Implementations"][0]
        # 只传了 AccessToken，Host / Port / Type 不能丢
        self.assertEqual(impl["Host"], "127.0.0.1")
        self.assertEqual(impl["Port"], 3803)
        self.assertEqual(impl["Type"], "ForwardWebSocket")
        self.assertEqual(impl["AccessToken"], "real")
        # 其它顶层键也不能丢
        self.assertEqual(merged["Account"]["Uin"], 123)
        self.assertEqual(merged["SignServerUrl"], "https://example.com/sign")

    def test_appsettings_partial_write_updates_given_field(self):
        existing = {
            "Implementations": [{"Host": "127.0.0.1", "Port": 3803, "AccessToken": "real"}]
        }
        merged = webui._merge_appsettings_secrets(
            {"Implementations": [{"Port": 9999}]}, existing
        )
        self.assertEqual(merged["Implementations"][0]["Port"], 9999)
        self.assertEqual(merged["Implementations"][0]["Host"], "127.0.0.1")


class SecurityModuleImportTests(unittest.TestCase):
    """core.security 必须保持零依赖，WebUI 才不会为了一个函数拉起 bot。"""

    def test_security_has_no_flask_import(self):
        import core.security as sec

        with open(sec.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("import flask", source)
        self.assertNotIn("from flask", source)

    def test_webui_module_exposes_helpers(self):
        self.assertTrue(callable(webui.is_safe_plugin_name))


if __name__ == "__main__":
    unittest.main()
