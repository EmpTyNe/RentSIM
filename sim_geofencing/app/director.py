"""Режиссёр симуляции — автоматический жизненный цикл самокатов.

Раз в TICK секунд просматривает все устройства и двигает их по состоянию:

  available (на парковке, стоит)
      │  через случайный интервал «кто-то арендует»
      ▼
  in_use (едет)
      ├─ с шансом CONFIG["break_chance"] → fault (поломка + уведомление)
      └─ иначе по истечении времени поездки → снова available
  fault     → ждёт, пока оператор нажмёт «На ремонт» → maintenance
  maintenance → через repair_cooldown_s → available
  charging    → через charge_cooldown_s → available + заряд 100%

Низкий заряд (≤ порога) во время работы даёт разовое уведомление, но самокат
продолжает ездить — как и просили.

Переходы и команды эмулятору (lock/unlock) держат движение в синхроне со статусом:
parked/fault/maintenance/charging → lock (стоит), in_use → unlock (едет).
"""
import asyncio
import logging
import random
from datetime import timedelta

from sqlalchemy import select, update

from app.config import get_settings
from app.database import SessionLocal
from app.models import Device, utcnow
from app.mqtt_client import send_command
from app.ws import manager

logger = logging.getLogger("director")
settings = get_settings()

TICK = 2.0  # секунды между тактами симуляции

# Параметры, настраиваемые в рантайме (через /admin/config)
CONFIG = {
    "break_chance": settings.break_chance,
    "repair_cooldown_s": settings.repair_cooldown_s,
    "charge_cooldown_s": settings.charge_cooldown_s,
    "low_battery_threshold": settings.low_battery_threshold,
}

# Переходные таймеры на каждое устройство (в памяти процесса)
_state: dict[str, dict] = {}


def _st(code: str) -> dict:
    return _state.setdefault(
        code,
        {
            "next_rent_at": None,   # когда стартует аренда (для available)
            "ride_end_at": None,    # когда завершится обычная поездка
            "break_at": None,       # когда сломается (если поездка «ломучая»)
            "will_break": False,
            "low_alerted": False,   # уже уведомили о низком заряде?
            "cooldown_until": None, # конец ремонта/зарядки
        },
    )


def note_admin_action(code: str, action: str) -> None:
    """Сообщить режиссёру о ручном действии оператора (сброс таймеров)."""
    s = _st(code)
    s["next_rent_at"] = s["ride_end_at"] = s["break_at"] = None
    s["will_break"] = False
    if action in ("maintenance", "charging"):
        s["cooldown_until"] = None   # режиссёр запустит свежий кулдаун
    elif action == "set_battery":
        s["low_alerted"] = False     # дать уведомлению сработать на новый уровень


def _delay() -> timedelta:
    return timedelta(seconds=random.randint(settings.rent_min_delay_s, settings.rent_max_delay_s))


async def _tick() -> None:
    now = utcnow()
    events: list[dict] = []

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Device.id, Device.code, Device.status, Device.battery)
            )
        ).all()

        for r in rows:
            s = _st(r.code)

            # Уведомление о низком заряде (один раз; самокат продолжает работать)
            if (
                r.status != "charging"
                and r.battery <= CONFIG["low_battery_threshold"]
                and not s["low_alerted"]
            ):
                s["low_alerted"] = True
                events.append({
                    "type": "alert", "level": "warning", "code": r.code,
                    "message": f"Самокат {r.code}: низкий заряд ({r.battery}%)",
                })

            if r.status == "available":
                if s["next_rent_at"] is None:
                    s["next_rent_at"] = now + _delay()
                elif now >= s["next_rent_at"]:
                    # Старт аренды
                    s["next_rent_at"] = None
                    s["will_break"] = random.random() < CONFIG["break_chance"]
                    if s["will_break"]:
                        s["break_at"] = now + timedelta(seconds=random.randint(5, 12))
                        s["ride_end_at"] = None
                    else:
                        s["ride_end_at"] = now + timedelta(seconds=random.randint(10, 25))
                        s["break_at"] = None
                    await session.execute(
                        update(Device).where(Device.id == r.id).values(status="in_use")
                    )
                    await send_command(r.code, {"command": "unlock"})
                    events.append({"type": "status", "code": r.code, "status": "in_use", "battery": r.battery})

            elif r.status == "in_use":
                if s["will_break"] and s["break_at"] and now >= s["break_at"]:
                    s["break_at"] = None
                    s["will_break"] = False
                    await session.execute(
                        update(Device).where(Device.id == r.id).values(status="fault")
                    )
                    await send_command(r.code, {"command": "lock"})
                    events.append({"type": "status", "code": r.code, "status": "fault", "battery": r.battery})
                    events.append({
                        "type": "alert", "level": "error", "code": r.code,
                        "message": f"Самокат {r.code}: зафиксирована поломка — требуется выезд",
                    })
                elif (not s["will_break"]) and s["ride_end_at"] and now >= s["ride_end_at"]:
                    s["ride_end_at"] = None
                    s["next_rent_at"] = now + _delay()
                    await session.execute(
                        update(Device).where(Device.id == r.id).values(status="available")
                    )
                    await send_command(r.code, {"command": "lock"})
                    events.append({"type": "status", "code": r.code, "status": "available", "battery": r.battery})

            elif r.status == "maintenance":
                if s["cooldown_until"] is None:
                    s["cooldown_until"] = now + timedelta(seconds=CONFIG["repair_cooldown_s"])
                elif now >= s["cooldown_until"]:
                    s["cooldown_until"] = None
                    s["low_alerted"] = False
                    s["next_rent_at"] = now + _delay()
                    await session.execute(
                        update(Device).where(Device.id == r.id).values(status="available")
                    )
                    await send_command(r.code, {"command": "lock"})
                    events.append({"type": "status", "code": r.code, "status": "available", "battery": r.battery})

            elif r.status == "charging":
                if s["cooldown_until"] is None:
                    s["cooldown_until"] = now + timedelta(seconds=CONFIG["charge_cooldown_s"])
                elif now >= s["cooldown_until"]:
                    s["cooldown_until"] = None
                    s["low_alerted"] = False
                    s["next_rent_at"] = now + _delay()
                    await session.execute(
                        update(Device)
                        .where(Device.id == r.id)
                        .values(status="available", battery=100)
                    )
                    await send_command(r.code, {"command": "set_battery", "value": 100})
                    await send_command(r.code, {"command": "lock"})
                    events.append({"type": "status", "code": r.code, "status": "available", "battery": 100})

            # status == "fault" → ждём действия оператора, ничего не делаем

        await session.commit()

    # Рассылаем события на дашборд уже после фиксации в БД
    for ev in events:
        await manager.broadcast(ev)


async def run_director(stop_event: asyncio.Event) -> None:
    """Фоновая задача симуляции жизненного цикла."""
    while not stop_event.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("Ошибка в такте симуляции")
        await asyncio.sleep(TICK)
