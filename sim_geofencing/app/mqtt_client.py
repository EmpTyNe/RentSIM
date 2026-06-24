"""MQTT-мост между бэкендом и (эмулированными) устройствами.

Это вторая «глубокая» фича — связь с IoT, как в реальном сервисе кикшеринга.

Топики:
  devices/{code}/telemetry  — устройство → бэкенд (координаты, заряд, скорость);
  devices/{code}/commands   — бэкенд → устройство (lock/unlock, set_speed_limit).

Поток обработки одной телеметрии:
  1. распарсить координаты и заряд;
  2. прогнать точку через геозоны (app.geofencing.evaluate_point);
  3. обновить устройство в БД (позиция, заряд, текущий лимит скорости);
  4. при необходимости отправить команду устройству:
       - попало в запрещённую зону во время поездки → команда lock;
       - сменился лимит скорости → команда set_speed_limit;
  5. транслировать обновление в WebSocket для live-карты.
"""
import asyncio
import json
import logging

import aiomqtt
from sqlalchemy import func, select, update

from app.config import get_settings
from app.database import SessionLocal
from app.geofencing import evaluate_point, nearest_parking_for_device
from app.models import Device, utcnow
from app.ws import manager
from app import events
from app import trips

logger = logging.getLogger("mqtt")
settings = get_settings()

TELEMETRY_TOPIC = "devices/+/telemetry"


def _command_topic(code: str) -> str:
    return f"devices/{code}/commands"


async def send_command(code: str, command: dict) -> None:
    """Разовая публикация команды устройству (для REST/админки)."""
    async with aiomqtt.Client(
        hostname=settings.mqtt_host, port=settings.mqtt_port
    ) as client:
        await client.publish(_command_topic(code), json.dumps(command))


async def _handle_telemetry(client: aiomqtt.Client, code: str, data: dict) -> None:
    lat = float(data["lat"])
    lng = float(data["lng"])
    battery = int(data.get("battery", 100))
    speed = float(data.get("speed", 0.0))

    async with SessionLocal() as session:
        # Берём только нужные поля (не трогаем geom на чтении — меньше точек отказа)
        row = (
            await session.execute(
                select(Device.id, Device.status).where(Device.code == code)
            )
        ).one_or_none()
        if row is None:
            # Устройства ещё нет в БД (например, не запускали seed) — тихо пропускаем
            return
        device_id, status = row.id, row.status

        # 1) Геозоны: что разрешено в этой точке
        fence = await evaluate_point(session, lat, lng)

        # 2) Обновляем состояние устройства в БД.
        #    geom задаём через ST_SetSRID(ST_MakePoint(...)) — корректное
        #    геометрическое выражение без неоднозначностей с типом text.
        await session.execute(
            update(Device)
            .where(Device.id == device_id)
            .values(
                geom=func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326),
                battery=battery,
                speed_limit=fence.effective_speed_limit,
                last_seen=utcnow(),
            )
        )
        await session.commit()

        was_in_use = status == "in_use"

        # Копим маршрут активной поездки для расчёта дистанции
        if was_in_use:
            trips.add_point(code, lat, lng)

        # Нарушение: въезд в запрещённую зону во время аренды.
        # Поездка прекращается, самокат эвакуируется на ближайшую парковку.
        if was_in_use and fence.in_forbidden:
            pt = await nearest_parking_for_device(session, device_id)
            vals = {"status": "available"}
            if pt:
                vals["geom"] = func.ST_SetSRID(func.ST_MakePoint(pt[1], pt[0]), 4326)
            await session.execute(
                update(Device).where(Device.id == device_id).values(**vals)
            )
            await trips.close_trip(session, code, "violation")
            await session.commit()
            if pt:
                await client.publish(_command_topic(code),
                                     json.dumps({"command": "set_position", "lat": pt[0], "lng": pt[1]}))
            await client.publish(_command_topic(code),
                                 json.dumps({"command": "lock", "reason": "forbidden_zone"}))
            await manager.broadcast({
                "type": "alert", "level": "error", "code": code,
                "message": f"Самокат {code}: въезд в запрещённую зону — поездка прекращена",
            })
            await manager.broadcast({
                "type": "status", "code": code, "status": "available",
                **({"lat": pt[0], "lng": pt[1]} if pt else {}),
            })
            await events.log("error", f"{code}: въезд в запрещённую зону — эвакуация на парковку", code)
            return  # дальнейшую обработку телеметрии пропускаем
    await client.publish(
        _command_topic(code),
        json.dumps(
            {"command": "set_speed_limit", "value": fence.effective_speed_limit}
        ),
    )

    # 4) Трансляция в live-карту
    await manager.broadcast(
        {
            "type": "telemetry",
            "code": code,
            "lat": lat,
            "lng": lng,
            "battery": battery,
            "speed": speed,
            "speed_limit": fence.effective_speed_limit,
            "in_forbidden": fence.in_forbidden,
            "zones": fence.zone_names,
        }
    )


async def run_mqtt_bridge(stop_event: asyncio.Event) -> None:
    """Фоновая задача: держит соединение с брокером и обрабатывает телеметрию.

    При обрыве связи переподключается с паузой.
    """
    while not stop_event.is_set():
        try:
            async with aiomqtt.Client(
                hostname=settings.mqtt_host, port=settings.mqtt_port
            ) as client:
                await client.subscribe(TELEMETRY_TOPIC)
                logger.info("MQTT-мост подключён, подписка на %s", TELEMETRY_TOPIC)
                async for message in client.messages:
                    # topic вида devices/<code>/telemetry
                    parts = str(message.topic).split("/")
                    if len(parts) != 3:
                        continue
                    code = parts[1]
                    try:
                        data = json.loads(message.payload)
                        await _handle_telemetry(client, code, data)
                    except Exception:
                        logger.exception("Ошибка обработки телеметрии %s", code)
        except aiomqtt.MqttError as exc:
            logger.warning("MQTT недоступен (%s), повтор через 3 c", exc)
            await asyncio.sleep(3)
