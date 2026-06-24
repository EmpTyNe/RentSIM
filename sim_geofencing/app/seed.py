"""Наполнение БД тестовыми данными: пользователь, устройства и три геозоны.

Координаты — центр Краснодара. Запускать после поднятия БД:  python -m app.seed
Скрипт идемпотентный: повторный запуск просто перезальёт данные заново.
"""
import asyncio
import random

from sqlalchemy import delete, func, text

from app.auth import hash_password
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.models import Device, Trip, User, Zone

settings = get_settings()

# Центр карты — Краснодар (широта, долгота)
CENTER_LAT, CENTER_LNG = 45.0355, 38.9753


def point(lat: float, lng: float):
    """Геометрия точки (SRID 4326) через функцию PostGIS — без неоднозначностей."""
    return func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326)


def polygon(points: list[tuple[float, float]]):
    """Геометрия полигона из списка (lat, lng). PostGIS ждёт пары 'lng lat'."""
    ring = ", ".join(f"{lng} {lat}" for lat, lng in points)
    first_lng, first_lat = points[0][1], points[0][0]
    ewkt = f"SRID=4326;POLYGON(({ring}, {first_lng} {first_lat}))"
    return func.ST_GeomFromEWKT(ewkt)


async def main() -> None:
    # PostGIS-расширение + таблицы (на случай, если приложение ещё не стартовало)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.run_sync(Base.metadata.create_all)

    async with SessionLocal() as session:
        # Идемпотентность: чистим старые данные (порядок важен из-за внешних ключей)
        await session.execute(delete(Trip))
        await session.execute(delete(Device))
        await session.execute(delete(Zone))
        await session.execute(delete(User))

        session.add(
            User(
                username=settings.admin_user,
                email="admin@example.com",
                password_hash=hash_password(settings.admin_password),
                balance=0.0,
            )
        )

        # ── Самокаты: разбросаны по центру и улицам Краснодара ──
        devices = [
            ("SC-001", 45.0208, 38.9690),
            ("SC-002", 45.0250, 38.9720),
            ("SC-003", 45.0158, 38.9785),
            ("SC-004", 45.0335, 38.9755),
            ("SC-005", 45.0460, 38.9530),
            ("SC-006", 45.0280, 38.9800),
            ("SC-007", 45.0250, 38.9700),
            ("SC-008", 45.0400, 38.9740),
            ("SC-009", 45.0180, 38.9720),
            ("SC-010", 45.0445, 38.9560),
        ]
        for code, lat, lng in devices:
            session.add(Device(code=code, device_type="scooter", status="available",
                               battery=random.randint(70, 100), geom=point(lat, lng)))

        # ── Геозоны Краснодара ──
        def rect(lat, lng, dlat, dlng):
            return polygon([(lat - dlat, lng - dlng), (lat - dlat, lng + dlng),
                            (lat + dlat, lng + dlng), (lat + dlat, lng - dlng)])

        zones = []

        # Парковки — случайно раскиданы по улицам центральной части города
        for i in range(8):
            lat = random.uniform(45.014, 45.048)
            lng = random.uniform(38.957, 38.993)
            zones.append((f"Парковка СИМ №{i + 1}", "parking", None, lat, lng, 0.0005, 0.0006))

        # Медленные зоны — парки, скверы, пешеходные оси (name, limit, lat, lng, dlat, dlng)
        for name, limit, lat, lng, dlat, dlng in [
            ("Городской сад", 12, 45.0158, 38.9785, 0.0016, 0.0016),
            ("Чистяковская роща", 12, 45.0460, 38.9530, 0.0020, 0.0022),
            ("Парк 30-летия Победы", 12, 45.0445, 38.9560, 0.0016, 0.0018),
            ("Сквер у к/т «Аврора»", 10, 45.0345, 38.9760, 0.0009, 0.0010),
            ("Театральная площадь", 10, 45.0205, 38.9695, 0.0010, 0.0011),
            ("Пешеходная ул. Красная", 10, 45.0300, 38.9742, 0.0140, 0.0006),
            ("Кубанская набережная", 12, 45.0135, 38.9760, 0.0010, 0.0060),
        ]:
            zones.append((name, "slow", limit, lat, lng, dlat, dlng))

        # Запрещённые зоны — кладбища, парк Галицкого, Солнечный остров и др.
        for name, lat, lng, dlat, dlng in [
            ("Парк «Краснодар» (Галицкого)", 45.0290, 38.9975, 0.0024, 0.0024),
            ("Солнечный остров", 45.0235, 39.0250, 0.0026, 0.0030),
            ("Всесвятское кладбище", 45.0255, 38.9595, 0.0014, 0.0016),
            ("Славянское кладбище", 45.0725, 38.9440, 0.0022, 0.0026),
            ("Привокзальная пл. (вокзал)", 45.0210, 39.0010, 0.0010, 0.0012),
            ("Пойма у реки", 45.0100, 38.9850, 0.0014, 0.0020),
        ]:
            zones.append((name, "forbidden", None, lat, lng, dlat, dlng))

        for name, ztype, limit, lat, lng, dlat, dlng in zones:
            session.add(Zone(name=name, zone_type=ztype, speed_limit=limit,
                             geom=rect(lat, lng, dlat, dlng)))

        await session.commit()
    print(f"Готово: загружено {len(devices)} самокатов и {len(zones)} зон.")


if __name__ == "__main__":
    asyncio.run(main())
