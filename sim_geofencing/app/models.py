"""ORM-модели предметной области.

Геометрия хранится в PostGIS (SRID 4326 — обычные широта/долгота WGS84):
- Device.geom  — точка (текущее местоположение устройства);
- Zone.geom    — полигон (граница геозоны).
"""
from datetime import datetime, timezone

from geoalchemy2 import Geometry
from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Допустимые статусы устройства (включая «поломку» для демо)
DEVICE_STATUSES = ("available", "in_use", "charging", "maintenance", "fault")
# Типы геозон: парковка / медленная зона / запрещённая зона
ZONE_TYPES = ("parking", "slow", "forbidden")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    balance: Mapped[float] = mapped_column(Float, default=0.0)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    device_type: Mapped[str] = mapped_column(String(16), default="scooter")
    status: Mapped[str] = mapped_column(String(16), default="available")
    battery: Mapped[int] = mapped_column(Integer, default=100)  # заряд, %
    # Текущий разрешённый лимит скорости (км/ч) — пересчитывается по геозонам
    speed_limit: Mapped[int] = mapped_column(Integer, default=25)
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    # Точка местоположения. spatial_index=True создаёт GiST-индекс для гео-запросов.
    geom = mapped_column(
        Geometry(geometry_type="POINT", srid=4326, spatial_index=True),
        nullable=True,
    )


class Zone(Base):
    __tablename__ = "zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    zone_type: Mapped[str] = mapped_column(String(16))
    # Лимит скорости для медленной зоны (км/ч). Для остальных типов — NULL.
    speed_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    geom = mapped_column(
        Geometry(geometry_type="POLYGON", srid=4326, spatial_index=True)
    )


class Trip(Base):
    __tablename__ = "trips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Исход поездки: completed | breakdown | violation
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    distance_m: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    # Маршрут как список точек [[lng, lat], ...] — заполняется из телеметрии.
    path: Mapped[list] = mapped_column(JSONB, default=list)

    device: Mapped["Device"] = relationship()


class Operator(Base):
    """Оператор (учётная запись для входа). Пароль хранится в виде хеша."""
    __tablename__ = "operators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
