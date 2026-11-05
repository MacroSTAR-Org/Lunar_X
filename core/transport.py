import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlparse

import aiohttp
import websockets

from .logger import logger

# 事件队列上限。聊天机器人不需要无限积压：消费端卡住时，旧消息已经失去
# 回复价值，继续堆只会吃内存。满队列丢最旧的，保证 WebSocket 读取永不阻塞。
DEFAULT_EVENT_QUEUE_SIZE = 1024


class BoundedEventQueue:
    """有界事件队列：满时丢弃最旧事件并计数，而不是反压 WebSocket 读取。"""

    def __init__(self, maxsize: int = DEFAULT_EVENT_QUEUE_SIZE):
        self.maxsize = max(1, int(maxsize))
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=self.maxsize)
        self.dropped = 0

    async def put(self, item: Any) -> None:
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    continue
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 100 == 0:
                    logger.warning(
                        f"事件队列已满（上限 {self.maxsize}），"
                        f"已丢弃 {self.dropped} 条最旧事件"
                    )

    async def get(self) -> Any:
        return await self._queue.get()

    def get_nowait(self) -> Any:
        return self._queue.get_nowait()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    def stop(self) -> None:
        """清空积压并放入停止哨兵（None）。"""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._queue.put_nowait(None)


class BaseTransport:
    """传输层抽象：事件接收 + API 调用。

    协议适配器（OneBotAdapter / MilkyAdapter）依赖本接口，
    bot 核心只通过适配器访问协议，不直接感知协议细节。
    """

    _message_queue: BoundedEventQueue

    def queue_stats(self) -> dict[str, int]:
        """事件队列的深度/上限/累计丢弃数，供运行统计与排障使用。"""
        return {
            "size": self._message_queue.qsize(),
            "maxsize": self._message_queue.maxsize,
            "dropped": self._message_queue.dropped,
        }

    async def connect(self, is_reconnect: bool = False) -> bool:
        raise NotImplementedError

    def listen(self) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError

    async def send(
        self, data: dict[str, Any], wait_for_response: bool = True
    ) -> dict[str, Any] | None:
        raise NotImplementedError

    async def close(self):
        raise NotImplementedError


