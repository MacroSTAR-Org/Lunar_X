"""Computer Use 工具实现：工作空间文件操作、命令执行、屏幕截取与鼠标键盘控制。

移植自 MaiBot 的 xigua_computer-use-plugin（同一作者），按 Lunar_X 的
插件约定改写：配置从 plugin.json schema 读取，handler 签名对齐
core/tools.py 的 ToolRegistry（_ctx=ToolCallContext, **模型参数）。

依赖 pyautogui（pyproject 的 computer extra）：
- macOS 需在「系统设置 → 隐私与安全性」为运行进程授予
  「辅助功能」与「屏幕录制」权限；
- 无桌面环境（Linux 服务器）时 GUI 工具会优雅报错，不影响文件/命令工具。
"""

from __future__ import annotations

import asyncio
import base64
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 工作空间与路径安全
# ---------------------------------------------------------------------------



def _get(cfg: dict, dotted: str, default: Any = None) -> Any:
    """读插件配置：plugin.json 的 "a.b" 键运行时是嵌套 dict，平铺回退兼容手写配置。"""
    cur: Any = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return cfg.get(dotted, default)
        cur = cur[part]
    return cur


def _get_int(cfg: dict, dotted: str, default: int, lo: int | None = None,
             hi: int | None = None) -> int:
    try:
        value = int(_get(cfg, dotted, default))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        value = max(lo, value)
    if hi is not None:
        value = min(hi, value)
    return value


def workspace_root(cfg: dict) -> Path:
    """解析工作空间根目录（默认 ~/computer_use_workspace，与参考插件一致）。"""
    root = Path(str(_get(cfg, "workspace.root") or "~/computer_use_workspace"))
    return root.expanduser().resolve()


def ensure_workspace(cfg: dict) -> Path:
    root = workspace_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_path(cfg: dict, path_str: str) -> tuple[Path | None, str]:
    """把传入路径解析到工作空间内；越界拒绝（除非显式开 allow_outside）。

    返回 (路径, 错误信息)，成功时错误信息为空。
    """
    raw = (path_str or "").strip() or "."
    root = workspace_root(cfg)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve()
    except OSError as exc:
        return None, f"路径解析失败: {exc}"

    # 允许路径本身等于根目录，或位于根目录之下；越界拒绝
    outside = resolved != root and root not in resolved.parents
    if outside and not _get(cfg, "workspace.allow_outside", False):
        return (
            None,
            f"拒绝访问：路径 {resolved} 不在工作空间 {root} 内"
            "（可在配置开启 allow_outside）",
        )
    return resolved, ""


def check_dangerous(cfg: dict, command: str) -> str:
    """命中禁止片段返回错误信息；安全返回空串。"""
    patterns = _get(cfg, "command.deny_patterns") or []
    for pattern in patterns:
        if pattern and pattern.lower() in command.lower():
            return f"命令被拒绝：命中禁止片段「{pattern}」"
    return ""


def _int_cfg(cfg: dict, key: str, default: int, lo: int, hi: int) -> int:
    return _get_int(cfg, key, default, lo, hi)


# ---------------------------------------------------------------------------
# pyautogui 懒加载
# ---------------------------------------------------------------------------


def _get_pyautogui():
    try:
        import pyautogui  # pyright: ignore[reportMissingModuleSource]
    except Exception as exc:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            "pyautogui 不可用。请安装 computer 依赖（uv sync --extra computer），"
            "且 macOS 需在「系统设置 → 隐私与安全性」为运行进程授予"
            "「辅助功能」和「屏幕录制」权限。"
        ) from exc
    pyautogui.FAILSAFE = True  # 鼠标甩到屏幕左上角可紧急中止
    pyautogui.PAUSE = 0.05
    return pyautogui


def _input_enabled(cfg: dict) -> str:
    if not _get(cfg, "input.enabled", True):
        return "鼠标键盘控制已在配置中禁用"
    return ""


def _screenshot_enabled(cfg: dict) -> str:
    if not _get(cfg, "screenshot.enabled", True):
        return "截图功能已在配置中禁用"
    return ""


# ---------------------------------------------------------------------------
# 同步执行体（供 asyncio.to_thread 调用）
# ---------------------------------------------------------------------------


