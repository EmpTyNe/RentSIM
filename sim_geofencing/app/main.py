"""FastAPI-приложение сервиса аренды СИМ (фокус: симуляция + геозоны).

Эндпоинты:
  GET  /devices/nearby      — свободные устройства рядом (ST_DWithin);
  GET  /zones               — все геозоны как GeoJSON (для карты);
  GET  /devices/{code}/geofence  — что разрешено устройству прямо сейчас;
  POST /trips/start         — начать поездку (разблокировать устройство);
  POST /trips/{id}/end      — завершить (с проверкой запрещённой зоны);
  WS   /ws/devices          — live-поток позиций устройств.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin import router as admin_router
from app.auth import hash_password
from app.config import get_settings
from app.database import Base, SessionLocal, engine, get_session
from app.geofencing import evaluate_point, find_nearby_devices, zones_as_geojson
from app.models import Device, Trip, User
from app.mqtt_client import run_mqtt_bridge, send_command
from app.simulation import run_simulation
from app.ws import manager

logger = logging.getLogger("startup")
settings = get_settings()
_stop_event = asyncio.Event()


async def _init_schema() -> None:
    """Создаём расширение PostGIS и таблицы, если их ещё нет.

    С повторными попытками: если база медленно поднимается (бывает на Windows),
    приложение не падает, а ждёт и пробует снова.
    """
    last_exc: Exception | None = None
    for _ in range(40):
        try:
            async with engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:  # база ещё не готова — подождём и повторим
            last_exc = exc
            await asyncio.sleep(3)
    if last_exc:
        raise last_exc


async def _ensure_admin() -> None:
    """Гарантируем наличие пользователя-администратора (не трогая прочие данные).

    Делает вход надёжным даже без запуска app.seed: если админа в базе нет —
    создаём его с хешированным паролем. Существующие зоны/устройства не затрагиваются.
    """
    try:
        async with SessionLocal() as session:
            exists = (
                await session.execute(
                    select(User).where(User.username == settings.admin_user)
                )
            ).scalar_one_or_none()
            if exists is None:
                session.add(
                    User(
                        username=settings.admin_user,
                        email="admin@example.com",
                        password_hash=hash_password(settings.admin_password),
                    )
                )
                await session.commit()
                logger.info("Создан администратор по умолчанию: %s", settings.admin_user)
    except Exception:
        logger.exception(
            "Не удалось создать администратора — возможно, нужна пересборка БД "
            "(docker compose down -v) при обновлении со старой схемы"
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1) схема БД → 2) админ → 3) MQTT-мост → 4) движок симуляции
    await _init_schema()
    await _ensure_admin()
    _stop_event.clear()
    tasks = [
        asyncio.create_task(run_mqtt_bridge(_stop_event)),
        asyncio.create_task(run_simulation(_stop_event)),
    ]
    yield
    _stop_event.set()
    for t in tasks:
        t.cancel()


app = FastAPI(title="СИМ-шеринг: симуляция + геозоны", lifespan=lifespan)
app.include_router(admin_router)

ADMIN_HTML = Path(__file__).parent / "static" / "admin.html"


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    """Веб-панель оператора с живой картой устройств и геозон."""
    return ADMIN_HTML.read_text(encoding="utf-8")


async def _send_command(code: str, command: dict) -> None:
    await send_command(code, command)


@app.get("/sim/roster")
async def sim_roster(session: AsyncSession = Depends(get_session)):
    """Внутренний эндпоинт для эмулятора: список устройств и их позиций.

    Эмулятор периодически опрашивает его, чтобы знать, какие самокаты сейчас
    в парке (добавленные оператором — появляются, удалённые — исчезают).
    """
    rows = (
        await session.execute(
            select(
                Device.code,
                func.ST_Y(Device.geom).label("lat"),
                func.ST_X(Device.geom).label("lng"),
                Device.status,
            )
        )
    ).all()
    return [
        {"code": r.code, "lat": r.lat, "lng": r.lng, "status": r.status}
        for r in rows
    ]


@app.get("/devices/nearby")
async def devices_nearby(
    lat: float,
    lng: float,
    radius: float = 500.0,
    session: AsyncSession = Depends(get_session),
):
    devices = await find_nearby_devices(session, lat, lng, radius)
    return [d.__dict__ for d in devices]


@app.get("/zones")
async def zones(session: AsyncSession = Depends(get_session)):
    return {"type": "FeatureCollection", "features": await zones_as_geojson(session)}


@app.get("/devices/{code}/geofence")
async def device_geofence(code: str, session: AsyncSession = Depends(get_session)):
    device = (
        await session.execute(select(Device).where(Device.code == code))
    ).scalar_one_or_none()
    if device is None or device.geom is None:
        raise HTTPException(404, "Устройство не найдено или нет координат")
    # Достаём текущие lat/lng устройства
    from sqlalchemy import func

    row = (
        await session.execute(
            select(func.ST_Y(Device.geom), func.ST_X(Device.geom)).where(
                Device.code == code
            )
        )
    ).one()
    lat, lng = row[0], row[1]
    fence = await evaluate_point(session, lat, lng)
    return {
        "code": code,
        "in_forbidden": fence.in_forbidden,
        "speed_limit": fence.effective_speed_limit,
        "zones": fence.zone_names,
    }


@app.post("/trips/start")
async def start_trip(
    user_id: int,
    device_code: str,
    session: AsyncSession = Depends(get_session),
):
    device = (
        await session.execute(select(Device).where(Device.code == device_code))
    ).scalar_one_or_none()
    if device is None:
        raise HTTPException(404, "Устройство не найдено")
    if device.status != "available":
        raise HTTPException(409, "Устройство недоступно")

    device.status = "in_use"
    trip = Trip(user_id=user_id, device_id=device.id, path=[])
    session.add(trip)
    await session.commit()
    await session.refresh(trip)

    await _send_command(device_code, {"command": "unlock"})
    return {"trip_id": trip.id, "device": device_code, "status": "started"}


@app.post("/trips/{trip_id}/end")
async def end_trip(trip_id: int, session: AsyncSession = Depends(get_session)):
    trip = (
        await session.execute(select(Trip).where(Trip.id == trip_id))
    ).scalar_one_or_none()
    if trip is None:
        raise HTTPException(404, "Поездка не найдена")
    if trip.ended_at is not None:
        raise HTTPException(409, "Поездка уже завершена")

    device = (
        await session.execute(select(Device).where(Device.id == trip.device_id))
    ).scalar_one()

    # Геозона: запрещаем завершать поездку в запрещённой зоне
    from sqlalchemy import func

    row = (
        await session.execute(
            select(func.ST_Y(Device.geom), func.ST_X(Device.geom)).where(
                Device.id == device.id
            )
        )
    ).one()
    fence = await evaluate_point(session, row[0], row[1])
    if fence.in_forbidden:
        raise HTTPException(
            422, "Нельзя завершить поездку в запрещённой зоне — доедьте до разрешённой"
        )

    # Расчёт стоимости: старт + поминутно
    trip.ended_at = datetime.now(timezone.utc)
    minutes = (trip.ended_at - trip.started_at).total_seconds() / 60
    trip.cost = round(
        settings.tariff_unlock_price + minutes * settings.tariff_price_per_minute, 2
    )
    device.status = "available"
    await session.commit()

    await _send_command(device.code, {"command": "lock"})
    return {
        "trip_id": trip.id,
        "minutes": round(minutes, 1),
        "cost": trip.cost,
        "status": "ended",
    }


@app.websocket("/ws/devices")
async def ws_devices(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()  # клиент может просто пинговать
    except WebSocketDisconnect:
        await manager.disconnect(ws)
