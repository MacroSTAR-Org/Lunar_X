"""插件工具注册表：让插件向 LLM 暴露可调用的工具（function calling）。

设计：
- 插件在 LunarStartListen 时调用 bot.register_tool(...) 注册，
  on_unload 时按 owner 注销——热重载自动清场，不会留下僵尸工具。
- ai_chat 的 agent 循环枚举 bot.tools 拼 tools=[...]，模型决定调哪个。
- 权限是双层的：注册表级 admin_only 挡住越权调用（不执行），
  工具实现内部再做自己的检查（如 computer_use 的用户白名单）。
- 每次执行都写审计日志：谁、在哪个群、调了什么、参数、结果。
  Computer Use 类工具等于"提示词注入 → 远程执行"，审计是底线。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# 单个工具的默认执行上限；插件注册时可放宽
DEFAULT_TOOL_TIMEOUT = 30


@dataclass
class ToolCallContext:
    """一次工具调用的发起者信息，handler 用它做自己的权限判断。

    images 是回传给 LLM 的通道：截图类工具往里放 data URL，
    agent 循环在工具结果之后把它们作为图片消息注入——
    这是"截屏 → 模型看屏幕 → 决定下一步操作"闭环的关键。
    """

    user_id: int
    group_id: int | None = None
    images: list[str] = field(default_factory=list)


@dataclass
class ToolSpec:
    """一个已注册工具的完整描述。parameters 是 JSON Schema 的 parameters 对象。"""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[str]]
    owner: str
    admin_only: bool = False
    timeout: int = DEFAULT_TOOL_TIMEOUT
    # 运行时统计，WebUI 之后可以直接展示
    calls: int = field(default=0, init=False)


class ToolRegistry:
    """bot.tools 持有的注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    # -- 注册与注销 ---------------------------------------------------------

    def register(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any] | None,
        handler: Callable[..., Awaitable[str]],
        owner: str,
        *,
        admin_only: bool = False,
        timeout: int = DEFAULT_TOOL_TIMEOUT,
    ) -> None:
        if not name or not description:
            raise ValueError("工具必须有 name 和 description")
        if name in self._tools and self._tools[name].owner != owner:
            raise ValueError(f"工具名 {name!r} 已被插件 {self._tools[name].owner!r} 注册")
        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            handler=handler,
            owner=owner,
            admin_only=admin_only,
            timeout=max(1, min(int(timeout), 600)),
        )

    def unregister_owner(self, owner: str) -> int:
        """注销某插件的全部工具（on_unload 时调用），返回注销数量。"""
        names = [n for n, spec in self._tools.items() if spec.owner == owner]
        for name in names:
            del self._tools[name]
        return len(names)

    # -- 查询 ---------------------------------------------------------------

    def specs(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def openapi_schemas(self) -> list[dict[str, Any]]:
        """拼成 OpenAI function calling 的 tools 数组。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            }
            for spec in self._tools.values()
        ]

    def __len__(self) -> int:
        return len(self._tools)

    # -- 执行 ---------------------------------------------------------------

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        caller: ToolCallContext,
        *,
        is_admin: bool,
        log: Any | None = None,
    ) -> str:
        """执行一个工具调用，永远返回字符串（错误也转成文字给模型兜底）。

        权限、异常、超时都在这里兜底，handler 只管干活。
        """
        spec = self._tools.get(name)
        if spec is None:
            return f"未知工具: {name}"

        args_desc = repr(arguments)[:200]
        if spec.admin_only and not is_admin:
            self._audit(log, caller, spec, arguments, "拒绝（权限不足）")
            return f"权限不足：工具 {name} 仅管理员可用（发起者 {caller.user_id}）"

        spec.calls += 1
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                spec.handler(_ctx=caller, **arguments), timeout=spec.timeout
            )
        except TimeoutError:
            result = f"工具 {name} 执行超时（>{spec.timeout}s）"
        except TypeError as e:
            result = f"参数不匹配: {e}"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # 工具崩溃不能拖垮对话循环
            result = f"工具执行失败: {e}"

        elapsed = time.monotonic() - started
        truncated = str(result)
        self._audit(log, caller, spec, arguments, f"ok({elapsed:.1f}s) args={args_desc}")
        if len(truncated) > 8000:
            truncated = truncated[:8000] + f"\n...（结果过长已截断，共 {len(result)} 字符）"
        return truncated

    @staticmethod
    def _audit(log: Any | None, caller: ToolCallContext, spec: ToolSpec,
               arguments: dict[str, Any], outcome: str) -> None:
        if log is None:
            return
        safe_args = repr(arguments)[:200]
        log.info(
            "[工具审计] %s(%s) 群=%s 用户=%s admin=%s %s | %s",
            spec.name,
            spec.owner,
            caller.group_id,
            caller.user_id,
            spec.admin_only,
            outcome,
            safe_args,
        )
