"""图片理解的单元测试：魔数嗅探、base64 解码、大小限制、URL 提取、多图截断。

不发真实网络请求：下载路径通过 base64:// 与本地 file:// 覆盖，
_download 的失败分支用假 URL（非法 scheme / 不可达地址不会真的发请求——
嗯，会发，所以这里只测 scheme 拒绝这类纯逻辑分支）。
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_PLUGIN = Path(__file__).resolve().parent.parent / "plugins" / "ai_chat"
sys.path.insert(0, str(_PLUGIN))

import vision


def _image_seg(file_value: str):
    return SimpleNamespace(type="image", data={"file": file_value})


class MimeSniffTests(unittest.TestCase):
    def test_jpeg(self):
        url = vision.to_data_url(b"\xff\xd8\xffEXIF")
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))

    def test_png(self):
        url = vision.to_data_url(b"\x89PNG\r\n\x1a\n" + b"x")
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_gif(self):
        url = vision.to_data_url(b"GIF89a" + b"x")
        self.assertTrue(url.startswith("data:image/gif;base64,"))

    def test_unknown_defaults_to_jpeg(self):
        url = vision.to_data_url(b"\x00\x00\x00")
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))

    def test_payload_is_roundtrip(self):
        data = b"\x89PNG\r\n\x1a\n" + b"payload"
        url = vision.to_data_url(data)
        import base64

        encoded = url.split(",", 1)[1]
        self.assertEqual(base64.b64decode(encoded), data)


class DecodeBase64Tests(unittest.TestCase):
    def test_valid(self):
        import base64

        raw = b"hello image"
        self.assertEqual(
            vision._decode_base64(base64.b64encode(raw).decode(), 1024), raw
        )

    def test_base64_prefix_stripped_by_caller(self):
        """resolve_image 负责剥 base64:// 前缀，_decode_base64 只管纯数据。"""
        import base64

        raw = b"data"
        self.assertEqual(
            vision._decode_base64(base64.b64encode(raw).decode(), 1024), raw
        )

    def test_invalid_returns_none(self):
        self.assertIsNone(vision._decode_base64("!!!不是base64!!!", 1024))

    def test_oversize_returns_none(self):
        import base64

        big = base64.b64encode(b"x" * 2048).decode()
        self.assertIsNone(vision._decode_base64(big, 1024))


class LocalFileTests(unittest.TestCase):
    def test_read_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"img")
            data = vision._read_local(str(path), 1024)
            self.assertIsNotNone(data)
            self.assertTrue(data.startswith(b"\x89PNG"))

    def test_missing_file(self):
        self.assertIsNone(vision._read_local("/nonexistent/a.png", 1024))

    def test_oversize_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.bin"
            path.write_bytes(b"x" * 2048)
            self.assertIsNone(vision._read_local(str(path), 1024))


class ExtractUrlTests(unittest.TestCase):
    def test_onebot_style(self):
        self.assertEqual(
            vision._extract_url({"data": {"file": "abc.png", "url": "https://x/y.png"}}),
            "https://x/y.png",
        )

    def test_milky_temp_url(self):
        self.assertEqual(
            vision._extract_url({"temp_url": "https://t/x"}), "https://t/x"
        )

    def test_top_level_dict(self):
        self.assertEqual(vision._extract_url({"url": "https://a/b"}), "https://a/b")

    def test_base64_field(self):
        self.assertEqual(vision._extract_url({"base64": "aGk="}), "aGk=")

    def test_nothing_usable(self):
        self.assertIsNone(vision._extract_url({"data": {}}))
        self.assertIsNone(vision._extract_url("not a dict"))


class CollectSegmentsTests(unittest.TestCase):
    def test_only_images_in_order(self):
        segs = [
            SimpleNamespace(type="text", data={}),
            _image_seg("base64://a"),
            SimpleNamespace(type="at", data={"qq": 1}),
            _image_seg("base64://b"),
        ]
        result = vision.collect_image_segments(segs)
        self.assertEqual(len(result), 2)


class BuildImagePartsTests(unittest.TestCase):
    def test_base64_flow(self):
        import base64

        payload = b"\x89PNG\r\n\x1a\n" + b"pic"
        segs = [_image_seg("base64://" + base64.b64encode(payload).decode())]
        event = SimpleNamespace(message=segs)
        cfg = {"vision.max_images": 2, "vision.max_image_mb": 5}

        import asyncio

        parts = asyncio.run(vision.build_image_parts(None, None, event, cfg))
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["type"], "image_url")
        self.assertTrue(parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_max_images_respected(self):
        import base64

        payload = base64.b64encode(b"\xff\xd8\xffimg").decode()
        segs = [_image_seg(f"base64://{payload}") for _ in range(5)]
        event = SimpleNamespace(message=segs)
        cfg = {"vision.max_images": 2, "vision.max_image_mb": 5}

        import asyncio

        parts = asyncio.run(vision.build_image_parts(None, None, event, cfg))
        self.assertEqual(len(parts), 2)

    def test_bad_image_skipped(self):
        segs = [_image_seg("base64://!!!invalid!!!"), _image_seg("")]
        event = SimpleNamespace(message=segs)
        cfg = {}

        import asyncio

        parts = asyncio.run(vision.build_image_parts(None, None, event, cfg))
        self.assertEqual(parts, [])

    def test_oversize_base64_skipped(self):
        import base64

        payload = base64.b64encode(b"x" * (6 * 1024 * 1024)).decode()
        event = SimpleNamespace(message=[_image_seg(f"base64://{payload}")])
        cfg = {"vision.max_image_mb": 5}

        import asyncio

        parts = asyncio.run(vision.build_image_parts(None, None, event, cfg))
        self.assertEqual(parts, [])


class ResolveDispatchTests(unittest.TestCase):
    def test_rejects_non_http_download(self):
        """下载只放行 http(s)，ftp 等形态的 URL 不进下载器（不发任何请求）。"""
        import asyncio

        result = asyncio.run(vision._download(None, "ftp://example.com/a.png", 1024))
        self.assertIsNone(result)

    def test_rejects_empty(self):
        import asyncio

        self.assertIsNone(asyncio.run(vision.resolve_image(None, None, "", 1024)))


if __name__ == "__main__":
    unittest.main()
