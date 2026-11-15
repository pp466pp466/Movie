
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
    "/detect-scenes"
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

        data = json.loads(plan_text)

        clips = (
            data.get("edit_plan")
            if isinstance(data, dict)
            else data
        )

        if (
            not isinstance(clips, list)
            or not 1 <= len(clips) <= 20
        ):
            return jsonify({
                "error": "剪輯表必須包含 1 至 20 筆片段"
            }), 400

        with TemporaryDirectory() as tmp:

            folder = Path(tmp)
            source = folder / "source.mp4"

            video.save(source)

            # 取得原始影片長度
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source)
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )

            duration = float(
                probe.stdout.strip()
            )

            names = []

            # 按照剪輯表逐段處理
            for i, clip in enumerate(clips, 1):

                start = clip.get("start_sec")
                end = clip.get("end_sec")

                if (
                    type(start) not in (int, float)
                    or type(end) not in (int, float)
                    or not math.isfinite(start)
                    or not math.isfinite(end)
                    or not 0 <= start < end <= duration
                ):
                    return jsonify({
                        "error": f"片段 {i} 時間碼不合法"
                    }), 400

                name = f"clip_{i:03d}.mp4"
                target = folder / name

                run_ffmpeg([
                    "ffmpeg", "-y",
                    "-i", str(source),
                    "-ss", str(start),
                    "-t", str(end - start),

                    # 保留原始畫面及存在時的音軌
                    "-map", "0:v:0",
                    "-map", "0:a:0?",

                    # 高畫質輸出
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "18",
                    "-pix_fmt", "yuv420p",

                    # 音訊轉 AAC
                    "-c:a", "aac",
                    "-b:a", "192k",

                    str(target)
                ])

                names.append(name)

            # 建立影片拼接清單
            concat = folder / "concat.txt"

            concat.write_text(
                "\n".join(
                    f"file '{name}'"
                    for name in names
                ) + "\n",
                encoding="utf-8"
            )

            output = folder / "output.mp4"

            # 合併剪輯片段
            run_ffmpeg([
                "ffmpeg", "-y",
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat),
                "-c", "copy",
                str(output)
            ])

            return send_file(
                BytesIO(output.read_bytes()),
                mimetype="video/mp4",
                as_attachment=True,
                download_name="output.mp4"
            )

    except (ValueError, KeyError, TypeError):

        return jsonify({
            "error": "影片或剪輯表資料無效"
        }), 400

    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired
    ):

        return jsonify({
            "error": "FFmpeg 影片處理失敗"
        }), 500


# ==========================================
# MP4 影格擷取 API
#
# 支援：
# 1. 手動指定 timestamps
# 2. 自動使用 sample_count
#
# 兩種模式擇一
# ==========================================

@app.post("/extract-frames")
def extract_frames():

    video = request.files.get("video")

    timestamps_text = request.form.get(
        "timestamps"
    )

    sample_count_text = request.form.get(
        "sample_count", "6"
    )

    # 必須上傳影片
    if not video:

        return jsonify({
            "error": "需要 video"
        }), 400

    try:

        # ----------------------------------
        # 模式 A：手動指定時間碼
        # ----------------------------------

        if timestamps_text:

            timestamps = json.loads(
                timestamps_text
            )

            if (
                not isinstance(timestamps, list)
                or not 1 <= len(timestamps) <= 6
                or any(
                    type(t) not in (int, float)
                    or not math.isfinite(t)
                    or t < 0
                    for t in timestamps
                )
            ):

                return jsonify({
                    "error":
                    "timestamps 必須是 1 至 6 個非負秒數"
                }), 400

            mode = "manual"

        # ----------------------------------
        # 模式 B：自動抽幀
        # ----------------------------------

        else:

            if not sample_count_text.isdecimal():

                return jsonify({
                    "error":
                    "sample_count 必須是 1 至 6 的整數"
                }), 400

            sample_count = int(
                sample_count_text
            )

            if not 1 <= sample_count <= 6:

                return jsonify({
                    "error":
                    "sample_count 必須介於 1 至 6"
                }), 400

            timestamps = None

            mode = "auto"

        # ----------------------------------
        # 儲存影片暫存檔
        # ----------------------------------

        with TemporaryDirectory() as tmp:

            folder = Path(tmp)

            source = folder / "source.mp4"

            video.save(source)

            # ----------------------------------
            # FFprobe 取得影片長度
            # ----------------------------------

            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source)
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )

            duration = float(
                probe.stdout.strip()
            )

            if (
                not math.isfinite(duration)
                or duration <= 0
            ):

                return jsonify({
                    "error": "無法取得有效影片長度"
                }), 400

            # ----------------------------------
            # 自動計算時間碼
            # ----------------------------------

            if timestamps is None:

                timestamps = []

                for i in range(sample_count):

                    # 均勻分布於每個取樣區間的中央
                    timestamp = (
                        duration
                        * (i + 0.5)
                        / sample_count
                    )

                    # 避免四捨五入超出影片長度
                    timestamp = min(
                        timestamp,
                        math.nextafter(duration, 0.0)
                    )

                    timestamps.append(
                        timestamp
                    )

            # ----------------------------------
            # 時間碼範圍檢查
            # ----------------------------------

            if any(
                t < 0 or t >= duration
                for t in timestamps
            ):

                return jsonify({
                    "error": "抽幀時間超出影片長度"
                }), 400

            # ----------------------------------
            # FFmpeg 擷取影格
            # ----------------------------------

            frames = []

            for i, timestamp in enumerate(
                timestamps
            ):

                output = (
                    folder
                    / f"frame_{i:03d}.jpg"
                )

                run_ffmpeg([
                    "ffmpeg", "-y",

                    "-ss", str(timestamp),

                    "-i", str(source),

                    "-frames:v", "1",

                    # 測試階段使用 640px 寬度
                    "-vf", "scale=640:-2",

                    # JPG 品質
                    "-q:v", "4",

                    str(output)
                ])

                # ----------------------------------
                # 確認 JPG 並轉 Base64
                # ----------------------------------

                raw = output.read_bytes()

                if not raw.startswith(
                    b"\xff\xd8"
                ):

                    raise ValueError(
                        "抽幀結果不是 JPEG"
                    )

                encoded = base64.b64encode(
                    raw
                ).decode("ascii")

                frames.append({

                    "timestamp_sec": round(
                        timestamp, 3
                    ),

                    "mimeType": "image/jpeg",

                    "imageDataUrl":
                        "data:image/jpeg;base64,"
                        + encoded
                })

            # ----------------------------------
            # API 回傳結果
            # ----------------------------------

            return jsonify({

                "status": "success",

                "mode": mode,

                "duration_sec": duration,

                "frame_count": len(frames),

                "frames": frames
            })

    except (
        ValueError,
        TypeError,
        json.JSONDecodeError
    ):

        return jsonify({
            "error": "影片或時間碼資料無效"
        }), 400

    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        OSError
    ):

        return jsonify({
            "error": "FFmpeg 抽幀失敗"
        }), 500


