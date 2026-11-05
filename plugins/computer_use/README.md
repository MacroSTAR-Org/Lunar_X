# Computer Use（computer_use）

让机器人真正能干活：在工作空间里执行命令、读写文件，截屏并理解画面，
控制鼠标键盘。工具注册到 `bot.tools`，由 ai_chat 的 agent 循环
（function calling）调用；也有 `$截图` / `$电脑状态` 手动入口。

移植自 MaiBot 的 `xigua_computer-use-plugin`（同一作者），按 Lunar_X 的
工具注册表与配置约定重写。

## 能力清单

| 工具 | 档位 | 说明 |
|---|---|---|
| `run_command` | 一 | 工作空间内执行 Shell 命令（cwd=工作空间，超时+输出截断+黑名单） |
| `read_file` / `write_file` / `list_dir` | 一 | 工作空间内文件操作，路径越界一律拒绝 |
| `take_screenshot` | 二 | 截屏 → 发到聊天 → 画面回传给模型（"看屏幕"闭环） |
| `mouse_click` / `mouse_move` / `mouse_scroll` / `mouse_drag` | 二 | 鼠标控制 |
| `keyboard_type` / `keyboard_hotkey` | 二 | 键盘输入（中文走剪贴板粘贴） |
| `get_screen_info` | 二 | 屏幕分辨率与鼠标位置 |

## 怎么用起来

1. 装依赖：`uv sync --extra computer`
2. WebUI「插件管理 → ai_chat → 配置」里开启 **「启用工具调用（function calling）」**
   （模型需支持 function calling，deepseek-chat 支持）
3. 按需开启工具循环的轮数上限（默认 6 轮）
4. macOS 授权：系统设置 → 隐私与安全性 → 给运行进程授予
   「辅助功能」+「屏幕录制」权限
5. 在群里直接说："帮我看看屏幕" / "在工作空间里写个脚本跑一下"

## 权限模型（重要）

这是整个插件最关键的安全边界——Computer Use 等于把电脑交给提示词：

- **可用用户白名单** `computer.allowed_users`：留空 = 仅超级用户/管理员。
  普通群友只有进了白名单才能触发，双层校验（ai_chat 注册表层 + 本插件内层）。
- **工作空间锁定**：文件与命令都锁在 `workspace.root` 里，`../`、绝对路径、
  软链逃逸一律拒绝（除非显式开 `workspace.allow_outside`）。
- **危险命令黑名单**：`sudo`、`rm -rf /`、`shutdown`、`reboot`、`mkfs`、
  `dd if=`、fork 炸弹等片段直接拒绝（尽力而为的兜底，不是沙箱）。
- **全程审计**：每次工具调用都写 `[工具审计]` 日志（谁、在哪个群、调了什么、参数）。
- **紧急中止**：把鼠标甩到屏幕左上角（pyautogui FAILSAFE）；轮数/次数有硬上限。
- **按环境降级**：Linux 无桌面时 pyautogui 不可用，鼠标键盘工具优雅报错，
  文件与命令工具不受影响。

## 手动命令

| 命令 | 说明 |
|---|---|
| `$截图` | 立即截屏并发到当前聊天 |
| `$电脑状态` | 查看插件启用状态、工作空间、pyautogui 可用性 |

## 安全提醒

黑名单挡不住所有攻击（比如 `curl 恶意脚本 | sh`）。
真正兜底的是：默认仅管理员可用 + 工作空间隔离 + 审计日志。
把白名单开给不可信的人之前，先想清楚这台电脑上有什么。

## 依赖

`pyautogui`、`pillow`（`uv sync --extra computer`）。
截图/鼠标键盘失败时返回错误文字，不影响其余工具与对话。
