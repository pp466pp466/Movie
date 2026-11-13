from flask import Flask, request, jsonify, send_file
from pathlib import Path
from tempfile import TemporaryDirectory
from io import BytesIO
import subprocess
import json
import math

import os
import hmac

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 80 * 1024 * 1024

@app.before_request
def check_api_key():
    if request.path != "/edit":
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

def run_ffmpeg(command):
    subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=180
    )

@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "video-editor"
    })

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

        if not isinstance(clips, list) or not 1 <= len(clips) <= 20:
            return jsonify({
                "error": "剪輯表必須包含 1 至 20 筆片段"
            }), 400

        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / "source.mp4"
            video.save(source)

            probe = subprocess.run([
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(source)
            ], capture_output=True, text=True,
               check=True, timeout=30)

            duration = float(probe.stdout.strip())

            names = []

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
                    "-map", "0:v:0", "-an",
                    "-vf", "fps=30,scale=1280:-2,setsar=1",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    str(target)
                ])

                names.append(name)

            concat = folder / "concat.txt"
            concat.write_text(
                "\n".join(
                    f"file '{name}'" for name in names
                ) + "\n",
                encoding="utf-8"
            )

            output = folder / "output.mp4"

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

    except (subprocess.CalledProcessError,
            subprocess.TimeoutExpired):
        return jsonify({
            "error": "FFmpeg 影片處理失敗"
        }), 500

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
