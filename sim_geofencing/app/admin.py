"""Эндпоинты администратора (оператора парка).

Защищены простым токеном в заголовке X-Admin-Token (см. settings.admin_token).
Содержит CRUD по зонам и устройствам и управление состоянием устройств.
"""
import json

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_session
from app.models import Device, Trip, User, Zone
from app.mqtt_client import send_command
from app.geofencing import nearest_parking_for_device
from app.auth import verify_password
from app import simulation
from app import events
from app import trips
from app.ws import manager

settings = get_settings()
router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "Неверный admin-токен")


def _point(lat: float, lng: float):
    return func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326)


# ─────────────────────────── УСТРОЙСТВА ───────────────────────────

@router.get("/devices", dependencies=[Depends(require_admin)])
async def all_devices(session: AsyncSession = Depends(get_session)):
    """Все устройства парка с координатами и состоянием (для карты оператора)."""
    stmt = select(
        Device.code, Device.device_type, Device.status, Device.battery,
        Device.speed_limit,
        func.ST_Y(Device.geom).label("lat"),
        func.ST_X(Device.geom).label("lng"),
        Device.last_seen,
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "code": r.code, "device_type": r.device_type, "status": r.status,
            "battery": r.battery, "speed_limit": r.speed_limit,
            "lat": r.lat, "lng": r.lng,
            "last_seen": r.last_seen.isoformat() if r.last_seen else None,
        }
        for r in rows
    ]


class DeviceIn(BaseModel):
    code: str
    device_type: str = "scooter"
    lat: float
    lng: float


