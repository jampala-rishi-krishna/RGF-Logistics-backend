from __future__ import annotations

import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("ws_manager")


class ConnectionManager:
    """Flat connection list with broadcast
    to all. Single-tenant (one dispatcher-facing fleet), so no per-user connection map."""

    def __init__(self) -> None:
        self._connections: list[WebSocket] = []
        self.on_first_connect = None  # set by main.py to resume the GPS poller
        self.on_last_disconnect = None  # set by main.py to pause the GPS poller

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        was_empty = not self._connections
        self._connections.append(ws)
        logger.info(f"[WS] Client connected ({len(self._connections)} total)")
        if was_empty and self.on_first_connect:
            self.on_first_connect()

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self._connections:
            self._connections.remove(ws)
        logger.info(f"[WS] Client disconnected ({len(self._connections)} total)")
        if not self._connections and self.on_last_disconnect:
            self.on_last_disconnect()

    async def broadcast(self, payload: dict) -> None:
        if not self._connections:
            return
        message = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


manager = ConnectionManager()
