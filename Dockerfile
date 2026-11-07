FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DB_PATH=/data/prices.db \
    LOG_FILE=/data/monitor.log

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

# O histórico de preços é o coração do monitor: monte um volume/disco persistente em /data,
# senão ele é perdido a cada novo deploy.
RUN mkdir -p /data
VOLUME /data

CMD ["python", "main.py"]
