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
        self._poller_running = False  # True once on_first_connect succeeded, until the last client leaves

    async def connect(self, ws: WebSocket) -> bool:
        """Accept and register. Returns False when the GPS poller could not be started (the socket stays registered)."""
        await ws.accept()
        return self.register(ws)

    def register(self, ws: WebSocket) -> bool:
        """Register an ALREADY-ACCEPTED socket. Never raises: if starting the GPS poller (on_first_connect) fails, the
        error is logged, the socket stays connected, and False is returned so the caller can tell the client. The poller
        start is retried on the next connection while it is not running."""
        if ws not in self._connections:
            self._connections.append(ws)
        logger.info(f"[WS] Client connected ({len(self._connections)} total)")
        if self._poller_running or not self.on_first_connect:
            return True
        try:
            self.on_first_connect()
        except Exception as exc:  # noqa: BLE001
            logger.error("[WS] could not start the GPS poller (%s); clients stay connected without live positions", type(exc).__name__, exc_info=True)
            return False
        self._poller_running = True
        return True

    def disconnect(self, ws: WebSocket) -> None:
        if ws not in self._connections:
            return
        self._connections.remove(ws)
        logger.info(f"[WS] Client disconnected ({len(self._connections)} total)")
        if not self._connections and self._poller_running:
            self._poller_running = False
            if self.on_last_disconnect:
                try:
                    self.on_last_disconnect()
                except Exception as exc:  # noqa: BLE001
                    logger.error("[WS] could not pause the GPS poller (%s)", type(exc).__name__, exc_info=True)

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
