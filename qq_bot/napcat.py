# -*- coding: utf-8 -*-
"""NapCat（OneBot 11）WebSocket 客户端。

实测结论（NapCat「WS 正向服务器」，本机 3001 端口）：

* 连接必须带 ``Authorization: Bearer <token>`` 头
* 请求带 ``echo`` 字段做请求-响应配对；响应原样带回该 ``echo``
* **本地文件必须写成 ``file:///`` + 正斜杠路径，且不做百分号编码** ——
  一旦编码（``%E6%9A%96`` 之类）NapCat 无法解析，请求会挂死到超时
* 引用（回复）消息：message 数组首段为
  ``{"type": "reply", "data": {"id": "<message_id>"}}``
* 合并转发：``send_private_forward_msg`` / ``send_group_forward_msg``，
  节点形如 ``{"type": "node", "data": {"uin", "name", "content": [...]}}``，
  节点 content 内可放 image 段

读线程与请求-响应共用同一条连接，因此 recv 只在读线程里发生，
``call()`` 通过 ``echo`` 从读线程取回自己的响应。
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

import websocket

__all__ = ["NapCatClient", "NapCatError", "to_file_uri", "build_node"]


class NapCatError(Exception):
    """NapCat API 调用失败（超时 / status 非 ok）。"""


def to_file_uri(path: str) -> str:
    """本地路径 → NapCat 可接受的 file URI（正斜杠，不做百分号编码）。"""
    return "file:///" + str(path).replace("\\", "/")


def build_node(uin, name: str, content: list) -> dict:
    """构造合并转发节点。content 为 OneBot 消息段数组。"""
    return {"type": "node", "data": {"uin": str(uin), "name": name,
                                     "content": content}}


def text_seg(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}


def image_seg(path: str) -> dict:
    return {"type": "image", "data": {"file": to_file_uri(path)}}


def reply_seg(message_id) -> dict:
    return {"type": "reply", "data": {"id": str(message_id)}}


class NapCatClient:
    """同步的 NapCat 客户端；事件通过 ``on_event`` 回调投递。"""

    def __init__(self,
                 ws_url: str,
                 token: str = "",
                 on_event: Optional[Callable[[dict], None]] = None,
                 reconnect_interval: float = 5.0,
                 logger: Optional[Callable[[str], None]] = None,
                 call_timeout: float = 30.0,
                 read_timeout: float = 90.0):
        self.ws_url = ws_url
        self.token = token
        self.on_event = on_event
        self.reconnect_interval = reconnect_interval
        self.log = logger or (lambda m: None)
        self.call_timeout = call_timeout
        # 读超时必须大于 NapCat 心跳间隔（默认 30s），否则空闲时会被误判断线
        self.read_timeout = read_timeout

        self._ws = None
        self._reader: Optional[threading.Thread] = None
        self._seq = 0
        self._pending: dict = {}
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self.connected = False

    # ---------------------------------------------------------- 连接

    def connect(self, timeout: float = 15.0) -> None:
        """建立连接并启动读线程。"""
        header = [f"Authorization: Bearer {self.token}"] if self.token else None
        self._ws = websocket.create_connection(
            self.ws_url, header=header, timeout=timeout)
        # 连接建立后放宽读超时：空闲等心跳不算断线
        try:
            self._ws.settimeout(self.read_timeout)
        except Exception:
            pass
        self._stopped.clear()
        self.connected = True
        self._reader = threading.Thread(target=self._read_loop,
                                        name="napcat-reader", daemon=True)
        self._reader.start()

    def close(self) -> None:
        self._stopped.set()
        self.connected = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass

    def run_forever(self, on_connected=None) -> None:
        """连接并自动重连，直到 ``close()`` 被调用。

        :param on_connected: 每次连接成功后的回调（用于取登录信息、挂事件处理器）
        """
        while not self._stopped.is_set():
            try:
                self.connect()
                self.log(f"[ws] 已连接 {self.ws_url}")
                if on_connected:
                    try:
                        on_connected()
                    except Exception as e:
                        self.log(f"[ws] 连接后回调异常：{e}")
            except Exception as e:
                self.log(f"[ws] 连接失败：{e}")
                if self._stopped.is_set():
                    break
                time.sleep(self.reconnect_interval)
                continue

            # 等读线程退出（连接断开时退出）
            while self._reader and self._reader.is_alive():
                self._reader.join(timeout=1.0)
                if self._stopped.is_set():
                    break
            self.connected = False
            self._fail_pending("连接已断开")
            if self._stopped.is_set():
                break
            self.log(f"[ws] 连接断开，{self.reconnect_interval}s 后重连…")
            time.sleep(self.reconnect_interval)

    # ---------------------------------------------------------- 内部

    def _read_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue  # 空闲超时（心跳间隙），连接仍然有效
            except Exception as e:
                if not self._stopped.is_set():
                    self.log(f"[ws] 读取中断：{e}")
                return
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue

            echo = msg.get("echo")
            if echo:
                with self._lock:
                    slot = self._pending.pop(echo, None)
                if slot is not None:
                    slot["resp"] = msg
                    slot["event"].set()
                    continue

            if self.on_event:
                try:
                    self.on_event(msg)
                except Exception as e:
                    self.log(f"[event] 回调异常：{e}")

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["error"] = reason
            slot["event"].set()

    # ---------------------------------------------------------- 调用

    def call(self, action: str, timeout: Optional[float] = None, **params):
        """调用 OneBot API 并返回其 ``data``。

        :raises NapCatError: 未连接 / 超时 / status 非 ok
        """
        if not self._ws or not self.connected:
            raise NapCatError("尚未连接到 NapCat")
        timeout = self.call_timeout if timeout is None else timeout

        with self._lock:
            self._seq += 1
            echo = f"c{self._seq}"
            slot = {"event": threading.Event(), "resp": None, "error": None}
            self._pending[echo] = slot

        try:
            self._ws.send(json.dumps(
                {"action": action, "params": params, "echo": echo}))
        except Exception as e:
            with self._lock:
                self._pending.pop(echo, None)
            raise NapCatError(f"{action} 发送失败：{e}")

        if not slot["event"].wait(timeout):
            with self._lock:
                self._pending.pop(echo, None)
            raise NapCatError(f"{action} 超时（{timeout}s）")
        if slot["error"]:
            raise NapCatError(f"{action} 失败：{slot['error']}")

        resp = slot["resp"] or {}
        if resp.get("status") != "ok" or resp.get("retcode") not in (0, None):
            detail = resp.get("message") or resp.get("wording") or ""
            raise NapCatError(
                f"{action} 返回异常：status={resp.get('status')} "
                f"retcode={resp.get('retcode')} {detail}")
        return resp.get("data") or {}

    # ---------------------------------------------------------- 高层 API

    def get_login_info(self):
        return self.call("get_login_info", timeout=10)

    def get_status(self):
        return self.call("get_status", timeout=10)

    def send_private_msg(self, user_id, message, timeout: Optional[float] = None):
        return self.call("send_private_msg", timeout=timeout,
                         user_id=user_id, message=message)

    def send_group_msg(self, group_id, message, timeout: Optional[float] = None):
        return self.call("send_group_msg", timeout=timeout,
                         group_id=group_id, message=message)

    def send_private_forward(self, user_id, nodes, timeout: Optional[float] = None):
        return self.call("send_private_forward_msg", timeout=timeout,
                         user_id=user_id, messages=nodes)

    def send_group_forward(self, group_id, nodes, timeout: Optional[float] = None):
        return self.call("send_group_forward_msg", timeout=timeout,
                         group_id=group_id, messages=nodes)

    def send_msg(self, message_type: str, target_id, message,
                 timeout: Optional[float] = None):
        """按会话类型发送消息。"""
        if message_type == "group":
            return self.send_group_msg(target_id, message, timeout)
        return self.send_private_msg(target_id, message, timeout)

    def send_forward(self, message_type: str, target_id, nodes,
                     timeout: Optional[float] = None):
        """按会话类型发送合并转发。"""
        if message_type == "group":
            return self.send_group_forward(target_id, nodes, timeout)
        return self.send_private_forward(target_id, nodes, timeout)

    def reply(self, message_type: str, target_id, message_id, text: str,
              timeout: Optional[float] = None):
        """引用原消息回复一段文本。"""
        message = [reply_seg(message_id), text_seg(text)]
        return self.send_msg(message_type, target_id, message, timeout)
