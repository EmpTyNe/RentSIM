# Запуск под Windows

Проект кроссплатформенный. Есть два пути; для «продуктового» развёртывания
рекомендуется **путь A (Docker Desktop)** — одинаков на Windows, macOS и Linux.

---

## Путь A — Docker Desktop (рекомендуется)

1. Установите **Docker Desktop for Windows** (с официального сайта docker.com).
   Для Windows 10/11 он использует WSL2 — мастер установки включит его сам.
2. В корне проекта (PowerShell):

   ```powershell
   docker compose up --build
   ```

   Поднимутся 4 контейнера: PostgreSQL+PostGIS, Mosquitto, API, эмулятор.
3. Один раз наполните БД тестовыми данными:

   ```powershell
   docker compose exec api python -m app.seed
   ```
4. Откройте панель оператора: **http://localhost:8000/admin**
   (токен по умолчанию — `admin-secret`).

Остановить: `docker compose down` (добавьте `-v`, чтобы стереть данные БД).

---

## Путь B — нативно, без Docker

Подходит, если Docker ставить нельзя. Нужно поставить три компонента вручную.

### 1. Python 3.12
Установите с python.org, при установке отметьте **Add Python to PATH**.

### 2. PostgreSQL + PostGIS
- Установите PostgreSQL (установщик EDB). В конце он предложит **Stack Builder** —
  через него поставьте расширение **PostGIS**.
- Создайте БД и пользователя (psql или pgAdmin):

  ```sql
  CREATE USER mobility WITH PASSWORD 'mobility';
  CREATE DATABASE mobility OWNER mobility;
  ```
  Расширение PostGIS в БД включит сам скрипт `app/seed.py` (`CREATE EXTENSION`).

### 3. Mosquitto (MQTT-брокер)
- Установите **Eclipse Mosquitto for Windows** (mosquitto.org/download).
- Запустите с конфигом из проекта (разрешает анонимные подключения на 1883):

  ```powershell
  mosquitto.exe -c mosquitto\mosquitto.conf -v
  ```
  Либо запустите Mosquitto как службу Windows.

### 4. Настройка и запуск
- Переименуйте `.env.local` → `.env` (там `DB_HOST=localhost`, `MQTT_HOST=localhost`).
- Запустите скрипт (поднимет venv, зависимости, сиды, эмулятор и API):

  ```powershell
  powershell -ExecutionPolicy Bypass -File windows\run-local.ps1
  ```
- Панель: **http://localhost:8000/admin**

---

## Что увидит оператор

- Тёмная карта города, на ней — все самокаты живыми маркерами (двигаются от телеметрии);
- цвет маркера = статус (свободен / в аренде / ремонт);
- геозоны полигонами: красная — запрещённая, жёлтая — медленная, зелёная — парковка;
- слева список устройств с зарядом и статусом, сверху — счётчики парка;
- клик по самокату → карточка с кнопками: разблокировать / заблокировать /
  на ремонт / вернуть в строй (команды уходят устройству через MQTT).