class MilkyTransport(BaseTransport):
    """Milky 协议端传输层（应用端客户端）。

    - API 调用: POST {server}/api/{action}，Authorization: Bearer {token}
    - 事件接收: WebSocket {server}/event（GET /event 携带 Upgrade: websocket）
    - 响应统一转为 OneBot 风格 {status, retcode, data/message}，供上层无差别判断
    """

    def __init__(
        self,
        server_url: str,
        token: str | None = None,
        max_retries: int = 5,
        request_timeout: float = 30.0,
        queue_maxsize: int = DEFAULT_EVENT_QUEUE_SIZE,
    ):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.max_retries = max_retries
        self._request_timeout = request_timeout
        self.websocket = None
        self._session: aiohttp.ClientSession | None = None
        self._listener_task: asyncio.Task | None = None
        self._message_queue = BoundedEventQueue(queue_maxsize)
        self._is_closing = False

    @property
    def _ws_url(self) -> str:
        parsed = urlparse(self.server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        netloc = parsed.netloc
        if parsed.path not in ("", "/"):
            netloc = parsed.netloc + parsed.path.rstrip("/")
        return f"{scheme}://{netloc}/event"

    def _auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ---------- 连接与事件接收 ----------

    async def connect(self, is_reconnect: bool = False) -> bool:
        retries = 0
        max_attempts = self.max_retries if not is_reconnect else float("inf")

        while retries < max_attempts:
            if self._is_closing:
                logger.warning("连接正在关闭，停止重试。")
                return False

            try:
                if is_reconnect:
                    logger.warning(
                        f"尝试重连 Milky 事件通道 {self._ws_url} (第 {retries + 1} 次重试)"
                    )
                else:
                    logger.info(f"尝试连接 Milky 协议端事件通道 {self._ws_url}")

                self.websocket = await websockets.connect(
                    self._ws_url, additional_headers=self._auth_headers()
                )
                logger.success("Milky 事件通道连接成功")

                current_task = asyncio.current_task()
                if (
                    is_reconnect
                    and self._listener_task
                    and self._listener_task is not current_task
                    and not self._listener_task.done()
                ):
                    self._listener_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._listener_task

                self._listener_task = asyncio.create_task(self._listen_loop())
                return True
            except KeyboardInterrupt:
                logger.warning("连接被 KeyboardInterrupt 中断")
                self._is_closing = True
                return False
            except Exception as e:
                retries += 1
                logger.error(f"连接 Milky 事件通道失败: {e}")
                if retries < self.max_retries or is_reconnect:
                    wait_time = min(2**retries, 60)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)
                elif not is_reconnect:
                    logger.error("达到最大重试次数，连接失败")
                    return False

        return False

    async def _reconnect_loop(self):
        if self._is_closing:
            return

        logger.error("Milky 事件通道意外断开，尝试重新连接...")

        self.websocket = None

        success = await self.connect(is_reconnect=True)

        if success:
            logger.success("重连成功，继续监听。")
        else:
            logger.critical("重连失败，且达到最大重试次数或被主动关闭。")
            await self._message_queue.put(None)

    async def _listen_loop(self):
        if not self.websocket:
            logger.error("Milky 事件通道未连接，无法启动监听循环")
            return

        try:
            async for message in self.websocket:
                try:
                    data = await asyncio.to_thread(json.loads, message)
                    await self._message_queue.put(data)
                except json.JSONDecodeError:
                    logger.error(f"无法解析JSON消息: {message[:200]}...")
                except asyncio.CancelledError:
                    logger.info("WebSocket监听任务被取消")
                    break
                except Exception as e:
                    logger.error(f"监听器处理消息时发生未知错误: {e}, 消息: {message[:200]}...")

            # 新版 websockets 的 async for 在连接关闭时正常结束，不抛
            # ConnectionClosed；这里必须显式触发重连，否则会静默失联。
            if not self._is_closing:
                logger.info("Milky 事件通道已关闭 (迭代结束)")
                await self._reconnect_loop()

        except websockets.exceptions.ConnectionClosed:
            logger.info("Milky 事件通道已关闭 (意外断开或服务器关闭)")
            if not self._is_closing:
                await self._reconnect_loop()
        except asyncio.CancelledError:
            logger.info("WebSocket监听任务被取消")
        except KeyboardInterrupt:
            logger.warning("监听循环被 KeyboardInterrupt 中断")
            self._is_closing = True
        except Exception as e:
            logger.error(f"监听消息时发生错误: {e}")
            if not self._is_closing:
                await self._reconnect_loop()
        finally:
            logger.info("_listen_loop 结束。")
            if self._is_closing:
                self._message_queue.stop()

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                try:
                    message = await self._message_queue.get()
                    if message is None:
                        logger.info("事件监听已停止")
                        break
                    yield message
                except asyncio.CancelledError:
                    logger.info("listen() 任务被取消，正在退出。")
                    break
                except Exception as e:
                    logger.error(f"从消息队列获取消息时发生错误: {e}")
                    break
        except KeyboardInterrupt:
            logger.warning("Listen 函数被 KeyboardInterrupt 中断")
        finally:
            logger.info("Listen 函数结束")

    # ---------- API 调用 ----------

    async def send(
        self, data: dict[str, Any], wait_for_response: bool = True
    ) -> dict[str, Any] | None:
        action = data.get("action")
        params = data.get("params", {})

        if not action:
            logger.error("Milky API 调用缺少 action")
            return {"status": "failed", "message": "缺少 action"}

        if params is None:
            params = {}

        try:
            session = await self._get_session()
            async with session.post(
                f"{self.server_url}/api/{action}",
                json=params,
                headers=self._auth_headers(),
                timeout=aiohttp.ClientTimeout(total=self._request_timeout),
            ) as resp:
                if resp.status == 401:
                    return {
                        "status": "failed",
                        "retcode": -401,
                        "message": "鉴权凭据未提供或不匹配 (HTTP 401)",
                    }
                if resp.status == 404:
                    return {
                        "status": "failed",
                        "retcode": -404,
                        "message": f"API 不存在 (HTTP 404): {action}",
                    }
                if resp.status == 415:
                    return {
                        "status": "failed",
                        "retcode": -415,
                        "message": "POST Content-Type 不支持 (HTTP 415)",
                    }
                try:
                    body = await resp.json()
                except Exception:
                    return {
                        "status": "failed",
                        "retcode": resp.status,
                        "message": f"协议端返回非 JSON 响应 (HTTP {resp.status})",
                    }

            if body.get("status") == "ok":
                return {
                    "status": "ok",
                    "retcode": body.get("retcode", 0),
                    "data": body.get("data"),
                    "raw_response": body,
                }
            else:
                return {
                    "status": "failed",
                    "retcode": body.get("retcode"),
                    "message": body.get("message", "未知错误"),
                    "raw_response": body,
                }

        except TimeoutError:
            logger.error(f"Milky API 调用 {action} 超时，参数: {params}")
            raise TimeoutError(f"等待 Milky 协议端响应超时: {action}") from None
        except aiohttp.ClientError as e:
            logger.error(f"Milky API 调用 {action} 网络错误: {e}")
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error(f"Milky API 调用 {action} 时发生错误: {e}")
            return {"status": "error", "message": str(e)}

    # ---------- 关闭 ----------

    async def close(self):
        logger.info("正在关闭 Milky 传输层...")
        self._is_closing = True

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            finally:
                self._listener_task = None

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception:
                pass
            finally:
                self.websocket = None

        if self._session and not self._session.closed:
            await self._session.close()

        self._message_queue.stop()
        logger.info("Milky 传输层已关闭")
