import unittest

from core.commands import parse_command


class ParseCommandTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_command("$ping", "$"), ("ping", ""))
        self.assertEqual(parse_command("$ai 你好 世界", "$"), ("ai", "你好 世界"))

    def test_unicode_whitespace(self):
        self.assertEqual(parse_command("$ai\u3000你好", "$"), ("ai", "你好"))
        self.assertEqual(parse_command("$定时  10分钟  喝水", "$"), ("定时", "10分钟  喝水"))

    def test_not_command(self):
        self.assertIsNone(parse_command("hello", "$"))
        self.assertIsNone(parse_command("$", "$"))
        self.assertIsNone(parse_command("$   ", "$"))
        self.assertIsNone(parse_command("", "$"))
        self.assertIsNone(parse_command("$help", ""))

    def test_custom_trigger(self):
        self.assertEqual(parse_command("/help now", "/"), ("help", "now"))
        self.assertIsNone(parse_command("$help", "/"))


if __name__ == "__main__":
    unittest.main()
