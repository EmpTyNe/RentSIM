"""Запись поездок: открытие при старте аренды, накопление маршрута из
телеметрии и закрытие при завершении (с расчётом дистанции и стоимости).

Состояние открытых поездок держим в памяти (один процесс), в БД пишем при
старте (строка Trip) и при закрытии (дистанция, стоимость, исход, маршрут).
"""
import logging
import math
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Trip

settings = get_settings()
logger = logging.getLogger("trips")

_open: dict[str, dict] = {}  # code -> {"id", "points": [(lat,lng)], "start": datetime}


def _haversine(a: tuple, b: tuple) -> float:
    """Расстояние между двумя точками (lat,lng) в метрах."""
    r = 6371000.0
    dlat = math.radians(b[0] - a[0])
    dlng = math.radians(b[1] - a[1])
    s = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) * math.sin(dlng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(s))


async def open_trip(session: AsyncSession, code: str, device_id: int) -> int:
    trip = Trip(device_id=device_id, user_id=None, path=[])
    session.add(trip)
    await session.commit()
    _open[code] = {"id": trip.id, "points": [], "start": trip.started_at}
    return trip.id


def add_point(code: str, lat: float, lng: float) -> None:
    o = _open.get(code)
    if o is not None:
        o["points"].append((lat, lng))


async def close_trip(session: AsyncSession, code: str, outcome: str) -> None:
    o = _open.pop(code, None)
    if o is None:
        return
    pts = o["points"]
    dist = sum(_haversine(pts[i], pts[i + 1]) for i in range(len(pts) - 1)) if len(pts) > 1 else 0.0
    ended = datetime.now(timezone.utc)
    start = o.get("start")
    minutes = (ended - start).total_seconds() / 60 if start else 0.0
    cost = round(settings.tariff_unlock_price + minutes * settings.tariff_price_per_minute, 2)
    await session.execute(
        update(Trip)
        .where(Trip.id == o["id"])
        .values(
            ended_at=ended,
            distance_m=round(dist, 1),
            cost=cost,
            outcome=outcome,
            path=[[p[1], p[0]] for p in pts],
        )
    )
    await session.commit()