# ==========================================
# FFmpeg 自動場景切換偵測 API
# POST /detect-scenes
# ==========================================

@app.post("/detect-scenes")
def detect_scenes():

    import re

    video = request.files.get("video")

    if not video:
        return jsonify({
            "error": "需要上傳 video"
        }), 400

    # 場景變化偵測門檻
    # 越低越敏感，可能偵測到更多切換
    try:
        threshold = float(
            request.form.get("threshold", "0.35")
        )
    except (TypeError, ValueError):
        return jsonify({
            "error": "threshold 必須是數字"
        }), 400

    if (
        not math.isfinite(threshold)
        or not 0.1 <= threshold <= 0.8
    ):
        return jsonify({
            "error": "threshold 必須介於 0.1 至 0.8"
        }), 400

    try:
        with TemporaryDirectory() as tmp:

            folder = Path(tmp)
            source = folder / "source.mp4"

            video.save(source)

            # 取得影片長度
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v", "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(source)
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=30
            )

            duration = float(
                probe.stdout.strip()
            )

            if (
                not math.isfinite(duration)
                or duration <= 0
            ):
                return jsonify({
                    "error": "影片長度無效"
                }), 400

            # 使用 FFmpeg select + showinfo
            # 找出畫面明顯變化的時間點
            scene_filter = (
                "scale=320:-2,"
                f"select='gt(scene,{threshold})',"
                "showinfo"
            )

            result = subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel", "info",
                    "-i", str(source),
                    "-vf", scene_filter,
                    "-an",
                    "-f", "null",
                    "-"
                ],
                capture_output=True,
                text=True,
                timeout=180,
                check=True
            )

            # showinfo 會輸出 pts_time
            matches = re.findall(
                r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)",
                result.stderr
            )

            # 依時間排序並去除重複
            boundaries = [0.0]

            for value in matches:
                timestamp = float(value)

                if (
                    0 < timestamp < duration
                    and timestamp > boundaries[-1] + 0.001
                ):
                    boundaries.append(
                        round(timestamp, 3)
                    )

            boundaries.append(
                round(duration, 3)
            )

            # 轉換成連續場景區間
            scenes = []

            for i in range(len(boundaries) - 1):

                start = boundaries[i]
                end = boundaries[i + 1]

                scenes.append({
                    "scene_index": i,
                    "start_sec": start,
                    "end_sec": end,
                    "duration_sec": round(
                        end - start, 3
                    )
                })

            return jsonify({
                "status": "success",
                "duration_sec": duration,
                "threshold": threshold,
                "scene_count": len(scenes),
                "scene_boundaries": boundaries,
                "scenes": scenes
            })

    except subprocess.TimeoutExpired:
        return jsonify({
            "error": "場景偵測逾時"
        }), 504

    except (
        subprocess.CalledProcessError,
        OSError,
        ValueError
    ):
        return jsonify({
            "error": "FFmpeg 場景偵測失敗"
        }), 500

# ==========================================
# Flask 本機啟動
# Render 正式環境由 Gunicorn 啟動
# ==========================================

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
