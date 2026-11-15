from flask import Flask, request, jsonify, send_file
from pathlib import Path
from tempfile import TemporaryDirectory
from io import BytesIO
import subprocess
import json
import math
import os
import hmac
import base64


app = Flask(__name__)

# 最大上傳檔案大小：80 MB
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024


# ==========================================
# API Key 驗證
# ==========================================

@app.before_request
def check_api_key():

    # 只保護影片剪輯與抽幀 API
    if request.path not in (
        "/edit",
        "/extract-frames",
        "/detect-scenes",
    ):
        return

    expected = os.environ.get(
        "VIDEO_EDITOR_API_KEY", ""
    )

    received = request.headers.get(
        "X-Editor-Key", ""
    )

    if (
        not expected
        or not hmac.compare_digest(
            received, expected
        )
    ):
        return jsonify({
            "error": "Unauthorized"
        }), 401


# ==========================================
# FFmpeg 共用執行函式
# ==========================================

def run_ffmpeg(command):
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=180
    )


# ==========================================
# 健康檢查 API
# ==========================================
@app.get("/health")
def health():

    return jsonify({
        "status": "ok",
        "service": "video-editor"
    })


# ==========================================
# 既有影片剪輯 API
# 保留原本成功的剪輯邏輯
# ==========================================

@app.post("/edit")
def edit_video():

    video = request.files.get("video")
    plan_text = request.form.get("edit_plan")

    if not video or not plan_text:
        return jsonify({
            "error": "需要 video 和 edit_plan"
        }), 400

    try:
