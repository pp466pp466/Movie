
FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 10000

CMD ["sh", "-c", "gunicorn --workers 1 --threads 2 --timeout 180 --bind 0.0.0.0:${PORT:-10000} app:app"]