def _run_shell_sync(command: str, cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, f"命令执行超时（>{timeout}s）"


def _capture_screen_sync(cfg: dict, save_path: Path) -> tuple[int, int]:
    pag = _get_pyautogui()
    shot = pag.screenshot()
    screen_w, screen_h = shot.size
    max_width = _int_cfg(cfg, "screenshot.max_width", 1280, 320, 3840)
    if screen_w > max_width:
        shot = shot.resize((max_width, int(screen_h * max_width / screen_w)))
    quality = _int_cfg(cfg, "screenshot.quality", 85, 30, 100)
    shot.convert("RGB").save(str(save_path), format="JPEG", quality=quality)
    return screen_w, screen_h


def _type_text_sync(text: str, interval: float) -> str:
    """非 ASCII（中文等）pyautogui 直接打不了：写剪贴板后粘贴。"""
    pag = _get_pyautogui()
    if all(ord(ch) < 128 for ch in text):
        pag.typewrite(text, interval=interval)
        return f"已直接键入 {len(text)} 个 ASCII 字符"
    if platform.system() == "Darwin":
        proc = subprocess.run(["pbcopy"], input=text, text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            return "写入剪贴板失败，无法输入非 ASCII 文本"
        time.sleep(0.1)
        pag.hotkey("command", "v")
        return f"已通过剪贴板粘贴 {len(text)} 个字符（含非 ASCII）"
    return "当前平台暂不支持输入非 ASCII 文本，请只发送 ASCII 内容"


# ---------------------------------------------------------------------------
# 工具实现（对齐 ToolRegistry 的 handler 约定）
# ---------------------------------------------------------------------------


def _cfg_of(bot) -> dict:
    return bot.plugin_config("computer_use")


def _output_cap(cfg: dict, text: str) -> str:
    max_chars = _int_cfg(cfg, "command.max_output_chars", 4000, 200, 20000)
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...（内容过长已截断，共 {len(text)} 字符）"
    return text


async def tool_run_command(bot, cfg, _ctx, command: str = "", timeout: int | None = None) -> str:
    command = (command or "").strip()
    if not command:
        return "命令不能为空"
    if danger := check_dangerous(cfg, command):
        return danger

    run_timeout = timeout if isinstance(timeout, int) and timeout > 0 else _int_cfg(
        cfg, "command.timeout", 60, 1, 600
    )
    cwd = ensure_workspace(cfg)
    returncode, output = await asyncio.to_thread(_run_shell_sync, command, cwd, run_timeout)
    output = _output_cap(cfg, output.strip() or "(无输出)")
    return f"$ {command}\n[退出码 {returncode}]（cwd={cwd}）\n{output}"


async def tool_read_file(bot, cfg, _ctx, path: str = "") -> str:
    resolved, err = resolve_path(cfg, path)
    if err:
        return err
    assert resolved is not None  # err 为空时 resolved 必有值，供类型收窄
    if not resolved.is_file():
        return f"文件不存在或不是普通文件: {resolved}"
    try:
        text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"读取失败: {exc}"
    return f"--- {resolved} ---\n{_output_cap(cfg, text)}"


async def tool_write_file(bot, cfg, _ctx, path: str = "", content: str = "", append: bool = False) -> str:
    resolved, err = resolve_path(cfg, path)
    if err:
        return err
    assert resolved is not None
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with open(resolved, "a" if append else "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return f"写入失败: {exc}"
    action = "追加" if append else "写入"
    return f"已{action} {len(content)} 字符到 {resolved}"


async def tool_list_dir(bot, cfg, _ctx, path: str = "") -> str:
    resolved, err = resolve_path(cfg, path)
    if err:
        return err
    assert resolved is not None
    if not resolved.is_dir():
        return f"目录不存在: {resolved}"
    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name))
    except OSError as exc:
        return f"读取目录失败: {exc}"
    lines = [f"{'[dir] ' if p.is_dir() else ''}{p.name}" for p in entries[:200]]
    if len(entries) > 200:
        lines.append(f"...（共 {len(entries)} 项，仅显示前 200 项）")
    return f"{resolved}\n" + ("\n".join(lines) if lines else "(空目录)")


async def tool_take_screenshot(bot, cfg, _ctx: Any, note: str = "") -> str:
    """截屏：图片发送到当前聊天，data URL 放进 _ctx.images 供模型看屏幕。"""
    if err := _screenshot_enabled(cfg):
        return err
    data_dir = Path(bot.plugin_data_dir("computer_use")) / "screenshots"
    data_dir.mkdir(parents=True, exist_ok=True)
    save_path = data_dir / f"screenshot_{int(time.time() * 1000)}.jpg"

    try:
        screen_w, screen_h = await asyncio.to_thread(_capture_screen_sync, cfg, save_path)
    except Exception as exc:
        return f"截图失败: {exc}"

    data = save_path.read_bytes()
    if _ctx is not None and hasattr(_ctx, "images"):
        _ctx.images.append(
            f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
        )

    # 把图片发到聊天里（本地绝对路径，ImageSegment 支持）
    assert _ctx is not None  # 手动命令/agent 循环都会传上下文
    try:
        await bot.send(bot.msg.image(str(save_path)), group_id=_ctx.group_id, user_id=_ctx.user_id)
    except Exception as exc:
        return f"截图已保存到 {save_path}，但发送失败: {exc}"

    caption = (note or "").strip()
    text = f"截图已发送到聊天（屏幕 {screen_w}x{screen_h}，文件 {save_path.name}）。"
    if caption:
        text = f"{caption}\n{text}"
    return text


async def tool_get_screen_info(bot, cfg, _ctx) -> str:
    if err := _input_enabled(cfg):
        return err
    try:
        pag = await asyncio.to_thread(_get_pyautogui)
        size = await asyncio.to_thread(pag.size)
        pos = await asyncio.to_thread(pag.position)
    except Exception as exc:
        return f"获取屏幕信息失败: {exc}"
    return f"屏幕分辨率: {size.width}x{size.height}，鼠标位置: ({pos.x}, {pos.y})"


async def tool_mouse_click(
    bot, cfg, _ctx, x: int | None = None, y: int | None = None,
    button: str = "left", clicks: int = 1,
) -> str:
    if err := _input_enabled(cfg):
        return err
    if button not in ("left", "right", "middle"):
        return f"不支持的鼠标键: {button}"
    if clicks not in (1, 2, 3):
        return f"不支持的点击次数: {clicks}"
    try:
        pag = await asyncio.to_thread(_get_pyautogui)
        if x is not None and y is not None:
            duration = _int_cfg(cfg, "input.move_duration", 25, 0, 300) / 100
            await asyncio.to_thread(pag.moveTo, int(x), int(y), duration)
        await asyncio.to_thread(pag.click, button=button, clicks=clicks)
    except Exception as exc:
        return f"鼠标点击失败: {exc}"
    pos_desc = f"({x}, {y})" if x is not None and y is not None else "当前位置"
    return f"已在 {pos_desc} 用 {button} 键点击 {clicks} 次"


async def tool_mouse_move(bot, cfg, _ctx, x: int = 0, y: int = 0) -> str:
    if err := _input_enabled(cfg):
        return err
    try:
        pag = await asyncio.to_thread(_get_pyautogui)
        duration = _int_cfg(cfg, "input.move_duration", 25, 0, 300) / 100
        await asyncio.to_thread(pag.moveTo, int(x), int(y), duration)
        pos = await asyncio.to_thread(pag.position)
    except Exception as exc:
        return f"鼠标移动失败: {exc}"
    return f"鼠标已移动到 ({pos.x}, {pos.y})"


async def tool_mouse_scroll(bot, cfg, _ctx, direction: str = "down", amount: int = 3) -> str:
    if err := _input_enabled(cfg):
        return err
    if direction not in ("up", "down"):
        return f"不支持的方向: {direction}（只支持 up/down）"
    if amount <= 0:
        return "滚动格数必须为正整数"
    try:
        pag = await asyncio.to_thread(_get_pyautogui)
        await asyncio.to_thread(pag.scroll, amount if direction == "up" else -amount)
    except Exception as exc:
        return f"滚动失败: {exc}"
    return f"已向{'上' if direction == 'up' else '下'}滚动 {amount} 格"


async def tool_mouse_drag(bot, cfg, _ctx, x: int = 0, y: int = 0) -> str:
    if err := _input_enabled(cfg):
        return err
    try:
        pag = await asyncio.to_thread(_get_pyautogui)
        duration = max(_int_cfg(cfg, "input.move_duration", 25, 0, 300) / 100, 0.3)
        await asyncio.to_thread(pag.dragTo, int(x), int(y), duration, button="left")
    except Exception as exc:
        return f"拖拽失败: {exc}"
    return f"已拖拽到 ({x}, {y})"


async def tool_keyboard_type(bot, cfg, _ctx, text: str = "") -> str:
    if err := _input_enabled(cfg):
        return err
    if not text:
        return "文本不能为空"
    try:
        interval = _int_cfg(cfg, "input.type_interval", 20, 0, 1000) / 100
        return await asyncio.to_thread(_type_text_sync, text, interval)
    except Exception as exc:
        return f"输入失败: {exc}"


async def tool_keyboard_hotkey(bot, cfg, _ctx, keys: str = "") -> str:
    if err := _input_enabled(cfg):
        return err
    key_list = [k.strip().lower() for k in (keys or "").split("+") if k.strip()]
    if not key_list:
        return "组合键不能为空，例如 ctrl+c"
    try:
        pag = await asyncio.to_thread(_get_pyautogui)
        await asyncio.to_thread(pag.hotkey, *key_list)
    except Exception as exc:
        return f"组合键失败: {exc}"
    return f"已按下组合键 {'+'.join(key_list)}"


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------


def user_allowed(bot, cfg, user_id: int) -> bool:
    """双层权限：computer_use.allowed_users 白名单（空=仅管理员）。

    管理员永远放行；普通用户只有在白名单里才放行。
    """
    if bot._check_permission(user_id, "manager"):
        return True
    allowed = _get(cfg, "computer.allowed_users") or []
    return user_id in allowed
