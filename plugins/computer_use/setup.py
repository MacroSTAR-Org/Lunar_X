"""Computer Use 插件：让机器人具备操作电脑的能力。

能力来源参考了 MaiBot 的 xigua_computer-use-plugin（同一作者），按 Lunar_X 的
工具注册表（core/tools.py）重新接线：

- 工具在框架启动时注册到 bot.tools，供 ai_chat 的 agent 循环做 function calling；
- 权限双层：注册表不做限制，本插件用 computer.allowed_users（空=仅管理员）把关；
- 鼠标键盘/截图是"第二档"能力，input.enabled / screenshot.enabled 单独开关，
  无桌面环境时 pyautogui 不可用会优雅报错（按环境自动降级）。

手动命令：$截图 / $电脑状态
"""

TRIGGHT_KEYWORD = "Any"
PLT_ST = 20
HELP_MESSAGE = "Computer Use：$截图 立即截屏，$电脑状态 查看能力状态（agent 对话中自动可用工具）"

from typing import Any

import computer

from core.events import MessageEvent
from core.tools import ToolCallContext

_OWNER = "computer_use"

# 注册到注册表时的超时（秒）：命令执行可能要装依赖，给宽一些
_TOOL_TIMEOUTS = {
    "run_command": 180,
    "take_screenshot": 30,
    "keyboard_type": 30,
}


def _cfg(bot) -> dict:
    return bot.plugin_config(_OWNER)


def _guard(fn):
    """工具包装：插件开关 + 用户白名单，双关都过才执行。"""

    async def _handler(_ctx: ToolCallContext, bot=None, **kwargs) -> str:
        if bot is None:
            return "内部错误：缺少 bot 引用"
        cfg = _cfg(bot)
        if not cfg.get("enabled", True):
            return "Computer Use 插件已在配置中禁用"
        if not computer.user_allowed(bot, cfg, _ctx.user_id):
            return (
                f"权限不足：Computer Use 仅管理员或白名单用户可用"
                f"（发起者 {_ctx.user_id}）"
            )
        return await fn(bot, cfg, _ctx, **kwargs)

    _handler.__name__ = f"guarded_{fn.__name__}"
    return _handler


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or []}


def _register_tools(bot) -> None:
    """把 12 个工具注册到 bot.tools。"""
    register = bot.tools.register

    register(
        "run_command",
        "在电脑工作空间执行 Shell 命令。用户要求『执行命令』『运行代码』"
        "『帮我装/查/处理』时使用",
        _schema(
            {
                "command": {"type": "string", "description": "要执行的 Shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 60"},
            },
            ["command"],
        ),
        _guard(computer.tool_run_command),
        owner=_OWNER,
        timeout=_TOOL_TIMEOUTS["run_command"],
    )
    register(
        "read_file",
        "读取电脑工作空间内文件的内容。用户想查看某个文件或代码时使用",
        _schema({"path": {"type": "string", "description": "文件路径（相对工作空间）"}}, ["path"]),
        _guard(computer.tool_read_file),
        owner=_OWNER,
    )
    register(
        "write_file",
        "把文本写入电脑工作空间内的文件（写代码、新建或修改文件）。",
        _schema(
            {
                "path": {"type": "string", "description": "目标文件路径（相对工作空间）"},
                "content": {"type": "string", "description": "要写入的文本内容"},
                "append": {"type": "boolean", "description": "是否追加而非覆盖，默认覆盖"},
            },
            ["path", "content"],
        ),
        _guard(computer.tool_write_file),
        owner=_OWNER,
    )
    register(
        "list_dir",
        "列出电脑工作空间内某个目录的文件和子目录。",
        _schema({"path": {"type": "string", "description": "目录路径，默认工作空间根目录"}}),
        _guard(computer.tool_list_dir),
        owner=_OWNER,
    )
    register(
        "take_screenshot",
        "截取电脑屏幕：图片会发送到当前聊天，同时画面内容回传给你（你能看到屏幕）。"
        "用户提到『截图』『看看我屏幕』必须使用本工具，绝对不要回答『无法操作你的电脑』。"
        "截图后请继续观察画面并回应，或继续鼠标键盘操作。",
        _schema({"note": {"type": "string", "description": "随截图附带的备注文字，可为空"}}),
        _guard(computer.tool_take_screenshot),
        owner=_OWNER,
        timeout=_TOOL_TIMEOUTS["take_screenshot"],
    )
    register(
        "get_screen_info",
        "获取电脑屏幕分辨率和鼠标当前位置。鼠标操作前用来确定坐标范围。",
        _schema({}),
        _guard(computer.tool_get_screen_info),
        owner=_OWNER,
    )
    register(
        "mouse_click",
        "控制电脑鼠标点击：单击/双击/右键，可指定屏幕坐标。",
        _schema(
            {
                "x": {"type": "integer", "description": "目标 X 坐标，不填为当前位置"},
                "y": {"type": "integer", "description": "目标 Y 坐标，不填为当前位置"},
                "button": {"type": "string", "description": "left/right/middle，默认 left"},
                "clicks": {"type": "integer", "description": "点击次数：1 单击、2 双击"},
            }
        ),
        _guard(computer.tool_mouse_click),
        owner=_OWNER,
    )
    register(
        "mouse_move",
        "把电脑鼠标移动到屏幕指定坐标。",
        _schema(
            {
                "x": {"type": "integer", "description": "目标 X 坐标"},
                "y": {"type": "integer", "description": "目标 Y 坐标"},
            },
            ["x", "y"],
        ),
        _guard(computer.tool_mouse_move),
        owner=_OWNER,
    )
    register(
        "mouse_scroll",
        "滚动电脑鼠标滚轮（向上/向下翻页）。",
        _schema(
            {
                "direction": {"type": "string", "description": "up/down，默认 down"},
                "amount": {"type": "integer", "description": "滚动格数，默认 3"},
            }
        ),
        _guard(computer.tool_mouse_scroll),
        owner=_OWNER,
    )
    register(
        "mouse_drag",
        "按住鼠标左键从当前位置拖拽到指定坐标（拖动文件、滑动选择等）。",
        _schema(
            {
                "x": {"type": "integer", "description": "拖拽目标 X 坐标"},
                "y": {"type": "integer", "description": "拖拽目标 Y 坐标"},
            },
            ["x", "y"],
        ),
        _guard(computer.tool_mouse_drag),
        owner=_OWNER,
    )
    register(
        "keyboard_type",
        "在电脑当前焦点位置输入一段文本（支持中文，走剪贴板粘贴）。",
        _schema({"text": {"type": "string", "description": "要输入的文本"}}, ["text"]),
        _guard(computer.tool_keyboard_type),
        owner=_OWNER,
        timeout=_TOOL_TIMEOUTS["keyboard_type"],
    )
    register(
        "keyboard_hotkey",
        "在电脑上按下组合键，如 ctrl+c、command+v、alt+tab、enter、esc。",
        _schema({"keys": {"type": "string", "description": "组合键，用 + 连接，如 ctrl+c"}},
                ["keys"]),
        _guard(computer.tool_keyboard_hotkey),
        owner=_OWNER,
    )


