"""Эмулятор устройств (СИМ) — заменяет реальное железо.

Что делает:
- периодически опрашивает бэкенд (/sim/roster) и синхронизирует список самокатов:
  добавленные оператором появляются, удалённые — исчезают;
- «ездит» разблокированными самокатами, публикует телеметрию в MQTT;
- слушает команды: unlock / lock / set_speed_limit / set_battery.

Запуск:  python -m emulator.emulator
"""
import asyncio
import json
import logging
import os
import random
import urllib.request

import aiomqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("emulator")

MQTT_HOST = os.getenv("MQTT_HOST", "mqtt")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
API_URL = os.getenv("API_URL", "http://api:8000")

TELEMETRY_INTERVAL = 2.0  # сек между публикациями телеметрии
ROSTER_INTERVAL = 5.0     # сек между опросами списка устройств


class SimDevice:
    """Состояние одного эмулируемого устройства."""

    def __init__(self, code: str, lat: float, lng: float) -> None:
        self.code = code
        self.lat = lat
        self.lng = lng
        self.battery = random.randint(70, 100)
        self.speed_limit = 25
        self.locked = True   # самокат «припаркован», пока не пришёл unlock
        self.speed = 0.0

    def step(self) -> None:
        if self.locked:
            self.speed = 0.0
            return
        self.speed = round(random.uniform(0.4, 1.0) * self.speed_limit, 1)
        delta = self.speed * 1e-5
        self.lat += random.uniform(-delta, delta)
        self.lng += random.uniform(-delta, delta)
        if random.random() < 0.3:
            self.battery = max(0, self.battery - 1)

    def telemetry(self) -> dict:
        return {
            "lat": round(self.lat, 6), "lng": round(self.lng, 6),
            "battery": self.battery, "speed": self.speed,
        }


def _fetch_roster() -> list[dict]:
    """Синхронный HTTP-запрос списка устройств (запускается в отдельном потоке)."""
    with urllib.request.urlopen(f"{API_URL}/sim/roster", timeout=5) as resp:
        return json.loads(resp.read().decode())


async def roster_sync(devices: dict[str, SimDevice]) -> None:
    """Держит набор эмулируемых устройств в соответствии с базой."""
    while True:
        try:
            roster = await asyncio.to_thread(_fetch_roster)
            codes = {d["code"] for d in roster}
            for d in roster:
                if d["code"] not in devices and d["lat"] is not None:
                    devices[d["code"]] = SimDevice(d["code"], d["lat"], d["lng"])
                    logger.info("Добавлен самокат %s", d["code"])
            for code in list(devices):
                if code not in codes:
                    del devices[code]
                    logger.info("Удалён самокат %s", code)
        except Exception as exc:
            logger.warning("Не удалось получить список устройств: %s", exc)
        await asyncio.sleep(ROSTER_INTERVAL)


async def publisher(client: aiomqtt.Client, devices: dict[str, SimDevice]) -> None:
    while True:
        for dev in list(devices.values()):
            dev.step()
            await client.publish(f"devices/{dev.code}/telemetry", json.dumps(dev.telemetry()))
        await asyncio.sleep(TELEMETRY_INTERVAL)


async def commander(client: aiomqtt.Client, devices: dict[str, SimDevice]) -> None:
    await client.subscribe("devices/+/commands")
    async for message in client.messages:
        parts = str(message.topic).split("/")
        if len(parts) != 3:
            continue
        dev = devices.get(parts[1])
        if dev is None:
            continue
        cmd = json.loads(message.payload)
        c = cmd.get("command")
        if c == "unlock":
            dev.locked = False
            logger.info("%s: разблокирован", dev.code)
        elif c == "lock":
            dev.locked = True
            logger.info("%s: заблокирован %s", dev.code, cmd.get("reason", ""))
        elif c == "set_speed_limit":
            dev.speed_limit = int(cmd["value"])
        elif c == "set_battery":
            dev.battery = max(0, min(100, int(cmd["value"])))
            logger.info("%s: заряд выставлен в %s%%", dev.code, dev.battery)
        elif c == "set_position":
            dev.lat = float(cmd["lat"]); dev.lng = float(cmd["lng"])
            logger.info("%s: перемещён на парковку", dev.code)


async def main() -> None:
    devices: dict[str, SimDevice] = {}
    asyncio.create_task(roster_sync(devices))  # синхронизация — независимо от MQTT
    while True:
        try:
            async with aiomqtt.Client(hostname=MQTT_HOST, port=MQTT_PORT) as client:
                logger.info("Эмулятор подключён к брокеру %s:%s", MQTT_HOST, MQTT_PORT)
                await asyncio.gather(
                    publisher(client, devices),
                    commander(client, devices),
                )
        except aiomqtt.MqttError as exc:
            logger.warning("Брокер недоступен (%s), повтор через 3 с", exc)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
