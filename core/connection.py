import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import websockets

from .logger import logger


class WebSocketConnection:
    def __init__(
        self,
        ws_url: str,
        token: str | None = None,
        max_retries: int = 5,
        request_timeout: float = 30.0,
    ):
        self.ws_url = ws_url
        self.token = token
        self.max_retries = max_retries
        self.websocket = None
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._request_timeout = request_timeout
        self._listener_task: asyncio.Task | None = None
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._is_closing = False

    async def connect(self, is_reconnect: bool = False) -> bool:
        retries = 0
        max_attempts = self.max_retries if not is_reconnect else float("inf")

        ws_url = self.ws_url
        if self.token:
            separator = "&" if "?" in ws_url else "?"
            ws_url = f"{ws_url}{separator}access_token={self.token}"
            if not is_reconnect:
                logger.info(f"将使用Token作为URL查询参数进行认证: {ws_url}")

        while retries < max_attempts:
            if self._is_closing:
                logger.warning("连接正在关闭，停止重试。")
                return False

            try:
                if is_reconnect:
                    logger.warning(f"尝试重连至WebSocket服务器 {ws_url} (第 {retries + 1} 次重试)")
                else:
                    logger.info(
                        f"尝试连接至WebSocket服务器 {ws_url} (尝试 {retries + 1}/{self.max_retries})"
                    )

                self.websocket = await websockets.connect(ws_url)
                logger.success("WebSocket连接成功")

                if is_reconnect and self._listener_task and not self._listener_task.done():
                    self._listener_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._listener_task

                self._listener_task = asyncio.create_task(self._listen_loop())
                return True
            except websockets.exceptions.ConnectionClosed as e:
                retries += 1
                logger.error(f"连接失败: {e}")
                if retries < self.max_retries or is_reconnect:
                    wait_time = min(2**retries, 60)
                    logger.info(f"等待 {wait_time} 秒后重试...")
                    await asyncio.sleep(wait_time)
                elif not is_reconnect:
                    logger.error("达到最大重试次数，连接失败")
                    return False
            except KeyboardInterrupt:
                logger.warning("连接被 KeyboardInterrupt 中断")
                self._is_closing = True
                return False
            except Exception as e:
                retries += 1
                logger.error(f"连接失败: {e}")
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

        logger.error("WebSocket连接意外断开，尝试重新连接...")

        self.websocket = None

        success = await self.connect(is_reconnect=True)

        if success:
            logger.success("重连成功，继续监听。")
        else:
            logger.critical("重连失败，且达到最大重试次数或被主动关闭。")
            await self._message_queue.put(None)

    async def _listen_loop(self):
        if not self.websocket:
            logger.error("WebSocket未连接，无法启动监听循环")
            return

        try:
            async for message in self.websocket:
                try:
                    data = await asyncio.to_thread(json.loads, message)

                    request_id = None
                    if (
                        "echo" in data
                        and isinstance(data["echo"], dict)
                        and "request_id" in data["echo"]
                    ):
                        request_id = data["echo"]["request_id"]

                    if request_id:
                        future = self._pending_requests.get(request_id)
                        if future and not future.done():
                            if "status" in data and data["status"] == "error":
                                future.set_exception(
                                    Exception(f"服务器错误: {data.get('message', '未知错误')}")
                                )
                            else:
                                future.set_result(data)
                        else:
                            await self._message_queue.put(data)
                    else:
                        await self._message_queue.put(data)
                except json.JSONDecodeError:
                    logger.error(f"无法解析JSON消息: {message[:200]}...")
                except asyncio.CancelledError:
                    logger.info("WebSocket监听任务被取消")
                    break
                except Exception as e:
                    logger.error(f"监听器处理消息时发生未知错误: {e}, 消息: {message[:200]}...")

        except websockets.exceptions.ConnectionClosed:
            logger.info("WebSocket连接已关闭 (意外断开或服务器关闭)")
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
                while not self._message_queue.empty():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        self._message_queue.get_nowait()
                await self._message_queue.put(None)

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

    async def send(
        self, data: dict[str, Any], wait_for_response: bool = False
    ) -> dict[str, Any] | None:
        if not self.websocket:
            logger.error("WebSocket未连接，无法发送消息")
            if wait_for_response:
                raise ConnectionError("WebSocket未连接，无法发送消息并等待响应")
            return None

        request_id = None
        future = None

        if wait_for_response:
            request_id = str(uuid.uuid4())
            future = asyncio.get_event_loop().create_future()
            self._pending_requests[request_id] = future

            if "echo" not in data:
                data["echo"] = {}
            elif not isinstance(data["echo"], dict):
                logger.warning("消息中已存在非字典类型的'echo'字段，将被覆盖以包含request_id。")
                data["echo"] = {}

            data["echo"]["request_id"] = request_id

        try:
            message_json = await asyncio.to_thread(json.dumps, data)
            await self.websocket.send(message_json)

            if wait_for_response:
                try:
                    response = await asyncio.wait_for(future, timeout=self._request_timeout)
                    return response
                except TimeoutError:
                    logger.error(f"等待服务器响应超时，request_id: {request_id}")
                    raise TimeoutError(f"等待服务器响应超时，request_id: {request_id}") from None
                except Exception as e:
                    logger.error(f"等待请求 {request_id} 响应时发生错误: {e}")
                    raise
            else:
                return None
        except websockets.exceptions.ConnectionClosed as e:
            logger.error(f"连接已关闭: {e}")
            if (
                wait_for_response
                and request_id
                and request_id in self._pending_requests
                and not future.done()
            ):
                self._pending_requests.pop(request_id).set_exception(e)
            raise
        except KeyboardInterrupt:
            logger.warning("发送消息被 KeyboardInterrupt 中断")
            if (
                wait_for_response
                and request_id
                and request_id in self._pending_requests
                and not future.done()
            ):
                future.cancel("发送被中断")
            return None
        except Exception as e:
            logger.error(f"发送消息时发生错误: {e}")
            if (
                wait_for_response
                and request_id
                and request_id in self._pending_requests
                and not future.done()
            ):
                self._pending_requests.pop(request_id).set_exception(e)
            raise
        finally:
            if (
                wait_for_response
                and request_id
                and request_id in self._pending_requests
                and future.done()
            ):
                self._pending_requests.pop(request_id, None)

    async def close(self):
        logger.info("正在关闭WebSocket连接...")
        self._is_closing = True

        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
            except KeyboardInterrupt:
                logger.warning("关闭监听任务被 KeyboardInterrupt 中断")
            finally:
                self._listener_task = None

        if self.websocket:
            try:
                await self.websocket.close()
                await asyncio.sleep(0.1)
            except Exception as e:
                logger.error(f"关闭WebSocket连接时发生错误: {e}")
            except KeyboardInterrupt:
                logger.warning("关闭 WebSocket 连接被 KeyboardInterrupt 中断")
            finally:
                self.websocket = None
                logger.info("WebSocket连接已关闭")

        for _request_id, future in list(self._pending_requests.items()):
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        while not self._message_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._message_queue.get_nowait()
        await self._message_queue.put(None)
