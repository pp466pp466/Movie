FROM python:3.12-slim

WORKDIR /app

# Keep the user's working FFmpeg, subtitle/font and app dependencies.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "psycopg[binary]>=3.2,<4"

COPY download_fonts.py ./
RUN python download_fonts.py && fc-cache -f

COPY app.py ./

EXPOSE 10000

# Keep the original single-worker setup; database now stores job state.
CMD ["sh", "-c", "gunicorn --workers 1 --threads 2 --timeout 180 --bind 0.0.0.0:${PORT:-10000} app:app"]