@router.post("/devices", dependencies=[Depends(require_admin)])
async def create_device(body: DeviceIn, session: AsyncSession = Depends(get_session)):
    exists = (
        await session.execute(select(Device.id).where(Device.code == body.code))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(409, f"Устройство {body.code} уже существует")
    dev = Device(
        code=body.code, device_type=body.device_type, status="available",
        battery=100, speed_limit=settings.default_speed_limit,
        geom=_point(body.lat, body.lng),
    )
    session.add(dev)
    await session.commit()
    await events.log("info", f"{body.code}: добавлен новый самокат (оператор)", body.code)
    return {"code": body.code, "status": "available"}


@router.get("/events", dependencies=[Depends(require_admin)])
async def get_events():
    """Последние события парка (для ленты журнала на дашборде)."""
    return events.recent()


@router.delete("/devices/{code}", dependencies=[Depends(require_admin)])
async def delete_device(code: str, session: AsyncSession = Depends(get_session)):
    dev = (
        await session.execute(select(Device).where(Device.code == code))
    ).scalar_one_or_none()
    if dev is None:
        raise HTTPException(404, "Устройство не найдено")
    await session.execute(delete(Trip).where(Trip.device_id == dev.id))
    await session.execute(delete(Device).where(Device.id == dev.id))
    await session.commit()
    await events.log("info", f"{code}: самокат удалён (оператор)", code)
    return {"code": code, "deleted": True}


class ControlIn(BaseModel):
    action: str               # maintenance | charging | fault | release | set_battery
    value: int | None = None  # для set_battery — уровень заряда


@router.post("/devices/{code}/control", dependencies=[Depends(require_admin)])
async def control_device(
    code: str, body: ControlIn, session: AsyncSession = Depends(get_session)
):
    """Действия оператора над устройством.

    - maintenance: снять на ремонт; через кулдаун движок вернёт в строй;
    - charging:    отправить на зарядку; через кулдаун вернётся заряженным;
    - fault:       вручную сымитировать поломку (для демонстрации);
    - release:     сразу вернуть в работу;
    - set_battery: выставить заряд (например, чтобы показать сценарий разрядки).
    """
    dev = (
        await session.execute(select(Device).where(Device.code == code))
    ).scalar_one_or_none()
    if dev is None:
        raise HTTPException(404, "Устройство не найдено")

    action = body.action

    if action == "set_battery":
        lvl = max(0, min(100, int(body.value or 0)))
        dev.battery = lvl
        await session.commit()
        await send_command(code, {"command": "set_battery", "value": lvl})
        return {"code": code, "action": action, "battery": lvl}

    if action == "maintenance":
        dev.status = "maintenance"
        await session.commit()
        await send_command(code, {"command": "lock"})
        simulation.schedule(code, simulation.CONFIG["repair_cooldown"])
        await simulation.broadcast_status(code, "maintenance")
        await events.log("info", f"{code}: отправлен на ремонт (оператор)", code)
        return {"code": code, "action": action, "status": "maintenance"}

    if action == "charging":
        dev.status = "charging"
        await session.commit()
        await send_command(code, {"command": "lock"})
        simulation.schedule(code, simulation.CONFIG["charge_cooldown"])
        await simulation.broadcast_status(code, "charging")
        await events.log("info", f"{code}: отправлен на зарядку (оператор)", code)
        return {"code": code, "action": action, "status": "charging"}

    if action == "fault":
        dev.status = "fault"
        await session.commit()
        await send_command(code, {"command": "lock"})
        await manager.broadcast({
            "type": "alert", "level": "error", "code": code,
            "message": f"Самокат {code}: зафиксирована поломка — требуется выезд",
        })
        await simulation.broadcast_status(code, "fault")
        await events.log("error", f"{code}: поломка отмечена вручную (оператор)", code)
        return {"code": code, "action": action, "status": "fault"}

    if action == "release":
        dev.status = "available"
        await session.commit()
        await send_command(code, {"command": "lock"})
        simulation.schedule(code, 3)
        await simulation.broadcast_status(code, "available")
        await events.log("info", f"{code}: возвращён в строй (оператор)", code)
        return {"code": code, "action": action, "status": "available"}

    raise HTTPException(400, f"Неизвестное действие: {action}")


class SimConfigIn(BaseModel):
    breakdown_chance: float | None = None
    repair_cooldown: int | None = None
    charge_cooldown: int | None = None
    low_battery: int | None = None


@router.get("/sim/config", dependencies=[Depends(require_admin)])
async def get_sim_config():
    return {**simulation.CONFIG, "paused": simulation.is_paused()}


@router.post("/sim/toggle", dependencies=[Depends(require_admin)])
async def toggle_sim(session: AsyncSession = Depends(get_session)):
    """Поставить симуляцию на паузу / снять с паузы.

    На паузе весь парк замораживается (lock); при возобновлении арендованные
    самокаты снова разблокируются и продолжают движение. Геозоны не затрагиваются.
    """
    paused = simulation.toggle_paused()
    rows = (await session.execute(select(Device.code, Device.status))).all()
    for r in rows:
        if paused:
            await send_command(r.code, {"command": "lock"})
        elif r.status == "in_use":
            await send_command(r.code, {"command": "unlock"})
    await events.log("warn", "Симуляция остановлена (оператор)" if paused
                     else "Симуляция возобновлена (оператор)")
    return {"paused": paused}


@router.put("/sim/config", dependencies=[Depends(require_admin)])
async def set_sim_config(body: SimConfigIn):
    if body.breakdown_chance is not None:
        simulation.CONFIG["breakdown_chance"] = max(0.0, min(1.0, body.breakdown_chance))
    if body.repair_cooldown is not None:
        simulation.CONFIG["repair_cooldown"] = max(1, body.repair_cooldown)
    if body.charge_cooldown is not None:
        simulation.CONFIG["charge_cooldown"] = max(1, body.charge_cooldown)
    if body.low_battery is not None:
        simulation.CONFIG["low_battery"] = max(0, min(100, body.low_battery))
    return simulation.CONFIG


# ─────────────────────────────── ЗОНЫ ───────────────────────────────

class ZoneIn(BaseModel):
    name: str
    zone_type: str            # parking | slow | forbidden
    speed_limit: int | None = None
    geometry: dict            # GeoJSON-полигон (как отдаёт Leaflet)


def _geom_from_geojson(geometry: dict):
    return func.ST_SetSRID(func.ST_GeomFromGeoJSON(json.dumps(geometry)), 4326)


@router.post("/zones", dependencies=[Depends(require_admin)])
async def create_zone(body: ZoneIn, session: AsyncSession = Depends(get_session)):
    zone = Zone(
        name=body.name, zone_type=body.zone_type,
        speed_limit=body.speed_limit, geom=_geom_from_geojson(body.geometry),
    )
    session.add(zone)
    await session.commit()
    await events.log("info", f"Создана зона «{zone.name}» ({zone.zone_type})")
    return {"id": zone.id, "name": zone.name, "zone_type": zone.zone_type}


class ZonePatch(BaseModel):
    name: str | None = None
    zone_type: str | None = None
    speed_limit: int | None = None
    geometry: dict | None = None


@router.put("/zones/{zone_id}", dependencies=[Depends(require_admin)])
async def update_zone(
    zone_id: int, body: ZonePatch, session: AsyncSession = Depends(get_session)
):
    values: dict = {}
    if body.name is not None:
        values["name"] = body.name
    if body.zone_type is not None:
        values["zone_type"] = body.zone_type
    if body.speed_limit is not None:
        values["speed_limit"] = body.speed_limit
    if body.geometry is not None:
        values["geom"] = _geom_from_geojson(body.geometry)
    if not values:
        raise HTTPException(400, "Нет полей для обновления")
    res = await session.execute(
        update(Zone).where(Zone.id == zone_id).values(**values)
    )
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Зона не найдена")
    return {"id": zone_id, "updated": True}


@router.delete("/zones/{zone_id}", dependencies=[Depends(require_admin)])
async def delete_zone(zone_id: int, session: AsyncSession = Depends(get_session)):
    res = await session.execute(delete(Zone).where(Zone.id == zone_id))
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Зона не найдена")
    await events.log("info", f"Удалена зона #{zone_id}")
    return {"id": zone_id, "deleted": True}


# ─────────────────────────── ВХОД ОПЕРАТОРА ───────────────────────────

class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)):
    """Вход оператора: проверка логина и хеша пароля по таблице users."""
    user = (
        await session.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user and user.password_hash and verify_password(body.password, user.password_hash):
        return {"token": settings.admin_token, "user": user.username}
    raise HTTPException(401, "Неверный логин или пароль")


# ─────────────────────────── АНАЛИТИКА ───────────────────────────

@router.get("/stats", dependencies=[Depends(require_admin)])
async def stats(session: AsyncSession = Depends(get_session)):
    """Сводная аналитика по поездкам и текущему состоянию парка."""
    # Итоги по завершённым поездкам
    totals = (
        await session.execute(
            select(
                func.count(Trip.id),
                func.coalesce(func.sum(Trip.distance_m), 0.0),
                func.coalesce(func.sum(Trip.cost), 0.0),
                func.coalesce(
                    func.avg(func.extract("epoch", Trip.ended_at - Trip.started_at)), 0.0
                ),
            ).where(Trip.ended_at.isnot(None))
        )
    ).one()

    # Разбивка по исходам
    outcome_rows = (
        await session.execute(
            select(Trip.outcome, func.count(Trip.id))
            .where(Trip.ended_at.isnot(None))
            .group_by(Trip.outcome)
        )
    ).all()
    outcomes = {(o or "completed"): c for o, c in outcome_rows}

    # Поездки по дням (последние 7)
    per_day_rows = (
        await session.execute(
            select(func.date(Trip.started_at).label("d"), func.count(Trip.id))
            .group_by(func.date(Trip.started_at))
            .order_by(func.date(Trip.started_at).desc())
            .limit(7)
        )
    ).all()
    per_day = [{"date": str(r.d), "count": r[1]} for r in reversed(per_day_rows)]

    # Текущее состояние парка
    fleet_rows = (
        await session.execute(select(Device.status, func.count(Device.id)).group_by(Device.status))
    ).all()
    fleet = {s: c for s, c in fleet_rows}

    return {
        "trips_total": totals[0],
        "distance_total_m": round(float(totals[1]), 1),
        "revenue_total": round(float(totals[2]), 2),
        "avg_duration_s": round(float(totals[3]), 1),
        "outcomes": outcomes,
        "per_day": per_day,
        "fleet": fleet,
    }


@router.get("/trips", dependencies=[Depends(require_admin)])
async def recent_trips(limit: int = 15, session: AsyncSession = Depends(get_session)):
    """Последние поездки для таблицы истории."""
    rows = (
        await session.execute(
            select(
                Trip.id, Device.code, Trip.started_at, Trip.ended_at,
                Trip.distance_m, Trip.cost, Trip.outcome,
            )
            .join(Device, Device.id == Trip.device_id)
            .where(Trip.ended_at.isnot(None))
            .order_by(Trip.ended_at.desc())
            .limit(max(1, min(100, limit)))
        )
    ).all()
    result = []
    for r in rows:
        dur = (r.ended_at - r.started_at).total_seconds() if r.ended_at and r.started_at else 0
        result.append({
            "id": r.id, "code": r.code,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "duration_s": round(dur, 0),
            "distance_m": r.distance_m, "cost": r.cost,
            "outcome": r.outcome or "completed",
        })
    return result


# ─────────────────── УЛЬТИМАТИВНЫЕ ДЕЙСТВИЯ ПО ПАРКУ ───────────────────

@router.post("/devices/repair-all", dependencies=[Depends(require_admin)])
async def repair_all(session: AsyncSession = Depends(get_session)):
    """Починить все сломанные/ремонтируемые самокаты сразу: вернуть в строй,
    эвакуировать на парковку, при заряде <50% — зарядить."""
    rows = (
        await session.execute(
            select(Device.id, Device.code, Device.status, Device.battery)
        )
    ).all()
    n = 0
    for r in rows:
        if r.status not in ("fault", "maintenance"):
            continue
        charge = r.battery < 50
        pt = await nearest_parking_for_device(session, r.id)
        vals = {"status": "available"}
        if charge:
            vals["battery"] = 100
        if pt:
            vals["geom"] = func.ST_SetSRID(func.ST_MakePoint(pt[1], pt[0]), 4326)
        await session.execute(update(Device).where(Device.id == r.id).values(**vals))
        await session.commit()
        if pt:
            await send_command(r.code, {"command": "set_position", "lat": pt[0], "lng": pt[1]})
        if charge:
            await send_command(r.code, {"command": "set_battery", "value": 100})
        await send_command(r.code, {"command": "lock"})
        simulation.schedule(r.code, 5)
        await simulation.broadcast_status(
            r.code, "available",
            battery=100 if charge else None,
            lat=pt[0] if pt else None, lng=pt[1] if pt else None,
        )
        n += 1
    await events.log("ok", f"Массовый ремонт: восстановлено {n} самокат(ов) (оператор)")
    return {"repaired": n}


@router.post("/devices/stop-all", dependencies=[Depends(require_admin)])
async def stop_all(session: AsyncSession = Depends(get_session)):
    """Аварийная остановка: завершить все поездки и заблокировать весь парк."""
    rows = (await session.execute(select(Device.id, Device.code, Device.status))).all()
    n = 0
    for r in rows:
        if r.status == "in_use":
            await trips.close_trip(session, r.code, "completed")
            n += 1
        if r.status != "fault":
            await session.execute(
                update(Device).where(Device.id == r.id).values(status="available")
            )
            await session.commit()
            await simulation.broadcast_status(r.code, "available")
        await send_command(r.code, {"command": "lock"})
        simulation.schedule(r.code, 45)  # пауза, чтобы парк не разъехался сразу
    await events.log("warn", f"Аварийная остановка парка: остановлено {n} поездок (оператор)")
    return {"stopped": n}
