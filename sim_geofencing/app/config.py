"""Конфигурация приложения. Значения берутся из переменных окружения (.env)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- База данных (PostgreSQL + PostGIS) ---
    db_host: str = "db"
    db_port: int = 5432
    db_name: str = "mobility"
    db_user: str = "mobility"
    db_password: str = "mobility"

    # --- MQTT-брокер ---
    mqtt_host: str = "mqtt"
    mqtt_port: int = 1883

    # --- Доступ администратора (поменяйте в проде!) ---
    admin_token: str = "admin-secret"
    admin_user: str = "admin"
    admin_password: str = "admin"

    # --- Автосценарий («жизнь» парка) для демонстрации ---
    sim_enabled: bool = True
    sim_tick_seconds: float = 3.0       # как часто пересчитывается состояние
    sim_rent_chance: float = 0.25       # шанс, что свободный самокат начнёт поездку (за тик)
    sim_breakdown_chance: float = 0.20  # шанс поломки за поездку (20% по умолчанию)
    sim_repair_cooldown: float = 20.0   # сек ремонта до возврата в строй

    # --- Параметры тарифа (для расчёта стоимости поездки) ---
    tariff_unlock_price: float = 50.0       # цена старта, руб.
    tariff_price_per_minute: float = 8.0    # поминутная цена, руб./мин

    # --- Геозоны: дефолтный лимит скорости вне медленных зон, км/ч ---
    default_speed_limit: int = 25

    # --- Авто-симуляция жизненного цикла самоката ---
    break_chance: float = 0.2        # шанс поломки за поездку (0..1), настраивается
    repair_cooldown_s: int = 20      # кулдаун ремонта, сек
    charge_cooldown_s: int = 20      # кулдаун зарядки, сек
    low_battery_threshold: int = 20  # порог низкого заряда для уведомления, %
    rent_min_delay_s: int = 5        # мин. простой на парковке до аренды, сек
    rent_max_delay_s: int = 15       # макс. простой на парковке до аренды, сек

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