async def on_lunar_event(event, bot):
    """框架启动：确保工作空间存在并注册工具。"""
    if type(event).__name__ != "LunarStartListen":
        return
    computer.ensure_workspace(_cfg(bot))
    _register_tools(bot)
    bot.plugin_logger.info(
        f"Computer Use 工具已注册（{len(bot.tools)} 个可用），"
        f"工作空间: {computer.workspace_root(_cfg(bot))}"
    )


async def on_unload(bot):
    """注销本插件注册的全部工具。"""
    removed = bot.tools.unregister_owner(_OWNER)
    if removed:
        bot.plugin_logger.info(f"Computer Use 已注销 {removed} 个工具")


async def on_message(event, bot):
    """手动命令：$截图 / $电脑状态（agent 对话之外的低门槛入口）。"""
    if not isinstance(event, MessageEvent):
        return False
    if not (event.is_command and event.command in ("截图", "电脑状态")):
        return False

    cfg = _cfg(bot)
    if not cfg.get("enabled", True):
        return False
    if not computer.user_allowed(bot, cfg, int(event.user_id or 0)):
        await bot.send("仅管理员或白名单用户可使用 Computer Use", user_id=event.user_id,
                       group_id=getattr(event, "group_id", None))
        return True

    group_id = getattr(event, "group_id", None)
    ctx = ToolCallContext(user_id=int(event.user_id or 0), group_id=group_id)

    if event.command == "截图":
        result = await computer.tool_take_screenshot(bot, cfg, ctx, note="🖥 当前屏幕截图")
        if "截图已发送" not in result:
            await bot.send(f"截图失败：{result}", group_id=group_id, user_id=event.user_id)
        return True

    # $电脑状态
    status: dict[str, str] = {
        "插件": "启用" if cfg.get("enabled", True) else "禁用",
        "工作空间": str(computer.workspace_root(cfg)),
        "鼠标键盘": "启用" if ctx_get(cfg, "input.enabled", True) else "禁用",
        "截图": "启用" if ctx_get(cfg, "screenshot.enabled", True) else "禁用",
        "已注册工具": f"{len(bot.tools)} 个",
    }
    try:
        import asyncio

        pag = await asyncio.to_thread(computer._get_pyautogui)
        size = await asyncio.to_thread(pag.size)
        status["pyautogui"] = f"可用（屏幕 {size.width}x{size.height}）"
    except RuntimeError as exc:
        status["pyautogui"] = f"不可用（{str(exc)[:40]}…）"

    lines = ["🖥 Computer Use 状态"] + [f"- {k}: {v}" for k, v in status.items()]
    await bot.send("\n".join(lines), group_id=group_id, user_id=event.user_id)
    return True


def ctx_get(cfg: dict, dotted: str, default=None):
    """computer.py 里的嵌套配置读取助手（本文件不便跨插件 import）。"""
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return cfg.get(dotted, default)
        cur = cur[part]
    return cur
