"""Геозоны (geofencing) на PostGIS.

Здесь сосредоточена вся пространственная логика — это одна из двух «глубоких»
фич проекта. Используем функции PostGIS через SQLAlchemy:
- ST_MakePoint / ST_SetSRID — построить точку из (долгота, широта);
- ST_Contains              — проверить, лежит ли точка внутри полигона зоны;
- ST_DWithin (по geography) — найти устройства в радиусе N метров;
- ST_Distance (по geography) — посчитать расстояние в метрах.
"""
from dataclasses import dataclass

from geoalchemy2 import Geography
from sqlalchemy import cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Device, Zone

settings = get_settings()


def _point(lat: float, lng: float):
    """SQL-выражение точки в SRID 4326. Внимание: PostGIS ждёт (lng, lat)!"""
    return func.ST_SetSRID(func.ST_MakePoint(lng, lat), 4326)


@dataclass
class GeofenceResult:
    """Результат проверки точки относительно геозон."""
    in_forbidden: bool          # точка в запрещённой зоне
    effective_speed_limit: int  # итоговый лимит скорости в этой точке, км/ч
    zone_names: list[str]       # имена всех зон, накрывающих точку


async def evaluate_point(session: AsyncSession, lat: float, lng: float) -> GeofenceResult:
    """Главная функция: что происходит с устройством в данной точке.

    Логика:
    - если точка попала хоть в одну запрещённую зону → in_forbidden = True;
    - лимит скорости = минимум по всем медленным зонам, накрывающим точку,
      иначе глобальный дефолт (settings.default_speed_limit).
    """
    point = _point(lat, lng)

    stmt = select(Zone.zone_type, Zone.name, Zone.speed_limit).where(
        func.ST_Contains(Zone.geom, point)
    )
    rows = (await session.execute(stmt)).all()

    in_forbidden = any(r.zone_type == "forbidden" for r in rows)

    slow_limits = [
        r.speed_limit for r in rows if r.zone_type == "slow" and r.speed_limit is not None
    ]
    effective = min(slow_limits) if slow_limits else settings.default_speed_limit

    return GeofenceResult(
        in_forbidden=in_forbidden,
        effective_speed_limit=effective,
        zone_names=[r.name for r in rows],
    )


@dataclass
class NearbyDevice:
    code: str
    device_type: str
    battery: int
    lat: float
    lng: float
    distance_m: float


async def find_nearby_devices(
    session: AsyncSession, lat: float, lng: float, radius_m: float = 500.0
) -> list[NearbyDevice]:
    """Найти свободные устройства в радиусе radius_m метров от точки.

    Расстояние считается по типу geography, поэтому радиус задаётся прямо
    в метрах (а не в градусах), что корректно на поверхности Земли.
    """
    point_geog = cast(_point(lat, lng), Geography)
    device_geog = cast(Device.geom, Geography)
    distance = func.ST_Distance(device_geog, point_geog)

    stmt = (
        select(
            Device.code,
            Device.device_type,
            Device.battery,
            func.ST_Y(Device.geom).label("lat"),
            func.ST_X(Device.geom).label("lng"),
            distance.label("distance_m"),
        )
        .where(Device.status == "available")
        .where(Device.geom.isnot(None))
        .where(func.ST_DWithin(device_geog, point_geog, radius_m))
        .order_by(distance)
    )
    rows = (await session.execute(stmt)).all()
    return [
        NearbyDevice(
            code=r.code,
            device_type=r.device_type,
            battery=r.battery,
            lat=r.lat,
            lng=r.lng,
            distance_m=round(r.distance_m, 1),
        )
        for r in rows
    ]


async def zones_as_geojson(session: AsyncSession) -> list[dict]:
    """Вернуть все зоны как GeoJSON-фичи — удобно для отрисовки на карте."""
    stmt = select(
        Zone.id,
        Zone.name,
        Zone.zone_type,
        Zone.speed_limit,
        func.ST_AsGeoJSON(Zone.geom).label("geojson"),
    )
    rows = (await session.execute(stmt)).all()
    import json

    return [
        {
            "type": "Feature",
            "properties": {
                "id": r.id,
                "name": r.name,
                "zone_type": r.zone_type,
                "speed_limit": r.speed_limit,
            },
            "geometry": json.loads(r.geojson),
        }
        for r in rows
    ]


async def nearest_parking_for_device(session: AsyncSession, dev_id: int):
    """Центр ближайшей к устройству зоны парковки → (lat, lng) или None.

    Использует KNN-оператор PostGIS (geom <-> geom) для быстрого поиска
    ближайшего полигона типа parking.
    """
    stmt = text(
        """
        SELECT ST_Y(ST_Centroid(z.geom)) AS lat,
               ST_X(ST_Centroid(z.geom)) AS lng
        FROM zones z, devices d
        WHERE z.zone_type = 'parking' AND d.id = :dev_id
        ORDER BY z.geom <-> d.geom
        LIMIT 1
        """
    )
    row = (await session.execute(stmt, {"dev_id": dev_id})).first()
    if row is None or row.lat is None:
        return None
    return (row.lat, row.lng)
