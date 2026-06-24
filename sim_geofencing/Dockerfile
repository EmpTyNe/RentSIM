FROM python:3.12-slim

WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY emulator ./emulator

# По умолчанию запускаем API; эмулятор переопределяет команду в compose
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
