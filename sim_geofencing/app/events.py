"""Журнал событий оператора.

Хранит последние события в памяти (кольцевой буфер) и транслирует каждое новое
событие в WebSocket, чтобы лента на дашборде обновлялась в реальном времени.
Уровни: info (обычное), ok (успех), warn (предупреждение), error (тревога).
"""
import collections
from datetime import datetime

from app.ws import manager

_EVENTS: collections.deque = collections.deque(maxlen=120)


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


async def log(level: str, message: str, code: str | None = None) -> None:
    ev = {"ts": _ts(), "level": level, "message": message, "code": code}
    _EVENTS.append(ev)
    await manager.broadcast({"type": "event", **ev})


def recent() -> list[dict]:
    return list(_EVENTS)
