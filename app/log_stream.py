"""实时日志流 — 环形缓冲区 + WebSocket 连接管理器"""

import json
import asyncio
import threading
from collections import deque

from fastapi import WebSocket


class LogRingBuffer:
    """线程安全环形缓冲区，保存最近 N 条日志"""

    def __init__(self, maxlen: int = 500):
        self._buffer: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, entry: dict):
        with self._lock:
            self._buffer.append(entry)

    def snapshot(self) -> list:
        with self._lock:
            return list(self._buffer)


class ConnectionManager:
    """管理 WebSocket 客户端连接"""

    def __init__(self):
        self._connections: set[WebSocket] = set()
        self._lock = threading.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        with self._lock:
            self._connections.add(ws)

    def disconnect(self, ws: WebSocket):
        with self._lock:
            self._connections.discard(ws)

    async def broadcast(self, entry: dict):
        """向所有已连接的客户端发送 JSON 消息，自动移除死连接"""
        dead: set[WebSocket] = set()
        with self._lock:
            conns = list(self._connections)
        for ws in conns:
            try:
                await ws.send_json(entry)
            except Exception:
                dead.add(ws)
        if dead:
            with self._lock:
                self._connections -= dead


# ── 模块级单例 ──
ring_buffer = LogRingBuffer()
manager = ConnectionManager()


def publish(entry: dict):
    """同步入口：追加到环形缓冲区 + 调度异步广播"""
    ring_buffer.append(entry)
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(manager.broadcast({"type": "entry", "entry": entry}))
    except RuntimeError:
        pass  # 不在异步上下文中（极少发生），仅写入缓冲区
