import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional, Set

from fastapi import WebSocket

logger = logging.getLogger("app.session_events")


def _format_event_time(dt: datetime) -> str:
    ts = dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return ts.replace("+00:00", "Z")


def _build_event_id(dt: datetime) -> str:
    return f"evt_{dt.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


@dataclass
class SessionConnection:
    websocket: WebSocket
    uid: str
    session_ids: Set[str] = field(default_factory=set)


class SessionEventBus:
    def __init__(self) -> None:
        self._connections: Dict[str, SessionConnection] = {}
        self._sessions: Dict[str, Set[str]] = {}
        self._lock = asyncio.Lock()

    async def register(self, websocket: WebSocket, uid: str) -> str:
        conn_id = uuid.uuid4().hex
        async with self._lock:
            self._connections[conn_id] = SessionConnection(websocket=websocket, uid=uid)
        return conn_id

    async def unregister(self, conn_id: str) -> None:
        async with self._lock:
            conn = self._connections.pop(conn_id, None)
            if not conn:
                return
            for session_id in list(conn.session_ids):
                listeners = self._sessions.get(session_id)
                if listeners:
                    listeners.discard(conn_id)
                    if not listeners:
                        self._sessions.pop(session_id, None)

    async def subscribe(self, conn_id: str, session_id: str) -> None:
        async with self._lock:
            conn = self._connections.get(conn_id)
            if not conn:
                return
            conn.session_ids.add(session_id)
            self._sessions.setdefault(session_id, set()).add(conn_id)

    async def unsubscribe(self, conn_id: str, session_id: str) -> None:
        async with self._lock:
            conn = self._connections.get(conn_id)
            if conn:
                conn.session_ids.discard(session_id)
            listeners = self._sessions.get(session_id)
            if listeners:
                listeners.discard(conn_id)
                if not listeners:
                    self._sessions.pop(session_id, None)

    async def publish(
        self,
        session_id: str,
        event_type: str,
        payload: Optional[dict] = None,
        updated_at: Optional[datetime] = None,
    ) -> None:
        now = updated_at or datetime.now(timezone.utc)
        event = {
            "type": event_type,
            "sessionId": session_id,
            "eventId": _build_event_id(now),
            "updatedAt": _format_event_time(now),
            "payload": payload or {},
        }

        async with self._lock:
            targets = list(self._sessions.get(session_id, set()))
            connections = {cid: self._connections.get(cid) for cid in targets}

        stale_ids: list[str] = []
        for conn_id, conn in connections.items():
            if not conn:
                stale_ids.append(conn_id)
                continue
            try:
                await conn.websocket.send_json(event)
            except Exception as e:
                logger.info(f"[SessionEvents] send failed conn={conn_id}: {e}")
                stale_ids.append(conn_id)

        for conn_id in stale_ids:
            await self.unregister(conn_id)


session_event_bus = SessionEventBus()


async def publish_session_event(
    session_id: str,
    event_type: str,
    payload: Optional[dict] = None,
    updated_at: Optional[datetime] = None,
) -> None:
    await session_event_bus.publish(session_id, event_type, payload=payload, updated_at=updated_at)
