FROM python:3.12-slim

WORKDIR /app

# FFmpeg: 音訊、影片與字幕；fontconfig: 字型選取
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fontconfig ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 重要：把字型下載程式放進映像檔並且在 Docker build 階段執行
COPY download_fonts.py ./
RUN python download_fonts.py && fc-cache -f

COPY app.py ./

EXPOSE 10000

CMD ["sh", "-c", "gunicorn --workers 1 --threads 2 --timeout 180 --bind 0.0.0.0:${PORT:-10000} app:app"]
