"""Движок жизненного цикла самокатов (серверная «игра» парка).

Состояния и переходы:
  available (стоит на парковке, заблокирован)
      └─ через случайную паузу → in_use (аренда: разблокирован, едет)
  in_use
      ├─ с вероятностью CONFIG['breakdown_chance'] поездка завершается ПОЛОМКОЙ → fault
      └─ иначе по таймеру → обратно в available
  in_use/available + низкий заряд → разовое уведомление оператору
  fault     — ждёт оператора (кнопка «На ремонт»)
  maintenance/charging — через кулдаун → available; самокат при этом
                         ЭВАКУИРУЕТСЯ в ближайшую зону парковки (после зарядки — 100%).

CONFIG меняется на лету через /admin/sim/config.
"""
import asyncio
import logging
import random
import time

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import events
from app import trips
from app.database import SessionLocal
from app.geofencing import nearest_parking_for_device
from app.models import Device
from app.mqtt_client import send_command
from app.ws import manager

logger = logging.getLogger("sim")

CONFIG: dict[str, float] = {
    "breakdown_chance": 0.20,
    "repair_cooldown": 20,
    "charge_cooldown": 20,
    "low_battery": 15,
    "rental_min": 5,
    "rental_max": 20,
    "ride_min": 15,
    "ride_max": 40,
}

_state: dict[str, dict] = {}

# Глобальная пауза симуляции (геозоны и данные не затрагиваются)
_paused = False


def is_paused() -> bool:
    return _paused


def set_paused(value: bool) -> None:
    global _paused
    _paused = bool(value)


def toggle_paused() -> bool:
    global _paused
    _paused = not _paused
    return _paused


def _now() -> float:
    return time.monotonic()


def schedule(code: str, seconds: float) -> None:
    st = _state.setdefault(code, {})
    st["at"] = _now() + seconds
    st["low_alerted"] = False


def _rental_pause() -> float:
    return random.uniform(CONFIG["rental_min"], CONFIG["rental_max"])


async def broadcast_status(code, status, battery=None, lat=None, lng=None) -> None:
    msg = {"type": "status", "code": code, "status": status}
    if battery is not None:
        msg["battery"] = battery
    if lat is not None:
        msg["lat"] = lat
        msg["lng"] = lng
    await manager.broadcast(msg)


async def _alert(code: str, level: str, message: str) -> None:
    await manager.broadcast(
        {"type": "alert", "level": level, "code": code, "message": message}
    )


async def _set(session: AsyncSession, dev_id: int, **values) -> None:
    await session.execute(update(Device).where(Device.id == dev_id).values(**values))
    await session.commit()


async def _return_to_parking(session: AsyncSession, dev_id: int, code: str,
                             *, charge: bool):
    """Перевести устройство в available и эвакуировать в ближайшую парковку."""
    pt = await nearest_parking_for_device(session, dev_id)
    values: dict = {"status": "available"}
    if charge:
        values["battery"] = 100
    if pt:
        values["geom"] = func.ST_SetSRID(func.ST_MakePoint(pt[1], pt[0]), 4326)
    await _set(session, dev_id, **values)
    if pt:
        await send_command(code, {"command": "set_position", "lat": pt[0], "lng": pt[1]})
    if charge:
        await send_command(code, {"command": "set_battery", "value": 100})
    await send_command(code, {"command": "lock"})
    await broadcast_status(code, "available",
                           battery=100 if charge else None,
                           lat=pt[0] if pt else None, lng=pt[1] if pt else None)
    return pt


async def _tick() -> None:
    if _paused:
        return  # симуляция на паузе — переходы не выполняем
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Device.id, Device.code, Device.status, Device.battery)
            )
        ).all()
        live = set()

        for r in rows:
            live.add(r.code)
            st = _state.setdefault(
                r.code,
                {"at": _now() + _rental_pause(), "will_break": False, "low_alerted": False},
            )
            now = _now()

            if (
                r.status in ("available", "in_use")
                and r.battery <= CONFIG["low_battery"]
                and not st.get("low_alerted")
            ):
                st["low_alerted"] = True
                await _alert(r.code, "warn", f"Самокат {r.code}: низкий заряд ({r.battery}%)")
                await events.log("warn", f"{r.code}: низкий заряд ({r.battery}%)", r.code)

            if r.status == "available":
                if now >= st.get("at", 0) and r.battery > 5:
                    st["will_break"] = random.random() < CONFIG["breakdown_chance"]
                    ride = random.uniform(CONFIG["ride_min"], CONFIG["ride_max"])
                    st["at"] = now + (random.uniform(8, ride) if st["will_break"] else ride)
                    await _set(session, r.id, status="in_use")
                    await trips.open_trip(session, r.code, r.id)
                    await send_command(r.code, {"command": "unlock"})
                    await broadcast_status(r.code, "in_use")
                    await events.log("info", f"{r.code}: начата аренда", r.code)

            elif r.status == "in_use":
                if r.battery <= 0:
                    # Полная разрядка → поездка немедленно прекращается
                    await _set(session, r.id, status="available")
                    await trips.close_trip(session, r.code, "depleted")
                    await send_command(r.code, {"command": "lock"})
                    st["low_alerted"] = True
                    await _alert(r.code, "warn", f"Самокат {r.code}: разряжен до 0% — поездка прекращена")
                    await broadcast_status(r.code, "available")
                    await events.log("warn", f"{r.code}: разряжен до 0%, поездка прекращена", r.code)
                elif now >= st.get("at", 0):
                    if st.get("will_break"):
                        await _set(session, r.id, status="fault")
                        await trips.close_trip(session, r.code, "breakdown")
                        await send_command(r.code, {"command": "lock"})
                        await _alert(r.code, "error", f"Самокат {r.code}: поломка — требуется выезд")
                        await broadcast_status(r.code, "fault")
                        await events.log("error", f"{r.code}: поломка в поездке", r.code)
                    else:
                        await _set(session, r.id, status="available")
                        await trips.close_trip(session, r.code, "completed")
                        await send_command(r.code, {"command": "lock"})
                        st["at"] = now + _rental_pause()
                        st["low_alerted"] = False
                        await broadcast_status(r.code, "available")
                        await events.log("info", f"{r.code}: поездка завершена", r.code)

            elif r.status == "maintenance":
                if now >= st.get("at", 0):
                    recharge = r.battery < 50  # сломанный с низким зарядом — заряжаем при ремонте
                    await _return_to_parking(session, r.id, r.code, charge=recharge)
                    st["at"] = now + _rental_pause()
                    suffix = ", заряд восстановлен" if recharge else ""
                    await events.log("ok", f"{r.code}: ремонт завершён{suffix}, на парковке", r.code)

            elif r.status == "charging":
                if now >= st.get("at", 0):
                    await _return_to_parking(session, r.id, r.code, charge=True)
                    st["at"] = now + _rental_pause()
                    await events.log("ok", f"{r.code}: заряжен (100%), на парковке", r.code)

        for code in list(_state):
            if code not in live:
                del _state[code]


async def run_simulation(stop_event: asyncio.Event) -> None:
    logger.info("Движок симуляции запущен")
    while not stop_event.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("Ошибка тика симуляции")
        await asyncio.sleep(2)
