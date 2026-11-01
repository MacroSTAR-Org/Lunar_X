"""命令解析。

独立成纯函数，方便单测，也避免协议层和插件层各自实现一套规则。
"""

from __future__ import annotations


def parse_command(text: str, trigger: str) -> tuple[str, str] | None:
    """把 ``$帮助 foo`` 解析成 ``("帮助", "foo")``。

    - 触发词为空、消息不以触发词开头、或只剩触发词时返回 None；
    - 命令与参数之间使用任意 Unicode 空白分隔（含全角空格）。
    """
    if not isinstance(text, str) or not isinstance(trigger, str):
        return None
    if not trigger or not text.startswith(trigger):
        return None

    rest = text[len(trigger) :].strip()
    if not rest:
        return None

    parts = rest.split(None, 1)
    command = parts[0]
    args = parts[1].strip() if len(parts) > 1 else ""
    return command, args
