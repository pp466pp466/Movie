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
        "/tts",
        "/audio-info",
        "/concat-audio",
        "/mix-narration",
        "/burn-subtitles",
        "/drive-file-info",
        "/drive-range-test",
        "/drive-scenes-job",
        "/drive-segment-job",
        "/drive-extract-frames",
        "/drive-extract-audio",
        "/drive-edit-preview",
        "/tts-timeline",
        "/tts-story-preview",
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
        timeout=600
    )



# ==========================================
# 健康檢查 API
# ==========================================

@app.get("/health")
def health():

    job_store_status = "unconfigured"
    if _JOB_BACKEND != "unconfigured":
        try:
            _job_init()
            with _job_transaction() as conn:
                conn.execute("SELECT 1").fetchone()
            job_store_status = "ready"
        except Exception:
            job_store_status = "unavailable"
            app.logger.exception("Health job store check failed")

    return jsonify({
        "status": "ok",
        "service": "video-editor",
        "job_store_backend": _JOB_BACKEND,
        "job_store_status": job_store_status
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
                    "-ss", str(start),
                    "-i", str(source),
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

            # 取得原始影片長度
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

            # FFmpeg 場景變化偵測
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
                timeout=600,
                check=True
            )

            # 從 FFmpeg 記錄擷取場景時間碼
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

            # 建立各場景的起訖時間
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
# 中文旁白轉 MP3 API
# POST /tts  (JSON body)
# 這是增量功能，不影響既有 FFmpeg 三個 API。
# ==========================================

@app.post("/tts")
def generate_tts():
    import re

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "請使用 JSON 傳入 text"}), 400

    speech_text = payload.get("text")
    if not isinstance(speech_text, str) or not speech_text.strip():
        return jsonify({"error": "text 必須為非空字串"}), 400

    speech_text = speech_text.strip()
    if len(speech_text) > 1000:
        return jsonify({"error": "測試階段 text 最多 1000 字"}), 400

    voice = payload.get("voice", "zh-TW-YunJheNeural")
    if voice not in ("zh-TW-YunJheNeural", "zh-TW-HsiaoChenNeural"):
        return jsonify({"error": "目前僅支援兩種台灣中文語音"}), 400

    rate = payload.get("rate", "+0%")
    if not isinstance(rate, str) or not re.fullmatch(r"[+-]\d{1,2}%", rate):
        return jsonify({"error": "rate 格式應為 +0%、+10%、-10% 等"}), 400
    if int(rate[:-1]) < -50 or int(rate[:-1]) > 50:
        return jsonify({"error": "rate 必須介於 -50% 和 +50%"}), 400

    try:
        # 在函式內匯入，避免缺少 TTS 套件時連 /health 都無法啟動。
        import edge_tts
        from edge_tts.exceptions import NoAudioReceived
        import time

        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "narration.mp3"
            retry_delays = (2, 5)

            for attempt in range(3):
                try:
                    # 每次都建立新連線，並避免使用上一次失敗的殘留檔案。
                    target.unlink(missing_ok=True)
                    speaker = edge_tts.Communicate(
                        speech_text,
                        voice=voice,
                        rate=rate,
                    )
                    speaker.save_sync(str(target))

                    audio_bytes = target.read_bytes()
                    if len(audio_bytes) < 1024:
                        raise ValueError("語音檔過小，可能合成失敗")

                    return send_file(
                        BytesIO(audio_bytes),
                        mimetype="audio/mpeg",
                        as_attachment=True,
                        download_name="narration.mp3",
                    )
                except NoAudioReceived:
                    if attempt == 2:
                        # 讓外層保留完整 traceback，方便查看 Render Logs。
                        raise
                    delay = retry_delays[attempt]
                    app.logger.warning(
                        "TTS NoAudioReceived (attempt %d/3); retry in %ds",
                        attempt + 1,
                        delay,
                    )
                    time.sleep(delay)

    except Exception:
        app.logger.exception("TTS generation failed")
        return jsonify({"error": "TTS 服務暫時無法產生音訊"}), 502


# ==========================================
# MP3 音訊長度資訊
# POST /audio-info (multipart: audio)
# ==========================================

def probe_media_info(media_path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration:stream=codec_type",
            "-of", "json", str(media_path)
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    details = json.loads(result.stdout)
    duration = float(details.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("無法取得媒體長度")

    streams = details.get("streams", [])
    return {
        "duration_sec": round(duration, 3),
        "has_audio": any(x.get("codec_type") == "audio" for x in streams),
        "has_video": any(x.get("codec_type") == "video" for x in streams),
    }


@app.post("/audio-info")
def audio_info():
    audio = request.files.get("audio")
    if not audio:
        return jsonify({"error": "請上傳 audio (MP3)"}), 400

    try:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "narration.mp3"
            audio.save(path)
            info = probe_media_info(path)
            if not info["has_audio"]:
                return jsonify({"error": "上傳檔案沒有音訊軌"}), 400
            return jsonify({
                "status": "success",
                "audio_duration_sec": info["duration_sec"],
                "size_bytes": path.stat().st_size,
            })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "音訊檢查逾時"}), 504
    except (subprocess.CalledProcessError, OSError, ValueError, json.JSONDecodeError):
        app.logger.exception("Audio probe failed")
        return jsonify({"error": "無法讀取音訊資訊"}), 400


# ==========================================
# 原始剪輯影片 + 旁白 MP3 混音
# POST /mix-narration (multipart: video, audio)
# video: 已完成剪輯的 MP4
# audio: TTS 旁白 MP3
# ==========================================

@app.post("/mix-narration")
def mix_narration():
    video = request.files.get("video")
    audio = request.files.get("audio")
    if not video or not audio:
        return jsonify({"error": "需要 video (MP4) 和 audio (MP3)"}), 400

    try:
        original_volume = float(request.form.get("original_volume", "0.25"))
        narration_volume = float(request.form.get("narration_volume", "1.0"))
    except (ValueError, TypeError):
        return jsonify({"error": "音量必須為數字"}), 400

    if (
        not math.isfinite(original_volume)
        or not math.isfinite(narration_volume)
        or not 0 <= original_volume <= 1
        or not 0 <= narration_volume <= 1.5
    ):
        return jsonify({"error": "original_volume 需介於 0～1，narration_volume 需介於 0～1.5"}), 400

    try:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source_video = folder / "input.mp4"
            source_audio = folder / "narration.mp3"
            output = folder / "video_with_narration.mp4"
            video.save(source_video)
            audio.save(source_audio)

            vinfo = probe_media_info(source_video)
            ainfo = probe_media_info(source_audio)
            if not vinfo["has_video"]:
                return jsonify({"error": "video 不包含影片軌"}), 400
            if not ainfo["has_audio"]:
                return jsonify({"error": "audio 不包含音訊軌"}), 400

            video_duration = vinfo["duration_sec"]
            audio_duration = ainfo["duration_sec"]

            # 不要默默截斷旁白。時間超出時先回報時長及建議語速。
            if audio_duration > video_duration + 0.1:
                speedup_pct = math.ceil(
                    ((audio_duration + 0.35) / video_duration - 1) * 100
                )
                return jsonify({
                    "error": "旁白比影片長，尚未混音；請提高 TTS rate 或縮短旁白",
                    "video_duration_sec": video_duration,
                    "audio_duration_sec": audio_duration,
                    "suggested_tts_rate": "+{}%".format(min(50, max(0, speedup_pct))),
                    "note": "建議語速為粗略估算；重新產生 MP3 後仍須檢查秒數",
                }), 422

            video_end = "{:.3f}".format(video_duration)
            if vinfo["has_audio"]:
                graph = (
                    "[0:a:0]volume={:.3f},apad,atrim=duration={},asetpts=PTS-STARTPTS[bg];"
                    "[1:a:0]volume={:.3f},apad,atrim=duration={},asetpts=PTS-STARTPTS[voice];"
                    "[bg][voice]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[a]"
                ).format(
                    original_volume, video_end,
                    narration_volume, video_end,
                )
            else:
                graph = (
                    "[1:a:0]volume={:.3f},apad,atrim=duration={},asetpts=PTS-STARTPTS[a]"
                ).format(narration_volume, video_end)

            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(source_video), "-i", str(source_audio),
                    "-filter_complex", graph,
                    "-map", "0:v:0", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-t", video_end, "-movflags", "+faststart",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )

            if not output.exists() or output.stat().st_size <= 1024:
                raise ValueError("輸出影片檔案過小")

            return send_file(
                BytesIO(output.read_bytes()),
                mimetype="video/mp4",
                as_attachment=True,
                download_name="video_with_narration.mp4",
            )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "FFmpeg 混音逾時"}), 504
    except (subprocess.CalledProcessError, OSError, ValueError, json.JSONDecodeError):
        app.logger.exception("Narration mix failed")
        return jsonify({"error": "音訊混音失敗，請檢查 Render Logs"}), 500



# ==========================================
# 將 SRT 繁體中文字幕燒錄到已混音的 MP4
# POST /burn-subtitles
# multipart: video (MP4), srt (UTF-8 SRT 文字)
# ==========================================

@app.post("/burn-subtitles")
def burn_subtitles():
    video = request.files.get("video")
    subtitle_text = request.form.get("srt", "")

    if not video:
        return jsonify({"error": "需要上傳 video (MP4)"}), 400

    if not isinstance(subtitle_text, str) or not subtitle_text.strip():
        return jsonify({"error": "需要提供 srt 字幕內容"}), 400

    if len(subtitle_text) > 30000:
        return jsonify({"error": "SRT 文字超過 30000 字元"}), 400

    if "-->" not in subtitle_text:
        return jsonify({"error": "無效的 SRT：找不到時間碼"}), 400

    try:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source_video = folder / "input.mp4"
            subtitle_file = folder / "captions.srt"
            output = folder / "video_with_subtitles.mp4"

            video.save(source_video)
            subtitle_file.write_text(
                subtitle_text.lstrip("\ufeff"),
                encoding="utf-8",
            )

            # 確認 Render 有可供 libass 使用的繁中 CJK 字型。
            font_folder = Path(__file__).resolve().parent / "fonts"
            font_file = font_folder / "NotoSansCJKtc-Regular.otf"
            if not font_file.is_file() or font_file.stat().st_size < 1000000:
                return jsonify({
                    "error": "缺少繁體中文字型，請於 Render Build Command 執行 python download_fonts.py"
                }), 503

            style = (
                "FontName=Noto Sans CJK TC,"
                "FontSize=24,"
                "PrimaryColour=&H00FFFFFF,"
                "OutlineColour=&H00000000,"
                "BorderStyle=1,"
                "Outline=2,"
                "Shadow=1,"
                "Alignment=2,"
                "MarginV=30"
            )
            subtitle_filter = (
                f"subtitles={subtitle_file}:fontsdir={font_folder}:force_style='{style}'"
            )

            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin", "-hide_banner",
                    "-loglevel", "error", "-y",
                    "-i", str(source_video),
                    "-vf", subtitle_filter,
                    "-map", "0:v:0",
                    "-map", "0:a?",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "22",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "copy",
                    "-movflags", "+faststart",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )

            if not output.exists() or output.stat().st_size <= 1024:
                raise ValueError("輸出檔案過小")

            return send_file(
                BytesIO(output.read_bytes()),
                mimetype="video/mp4",
                as_attachment=True,
                download_name="video_with_subtitles.mp4",
            )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "FFmpeg 字幕燒錄逾時"}), 504

    except (subprocess.CalledProcessError, OSError, ValueError):
        app.logger.exception("Subtitle burn failed")
        return jsonify({
            "error": "字幕燒錄失敗，請檢查 Render Logs 與中文字型"
        }), 500


# ==========================================
# Flask 本機啟動
# Render 正式環境由 Gunicorn 啟動
# ==========================================

# ==========================================
# 多段 MP3 旁白合併 API
# POST /concat-audio
# multipart/form-data: 多個 audio 欄位（依上傳順序）
# 原有 /tts、/audio-info 與 /mix-narration 不變
# ==========================================

@app.post("/concat-audio")
def concat_audio():
    audio_files = request.files.getlist("audio")

    if not 1 <= len(audio_files) <= 30:
        return jsonify({
            "error": "音訊片段數必須介於 1 至 30，且欄位名稱必須為 audio"
        }), 400

    try:
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            filenames = []

            # 固定命名並保留 multipart 表單傳入的順序
            for index, audio in enumerate(audio_files, 1):
                name = f"part_{index:03d}.mp3"
                path = folder / name
                audio.save(path)

                if path.stat().st_size < 100:
                    return jsonify({
                        "error": f"第 {index} 段 MP3 無效或內容為空"
                    }), 400

                filenames.append(name)

            # FFmpeg concat demuxer 依清單順序拼接
            concat_file = folder / "concat.txt"
            concat_file.write_text(
                "\n".join(
                    f"file '{name}'" for name in filenames
                ) + "\n",
                encoding="utf-8"
            )

            output = folder / "narration.mp3"

            # 重新編碼以統一音訊格式
            subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner",
                    "-loglevel", "error", "-y",
                    "-f", "concat", "-safe", "0",
                    "-i", str(concat_file),
                    "-vn", "-map", "0:a:0",
                    "-c:a", "libmp3lame",
                    "-b:a", "128k", "-ar", "24000",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )

            if not output.exists() or output.stat().st_size < 1024:
                raise ValueError("合併後的 MP3 無效")

            return send_file(
                BytesIO(output.read_bytes()),
                mimetype="audio/mpeg",
                as_attachment=True,
                download_name="narration.mp3",
            )

    except subprocess.TimeoutExpired:
        return jsonify({"error": "MP3 合併處理逾時"}), 504

    except (subprocess.CalledProcessError, OSError, ValueError):
        app.logger.exception("MP3 concat failed")
        return jsonify({
            "error": "MP3 合併失敗，請檢查 Render Logs"
        }), 500


# ==========================================
# Google Drive OAuth 連線測試 API
# 僅讀取指定測試影片的檔案資訊
# 不下載影片、不執行 FFmpeg
# ==========================================

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.auth.exceptions import RefreshError
import requests as google_requests


@app.get("/drive-file-info")
def drive_file_info():

    # 暫時只允許測試這支已確認的電影
    test_file_id = (
        "1HWBhQF5YR_lXk93Yt7QONqXvWyl6GwNv"
    )

    client_id = os.environ.get(
        "GOOGLE_CLIENT_ID", ""
    )

    client_secret = os.environ.get(
        "GOOGLE_CLIENT_SECRET", ""
    )

    refresh_token = os.environ.get(
        "GOOGLE_REFRESH_TOKEN", ""
    )

    # 檢查 Render 環境變數
    if not all([
        client_id,
        client_secret,
        refresh_token
    ]):
        return jsonify({
            "status": "error",
            "error": "Google OAuth environment variables missing"
        }), 503

    try:

        # 使用 Refresh Token 取得 Access Token
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=(
                "https://oauth2.googleapis.com/token"
            ),
            client_id=client_id,
            client_secret=client_secret,
            scopes=[
                "https://www.googleapis.com/auth/drive.readonly"
            ]
        )

        credentials.refresh(
            GoogleAuthRequest()
        )

    except RefreshError:

        # 不輸出憑證或 Google 回傳的敏感資料
        return jsonify({
            "status": "error",
            "error": "Google OAuth token refresh failed"
        }), 502

    try:

        # 只取得 metadata，不下載影片內容
        response = google_requests.get(
            "https://www.googleapis.com/drive/v3/files/"
            + test_file_id,
            headers={
                "Authorization": (
                    "Bearer " + credentials.token
                )
            },
            params={
                "fields": "id,name,mimeType,size"
            },
            timeout=(10, 30)
        )

        if response.status_code != 200:

            google_reason = "unknown"
            google_error_status = "unknown"

            try:
                error_data = response.json().get("error", {})

                if isinstance(error_data, dict):
                    google_error_status = str(
                        error_data.get("status", "unknown")
                    )[:80]

                    errors = error_data.get("errors", [])
                    if isinstance(errors, list) and errors:
                        first_error = errors[0]
                        if isinstance(first_error, dict):
                            google_reason = str(
                                first_error.get("reason", "unknown")
                            )[:100]

                    if google_reason == "unknown":
                        details = error_data.get("details", [])
                        if isinstance(details, list):
                            for detail in details:
                                if isinstance(detail, dict):
                                    reason = detail.get("reason")
                                    if reason:
                                        google_reason = str(reason)[:100]
                                        break

            except (ValueError, TypeError, AttributeError):
                pass

            return jsonify({
                "status": "error",
                "error": "Google Drive metadata request failed",
                "google_status": response.status_code,
                "google_reason": google_reason,
                "google_error_status": google_error_status
            }), 502

        file_data = response.json()

        size_bytes = int(
            file_data.get("size", 0)
        )

        return jsonify({
            "status": "success",
            "google_drive_connected": True,
            "file": {
                "id": file_data.get("id"),
                "name": file_data.get("name"),
                "mime_type": file_data.get("mimeType"),
                "size_bytes": size_bytes,
                "size_mb": round(
                    size_bytes / 1024 / 1024, 2
                )
            }
        })

    except (
        google_requests.RequestException,
        ValueError
    ):

        return jsonify({
            "status": "error",
            "error": "Google Drive connection failed"
        }), 502

# ==========================================
# Google Drive partial-download verification
# Protected by the existing X-Editor-Key.
# Reads at most 1 MiB of a fixed, approved test file.
# Does not download the complete movie or run FFmpeg.
# ==========================================

@app.get("/drive-range-test")
def drive_range_test():

    test_file_id = "1HWBhQF5YR_lXk93Yt7QONqXvWyl6GwNv"
    sample_size = 1024 * 1024

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

    if not all((client_id, client_secret, refresh_token)):
        return jsonify({
            "status": "error",
            "error": "Google OAuth environment variables missing"
        }), 503

    try:
        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=[
                "https://www.googleapis.com/auth/drive.readonly"
            ]
        )
        credentials.refresh(GoogleAuthRequest())
    except RefreshError:
        return jsonify({
            "status": "error",
            "error": "Google OAuth token refresh failed"
        }), 502

    try:
        # stream=True prevents loading the entire MP4 into memory.
        with google_requests.get(
            "https://www.googleapis.com/drive/v3/files/" + test_file_id,
            params={"alt": "media"},
            headers={
                "Authorization": "Bearer " + credentials.token,
                "Range": "bytes=0-1048575",
                "Accept-Encoding": "identity"
            },
            stream=True,
            timeout=(10, 30)
        ) as response:

            if response.status_code != 206:
                # Do not consume the body if Google ignores Range.
                return jsonify({
                    "status": "error",
                    "error": "Google Drive partial download failed",
                    "google_status": response.status_code,
                    "range_supported": False
                }), 502

            content_range = response.headers.get(
                "Content-Range", ""
            )

            if not content_range.startswith("bytes 0-1048575/"):
                return jsonify({
                    "status": "error",
                    "error": "Unexpected Google Drive content range",
                    "google_status": response.status_code
                }), 502

            bytes_read = 0
            for chunk in response.iter_content(chunk_size=65536):
                bytes_read += len(chunk)
                if bytes_read > sample_size:
                    return jsonify({
                        "status": "error",
                        "error": "Partial response exceeded safe test size"
                    }), 502

            if bytes_read != sample_size:
                return jsonify({
                    "status": "error",
                    "error": "Partial response was incomplete",
                    "bytes_read": bytes_read
                }), 502

            return jsonify({
                "status": "success",
                "google_drive_connected": True,
                "file_id": test_file_id,
                "range_supported": True,
                "google_status": response.status_code,
                "content_range": content_range,
                "bytes_read": bytes_read
            })

    except google_requests.RequestException:
        return jsonify({
            "status": "error",
            "error": "Google Drive partial download connection failed"
        }), 502


# ==========================================
# Drive scene detection: asynchronous test job
# The source is downloaded directly from Google Drive
# to a temporary file, never through an HTTP upload.
# This is process-local test state, not durable storage.
# ==========================================

import re as _scene_re
import shutil as _scene_shutil
import threading as _scene_threading
import time as _scene_time
import uuid as _scene_uuid

# ==========================================
# Durable background job store for Drive jobs
# Backends: PostgreSQL (DATABASE_URL) or SQLite on a mounted disk.
# Render Free does NOT offer persistent disks. For Free, use external Postgres.
# No store -> job POST/GET returns 503; do not silently use ephemeral memory.
# ==========================================

import sqlite3 as _jobs_sqlite
from contextlib import contextmanager as _jobs_contextmanager

_JOB_INSTANCE_ID = _scene_uuid.uuid4().hex
_JOB_DATABASE_URL = (
    os.environ.get("MOVIE_JOB_DATABASE_URL")
    or os.environ.get("DATABASE_URL")
    or ""
).strip()
_JOB_SQLITE_PATH = os.environ.get("MOVIE_JOB_SQLITE_PATH", "").strip()
_JOB_BACKEND = (
    "postgres" if _JOB_DATABASE_URL
    else "sqlite" if _JOB_SQLITE_PATH
    else "unconfigured"
)
_JOB_INIT_LOCK = _scene_threading.RLock()
_JOB_READY = False
_JOB_ACTIVE = ("queued", "downloading", "detecting_scenes")
_JOB_TERMINAL = ("completed", "failed")


def _job_sql(statement):
    return statement.replace("?", "%s") if _JOB_BACKEND == "postgres" else statement


def _job_conn_open():
    if _JOB_BACKEND == "postgres":
        import psycopg
        return psycopg.connect(_JOB_DATABASE_URL, connect_timeout=10)
    if _JOB_BACKEND == "sqlite":
        db_path = Path(_JOB_SQLITE_PATH)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = _jobs_sqlite.connect(str(db_path), timeout=20)
        connection.execute("PRAGMA busy_timeout=20000")
        return connection
    raise RuntimeError("durable_job_store_not_configured")


@_jobs_contextmanager
def _job_transaction(lock_writer=False):
    conn = _job_conn_open()
    try:
        if _JOB_BACKEND == "sqlite" and lock_writer:
            conn.execute("BEGIN IMMEDIATE")
        elif _JOB_BACKEND == "postgres" and lock_writer:
            # One transaction-wide lock prevents duplicate job creation across workers.
            conn.execute("SELECT pg_advisory_xact_lock(58024011)")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _job_init():
    global _JOB_READY
    if _JOB_READY:
        return
    with _JOB_INIT_LOCK:
        if _JOB_READY:
            return
        with _job_transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS movie_line_jobs (
                    job_id TEXT PRIMARY KEY,
                    job_kind TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    created_at BIGINT NOT NULL,
                    updated_at BIGINT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_movie_line_jobs_lookup
                ON movie_line_jobs (job_kind, task_key, created_at)
            """)
        _JOB_READY = True


def _job_row(conn, where, params, order="created_at DESC, job_id DESC"):
    sql = (
        "SELECT state_json FROM movie_line_jobs WHERE " + where
        + " ORDER BY " + order + " LIMIT 1"
    )
    row = conn.execute(_job_sql(sql), params).fetchone()
    return json.loads(row[0]) if row else None


def _job_put(conn, state):
    # Every update preserves the whole JSON result, not just a status flag.
    state["updated_at"] = int(_scene_time.time())
    conn.execute(_job_sql("""
        UPDATE movie_line_jobs
        SET status = ?, state_json = ?, updated_at = ?
        WHERE job_id = ?
    """), (
        state["status"], json.dumps(state, ensure_ascii=False),
        state["updated_at"], state["job_id"]
    ))


def _job_recover_if_interrupted(conn, state):
    if not state or state.get("status") not in _JOB_ACTIVE:
        return state
    # A daemon thread cannot survive process death. Never claim it is running
    # after another Gunicorn process becomes the owner of this job store.
    if state.get("_instance_id") != _JOB_INSTANCE_ID:
        state["status"] = "failed"
        state["error"] = "worker_interrupted_after_restart"
        state["interrupted_at"] = int(_scene_time.time())
        _job_put(conn, state)
    return state


def _job_create(kind, task_key, details):
    _job_init()
    with _job_transaction(lock_writer=True) as conn:
        latest = _job_row(conn, "job_kind = ? AND task_key = ?", (kind, task_key))
        if latest:
            latest = _job_recover_if_interrupted(conn, latest)
            if latest["status"] == "completed":
                return "reused", latest
            if latest["status"] in _JOB_ACTIVE:
                return "reused", latest
            # A previous worker was interrupted. Require a deliberate retry
            # after the caller has verified that no checkpoint exists.
            if latest.get("error") == "worker_interrupted_after_restart":
                if not details.get("confirmed_checkpoint_missing", False):
                    return "interrupted", latest

        # Preserve the old one-at-a-time behavior for each endpoint type.
        rows = conn.execute(_job_sql("""
            SELECT state_json FROM movie_line_jobs
            WHERE job_kind = ? AND status IN (?, ?, ?)
            ORDER BY created_at DESC
        """), (kind, *_JOB_ACTIVE)).fetchall()
        for row in rows:
            running = _job_recover_if_interrupted(conn, json.loads(row[0]))
            if running["status"] in _JOB_ACTIVE:
                return "busy", running

        timestamp = int(_scene_time.time())
        timestamp_ms = int(_scene_time.time() * 1000)
        job_id = _scene_uuid.uuid4().hex
        state = {
            "job_id": job_id,
            "status": "queued",
            "created_at": timestamp,
            "updated_at": timestamp,
            "_instance_id": _JOB_INSTANCE_ID,
            "_task_key": task_key,
            **{k: v for k, v in details.items() if k != "confirmed_checkpoint_missing"}
        }
        conn.execute(_job_sql("""
            INSERT INTO movie_line_jobs (
                job_id, job_kind, task_key, status,
                state_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """), (
            job_id, kind, task_key, "queued",
            json.dumps(state, ensure_ascii=False), timestamp_ms, timestamp_ms
        ))
        return "created", state


def _job_update(job_id, **values):
    try:
        _job_init()
        with _job_transaction(lock_writer=True) as conn:
            state = _job_row(conn, "job_id = ?", (job_id,))
            if not state or state.get("status") in _JOB_TERMINAL:
                return
            state.update(values)
            _job_put(conn, state)
        if values.get("status") in _JOB_TERMINAL:
            app.logger.info("movie_job_finished job_id=%s status=%s", job_id, values["status"])
    except Exception:
        app.logger.exception("movie_job_update_failed job_id=%s", job_id)
        # Do not leak credentials or accidentally report completion.
        raise


def _job_lookup(kind, job_id=None, task_key=None):
    _job_init()
    with _job_transaction(lock_writer=True) as conn:
        if job_id:
            state = _job_row(conn, "job_kind = ? AND job_id = ?", (kind, job_id))
        elif task_key:
            state = _job_row(conn, "job_kind = ? AND task_key = ?", (kind, task_key))
        else:
            state = _job_row(conn, "job_kind = ?", (kind,))
        return _job_recover_if_interrupted(conn, state)


def _job_public(state):
    if state is None:
        return {"status": "idle"}
    return {key: value for key, value in state.items() if not key.startswith("_")}


def _job_no_store_response():
    return jsonify({
        "status": "storage_unavailable",
        "error": "Durable job storage not configured",
        "required": "MOVIE_JOB_DATABASE_URL or MOVIE_JOB_SQLITE_PATH"
    }), 503


def _job_store_error():
    app.logger.exception("movie_job_store_error")
    return jsonify({"status": "storage_unavailable", "error": "Job storage temporarily unavailable"}), 503


def _job_api_response(outcome, state, endpoint):
    payload = {
        "job_id": state["job_id"],
        "status": state["status"],
        "status_endpoint": endpoint
    }
    if outcome == "created":
        return jsonify(payload), 202
    if outcome == "reused":
        payload["reused"] = True
        return jsonify(payload), 200
    if outcome == "busy":
        payload["error"] = "Another job of this kind is already running"
        return jsonify(payload), 409
    payload["error"] = "Previous job interrupted; check checkpoint before retry"
    payload["retry_requires"] = "confirmed_checkpoint_missing: true"
    return jsonify(payload), 409


_DRIVE_TEST_FILE_ID = "1HWBhQF5YR_lXk93Yt7QONqXvWyl6GwNv"


def _drive_scene_update(job_id, **values):
    _job_update(job_id, **values)


def _drive_scene_worker(job_id, threshold):
    try:
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("oauth_environment_missing")

        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        credentials.refresh(GoogleAuthRequest())

        headers = {
            "Authorization": "Bearer " + credentials.token,
            "Accept-Encoding": "identity"
        }
        url = (
            "https://www.googleapis.com/drive/v3/files/"
            + _DRIVE_TEST_FILE_ID
        )

        with google_requests.get(
            url,
            params={"fields": "id,name,mimeType,size,capabilities(canDownload)"},
            headers=headers,
            timeout=(15, 45)
        ) as metadata_response:
            if metadata_response.status_code != 200:
                raise ValueError("drive_metadata_failed")
            metadata = metadata_response.json()

        if metadata.get("mimeType") != "video/mp4":
            raise ValueError("not_an_mp4")
        if not metadata.get("capabilities", {}).get("canDownload", False):
            raise ValueError("drive_download_not_allowed")

        expected_bytes = int(metadata.get("size", 0))
        if expected_bytes < 1024 or expected_bytes > 1024 * 1024 * 1024:
            raise ValueError("invalid_or_oversized_file")

        with TemporaryDirectory(prefix="drive_scene_") as tmp:
            folder = Path(tmp)
            source = folder / "source.mp4"
            free_bytes = _scene_shutil.disk_usage(folder).free
            if free_bytes < expected_bytes + 256 * 1024 * 1024:
                raise ValueError("insufficient_temporary_disk_space")

            _drive_scene_update(
                job_id,
                status="downloading",
                total_bytes=expected_bytes,
                downloaded_bytes=0,
                percentage=0
            )

            downloaded = 0
            started_at = _scene_time.monotonic()

            with google_requests.get(
                url,
                params={"alt": "media"},
                headers=headers,
                stream=True,
                timeout=(15, 75)
            ) as download_response:
                if download_response.status_code != 200:
                    raise ValueError("drive_media_download_failed")

                with source.open("wb") as output:
                    for chunk in download_response.iter_content(
                        chunk_size=1024 * 1024
                    ):
                        if not chunk:
                            continue
                        downloaded += len(chunk)
                        if downloaded > expected_bytes:
                            raise ValueError("drive_size_mismatch")
                        if _scene_time.monotonic() - started_at > 1800:
                            raise ValueError("download_time_limit_exceeded")
                        output.write(chunk)
                        _drive_scene_update(
                            job_id,
                            downloaded_bytes=downloaded,
                            percentage=round(
                                downloaded / expected_bytes * 100, 1
                            )
                        )

            if downloaded != expected_bytes:
                raise ValueError("drive_download_incomplete")

            _drive_scene_update(
                job_id,
                status="detecting_scenes",
                downloaded_bytes=downloaded,
                percentage=100
            )

            probe = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    str(source)
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=60
            )
            duration = float(probe.stdout.strip())
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("invalid_video_duration")

            scene_filter = (
                "scale=320:-2,"
                f"select='gt(scene,{threshold})',"
                "showinfo"
            )

            ffmpeg_result = subprocess.run(
                [
                    "ffmpeg", "-nostdin", "-hide_banner",
                    "-loglevel", "info",
                    "-threads", "1",
                    "-filter_threads", "1",
                    "-i", str(source),
                    "-map", "0:v:0",
                    "-vf", scene_filter,
                    "-an", "-f", "null", "-"
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=3600
            )

            timestamps = sorted(set(
                round(float(x), 3)
                for x in _scene_re.findall(
                    r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)",
                    ffmpeg_result.stderr
                )
            ))

            duration_rounded = round(duration, 3)
            boundaries = [0.0]
            for timestamp in timestamps:
                if (
                    0 < timestamp < duration_rounded
                    and timestamp > boundaries[-1] + 0.001
                ):
                    boundaries.append(timestamp)
            boundaries.append(duration_rounded)

            scenes = [
                {
                    "scene_index": index,
                    "start_sec": boundaries[index],
                    "end_sec": boundaries[index + 1],
                    "duration_sec": round(
                        boundaries[index + 1] - boundaries[index], 3
                    )
                }
                for index in range(len(boundaries) - 1)
            ]

            _drive_scene_update(
                job_id,
                status="completed",
                duration_sec=round(duration, 3),
                threshold=threshold,
                scene_count=len(scenes),
                scene_boundaries=boundaries,
                scenes=scenes,
                completed_at=int(_scene_time.time())
            )

    except RefreshError:
        _drive_scene_update(
            job_id, status="failed", error="oauth_refresh_failed"
        )
    except subprocess.TimeoutExpired:
        _drive_scene_update(
            job_id, status="failed", error="ffmpeg_timeout"
        )
    except google_requests.RequestException:
        _drive_scene_update(
            job_id, status="failed", error="google_drive_request_failed"
        )
    except subprocess.CalledProcessError:
        _drive_scene_update(
            job_id, status="failed", error="ffmpeg_processing_failed"
        )
    except (OSError, TypeError, ValueError, KeyError) as error:
        safe_errors = {
            "oauth_environment_missing",
            "drive_metadata_failed",
            "not_an_mp4",
            "drive_download_not_allowed",
            "invalid_or_oversized_file",
            "insufficient_temporary_disk_space",
            "drive_media_download_failed",
            "drive_size_mismatch",
            "download_time_limit_exceeded",
            "drive_download_incomplete",
            "invalid_video_duration"
        }
        code = str(error)
        _drive_scene_update(
            job_id,
            status="failed",
            error=code if code in safe_errors else "processing_error"
        )
    except Exception:
        app.logger.exception("Full scene worker failed job_id=%s", job_id)
        try:
            _drive_scene_update(job_id, status="failed", error="processing_error")
        except Exception:
            app.logger.exception("Could not persist full job status job_id=%s", job_id)


@app.post("/drive-scenes-job")
def drive_scenes_job_start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    if data.get("file_id") != _DRIVE_TEST_FILE_ID:
        return jsonify({"error": "File ID not allowlisted"}), 403
    try:
        threshold = float(data.get("threshold", 0.35))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid threshold"}), 400
    if not math.isfinite(threshold) or not 0.1 <= threshold <= 0.8:
        return jsonify({"error": "Threshold must be 0.1 to 0.8"}), 400
    if _JOB_BACKEND == "unconfigured":
        return _job_no_store_response()

    task_key = f"{_DRIVE_TEST_FILE_ID}:full:{threshold:.6g}"
    try:
        outcome, state = _job_create("full", task_key, {
            "file_id": _DRIVE_TEST_FILE_ID,
            "threshold": threshold,
            "downloaded_bytes": 0,
            "confirmed_checkpoint_missing": data.get("confirmed_checkpoint_missing") is True
        })
    except Exception:
        return _job_store_error()

    if outcome == "created":
        thread = _scene_threading.Thread(
            target=_drive_scene_worker,
            args=(state["job_id"], threshold),
            daemon=True
        )
        try:
            thread.start()
        except Exception:
            _drive_scene_update(state["job_id"], status="failed", error="thread_start_failed")
            return jsonify({"error": "Could not start worker", "job_id": state["job_id"]}), 503
        app.logger.info("movie_job_started kind=full job_id=%s", state["job_id"])
    return _job_api_response(outcome, state, "/drive-scenes-job")


@app.get("/drive-scenes-job")
def drive_scenes_job_status():
    if _JOB_BACKEND == "unconfigured":
        return _job_no_store_response()
    try:
        state = _job_lookup("full", request.args.get("job_id"))
    except Exception:
        return _job_store_error()
    if state is None and request.args.get("job_id"):
        return jsonify({"status": "not_found", "error": "Unknown job_id"}), 404
    return jsonify(_job_public(state))


# ==========================================
# Drive segment scene detection (test version).
# Only analyzes a selected 15-300 second interval.
# Google Drive data is read by FFmpeg over HTTPS with Range seeks,
# without writing the complete movie to the local disk.
# Results are stored in PostgreSQL or SQLite, including completed scenes.
# ==========================================

def _segment_scene_update(job_id, **values):
    _job_update(job_id, **values)


def _segment_scene_worker(job_id, file_id, start_sec, duration_sec, threshold):
    process = None
    try:
        client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
        client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
        refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
        if not all((client_id, client_secret, refresh_token)):
            raise ValueError("oauth_environment_missing")

        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        credentials.refresh(GoogleAuthRequest())

        media_url = (
            "https://www.googleapis.com/drive/v3/files/"
            + file_id + "?alt=media"
        )
        # Never print command arguments: they contain an access token.
        http_headers = (
            "Authorization: Bearer " + credentials.token + "\r\n"
            "Accept-Encoding: identity\r\n"
        )

        with TemporaryDirectory(prefix="segment_scene_") as temp_dir:
            progress_file = Path(temp_dir) / "ffmpeg.progress"
            stderr_file = Path(temp_dir) / "ffmpeg.stderr"

            scene_filter = (
                "setpts=PTS-STARTPTS,"
                "scale=320:-2,"
                f"select='gt(scene,{threshold})',"
                "showinfo"
            )
            command = [
                "ffmpeg", "-nostdin", "-hide_banner",
                "-loglevel", "info",
                "-stats_period", "5",
                "-progress", str(progress_file),
                "-threads", "1", "-filter_threads", "1",
                "-rw_timeout", "30000000",
                "-seekable", "1",
                "-headers", http_headers,
                "-ss", str(start_sec),
                "-i", media_url,
                "-map", "0:v:0",
                "-t", str(duration_sec),
                "-vf", scene_filter,
                "-an", "-f", "null", "-"
            ]

            _segment_scene_update(
                job_id, status="detecting_scenes",
                progress_percent=0,
                processed_sec=0
            )

            with stderr_file.open("w", encoding="utf-8") as errors:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.DEVNULL,
                    stderr=errors
                )
                deadline = _scene_time.monotonic() + 900

                try:
                    while process.poll() is None:
                        if _scene_time.monotonic() > deadline:
                            process.kill()
                            process.wait(timeout=10)
                            raise subprocess.TimeoutExpired(
                                cmd="ffmpeg-segment", timeout=900
                            )

                        if progress_file.exists():
                            progress_text = progress_file.read_text(
                                encoding="utf-8", errors="replace"
                            )
                            matches = _scene_re.findall(
                                r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)",
                                progress_text
                            )
                            if matches:
                                hours, minutes, seconds = matches[-1]
                                processed = (
                                    int(hours) * 3600
                                    + int(minutes) * 60
                                    + float(seconds)
                                )
                                processed = min(
                                    duration_sec, max(0.0, processed)
                                )
                                _segment_scene_update(
                                    job_id,
                                    processed_sec=round(processed, 2),
                                    progress_percent=round(
                                        processed / duration_sec * 100, 1
                                    )
                                )

                        _scene_time.sleep(4)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)

            if process.returncode != 0:
                # Only a fixed error code is exposed; no bearer token.
                raise ValueError("ffmpeg_remote_segment_failed")

            output_log = stderr_file.read_text(
                encoding="utf-8", errors="replace"
            )
            relative_cuts = sorted(set(
                round(float(match), 3)
                for match in _scene_re.findall(
                    r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)",
                    output_log
                )
                if 0 < float(match) < duration_sec
            ))

            end_sec = round(start_sec + duration_sec, 3)
            cuts = [
                round(start_sec + relative_time, 3)
                for relative_time in relative_cuts
            ]
            # The segment edges are boundaries, NOT detected scene cuts.
            boundaries = [round(start_sec, 3), *cuts, end_sec]
            scenes = [
                {
                    "scene_index": index,
                    "start_sec": boundaries[index],
                    "end_sec": boundaries[index + 1],
                    "duration_sec": round(
                        boundaries[index + 1] - boundaries[index], 3
                    )
                }
                for index in range(len(boundaries) - 1)
            ]
            _segment_scene_update(
                job_id,
                status="completed",
                segment_start_sec=round(start_sec, 3),
                segment_end_sec=end_sec,
                segment_duration_sec=duration_sec,
                threshold=threshold,
                progress_percent=100,
                processed_sec=duration_sec,
                scene_count=len(scenes),
                detected_cut_timestamps=cuts,
                scene_boundaries=boundaries,
                scene_edges_are_artificial=True,
                scenes=scenes,
                completed_at=int(_scene_time.time())
            )

    except RefreshError:
        _segment_scene_update(
            job_id, status="failed", error="oauth_refresh_failed"
        )
    except subprocess.TimeoutExpired:
        _segment_scene_update(
            job_id, status="failed", error="ffmpeg_segment_timeout"
        )
    except google_requests.RequestException:
        _segment_scene_update(
            job_id, status="failed", error="google_drive_request_failed"
        )
    except (ValueError, OSError, TypeError) as error:
        code = str(error)
        allowed = {
            "oauth_environment_missing",
            "ffmpeg_remote_segment_failed"
        }
        _segment_scene_update(
            job_id, status="failed",
            error=code if code in allowed else "segment_processing_failed"
        )
    except Exception:
        app.logger.exception("Segment worker failed job_id=%s", job_id)
        try:
            _segment_scene_update(job_id, status="failed", error="segment_processing_failed")
        except Exception:
            app.logger.exception("Could not persist failed job status job_id=%s", job_id)


def _segment_time_key(value):
    # n8n canonical representation: milliseconds, no trailing zeros.
    return f"{value:.3f}".rstrip("0").rstrip(".")


@app.post("/drive-extract-frames")
def drive_extract_frames():
    """Extract one or two JPEG frames via authenticated Drive HTTP seeking.

    Does not download the original movie to the local filesystem.
    Read-only. Does not mutate FFmpeg jobs, Checkpoints, or Drive files.
    """
    import re
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    file_id = data.get("file_id")
    if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        return jsonify({"error": "Invalid file_id"}), 400
    stamps = data.get("timestamps_sec")
    # Gunicorn currently has a 180-second timeout. Avoid six long remote seeks
    # in a single synchronous request; start by validating up to two frames.
    if not isinstance(stamps, list) or not 1 <= len(stamps) <= 2:
        return jsonify({"error": "timestamps_sec must contain 1 or 2 timestamps"}), 400
    if any(type(t) not in (float, int) or not math.isfinite(t) or t < 0 or t > 86400
           for t in stamps):
        return jsonify({"error": "Invalid timestamp"}), 400
    if len(stamps) == 2 and abs(float(stamps[0]) - float(stamps[1])) < 0.005:
        return jsonify({"error": "Timestamps must be distinct"}), 400
    width = data.get("max_width", 640)
    if type(width) is not int or width not in (320, 480, 640):
        return jsonify({"error": "max_width must be 320, 480 or 640"}), 400

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    if not all((client_id, client_secret, refresh_token)):
        return jsonify({"error": "oauth_environment_missing"}), 503
    try:
        credentials = Credentials(
            token=None, refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id, client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        credentials.refresh(GoogleAuthRequest())
    except Exception:
        # Do not log the Google tokens, refresh result, or request headers.
        return jsonify({"error": "oauth_refresh_failed"}), 502

    media_root = "https://www.googleapis.com/drive/v3/files/" + file_id
    auth_header = "Authorization: Bearer " + credentials.token + "\r\nAccept-Encoding: identity\r\n"
    try:
        metadata = google_requests.get(
            media_root,
            params={"fields": "id,mimeType,size", "supportsAllDrives": "true"},
            headers={"Authorization": "Bearer " + credentials.token},
            timeout=(8, 15)
        )
        if metadata.status_code != 200:
            return jsonify({"error": "drive_metadata_failed"}), 502
        if metadata.json().get("mimeType") != "video/mp4":
            return jsonify({"error": "source_is_not_mp4"}), 400
    except Exception:
        return jsonify({"error": "drive_metadata_unavailable"}), 502

    images = []
    with TemporaryDirectory(prefix="drive_frame_smoke_") as tmp:
        for idx, sec in enumerate(stamps):
            target = Path(tmp) / ("frame_%02d.jpg" % idx)
            command = [
                "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-threads", "1", "-filter_threads", "1",
                "-rw_timeout", "30000000", "-seekable", "1",
                "-headers", auth_header, "-ss", "%.3f" % float(sec),
                "-i", media_root + "?alt=media", "-map", "0:v:0",
                "-frames:v", "1", "-vf", "scale=%d:-2" % width,
                "-q:v", "5", "-update", "1", str(target)
            ]
            # Never log command: it contains a short-lived Google access token.
            try:
                p = subprocess.run(
                    command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=65, check=False
                )
            except subprocess.TimeoutExpired:
                return jsonify({"error": "remote_seek_timeout", "sample_index": idx}), 504
            except OSError:
                return jsonify({"error": "ffmpeg_not_available"}), 502
            if p.returncode != 0 or not target.is_file():
                return jsonify({"error": "remote_frame_extraction_failed", "sample_index": idx}), 502
            payload = target.read_bytes()
            if not payload.startswith(b"\xff\xd8\xff") or not 1024 <= len(payload) <= 800000:
                return jsonify({"error": "invalid_extracted_jpeg", "sample_index": idx}), 502
            images.append({
                "sample_index": idx,
                "timestamp_sec": round(float(sec), 3),
                "mime_type": "image/jpeg",
                "byte_count": len(payload),
                "image_base64": base64.b64encode(payload).decode("ascii")
            })
    return jsonify({
        "status": "remote_frames_extracted", "file_id": file_id,
        "frame_count": len(images), "source_mode": "drive_authenticated_seek",
        "frames": images, "checkpoint_modified": False
    })


@app.post("/drive-extract-audio")
def drive_extract_audio():
    """Read-only bounded MP3 extract from Google Drive MP4 via remote FFmpeg seek."""
    import re
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "JSON object required"}), 400
    file_id = payload.get("file_id")
    if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", file_id):
        return jsonify({"error": "invalid_file_id"}), 400
    try:
        start_sec = float(payload.get("start_sec", 0))
        duration_sec = float(payload.get("duration_sec", 60))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid_audio_parameters"}), 400
    if not (math.isfinite(start_sec) and math.isfinite(duration_sec)) or not (0 <= start_sec <= 86400 and 15 <= duration_sec <= 120):
        return jsonify({"error": "audio_interval_out_of_range"}), 400
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    if not all((cid, secret, refresh)):
        return jsonify({"error": "oauth_environment_missing"}), 503
    try:
        credentials = Credentials(
            token=None, refresh_token=refresh,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=cid, client_secret=secret,
            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        credentials.refresh(GoogleAuthRequest())
    except Exception:
        return jsonify({"error": "oauth_refresh_failed"}), 502
    media_base = "https://www.googleapis.com/drive/v3/files/" + file_id
    try:
        meta = google_requests.get(
            media_base, params={"fields": "id,mimeType,size", "supportsAllDrives":"true"},
            headers={"Authorization": "Bearer " + credentials.token}, timeout=(8,15))
        if meta.status_code != 200:
            return jsonify({"error": "drive_metadata_failed"}), 502
        if meta.json().get("mimeType") != "video/mp4":
            return jsonify({"error": "source_is_not_mp4"}), 400
    except Exception:
        return jsonify({"error": "drive_metadata_unavailable"}), 502
    # Keep the bearer header out of logs and API response.
    auth = "Authorization: Bearer " + credentials.token + "\r\nAccept-Encoding: identity\r\n"
    with TemporaryDirectory(prefix="drive_audio_sample_") as temp:
        target = Path(temp) / "audio_sample.mp3"
        cmd = [
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
            "-threads", "1", "-filter_threads", "1", "-rw_timeout", "30000000",
            "-seekable", "1", "-headers", auth, "-ss", "%.3f" % start_sec,
            "-i", media_base + "?alt=media", "-map", "0:a:0",
            "-vn", "-t", "%.3f" % duration_sec,
            "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "48k",
            str(target)
        ]
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=145, check=False)
        except subprocess.TimeoutExpired:
            return jsonify({"error": "remote_audio_seek_timeout"}), 504
        except OSError:
            return jsonify({"error": "ffmpeg_not_available"}), 502
        if result.returncode != 0 or not target.exists():
            return jsonify({"error": "remote_audio_extraction_failed_or_no_audio_stream"}), 502
        raw = target.read_bytes()
        if not (len(raw) > 2000 and len(raw) <= 1500000 and
                (raw.startswith(b"ID3") or raw.startswith(b"\xff\xfb") or raw.startswith(b"\xff\xf3") or raw.startswith(b"\xff\xf2"))):
            return jsonify({"error": "invalid_extracted_mp3"}), 502
    return jsonify({
        "status": "remote_audio_extracted", "file_id": file_id,
        "start_sec": round(start_sec,3), "requested_duration_sec": round(duration_sec,3),
        "mime_type": "audio/mpeg", "byte_count": len(raw),
        "audio_base64": base64.b64encode(raw).decode("ascii"),
        "checkpoint_modified": False
    })

@app.post("/drive-segment-job")
def drive_segment_job_start():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "JSON object required"}), 400
    file_id = data.get("file_id")
    if not isinstance(file_id, str) or not _scene_re.fullmatch(
        r"[A-Za-z0-9_-]{10,200}", file_id
    ):
        return jsonify({"error": "Invalid file_id"}), 400

    try:
        start_sec = float(data.get("segment_start_sec", 0))
        duration_sec = float(data.get("segment_duration_sec", 60))
        threshold = float(data.get("threshold", 0.35))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid segment parameters"}), 400

    if (
        not all(map(math.isfinite, (start_sec, duration_sec, threshold)))
        or not 0 <= start_sec <= 86_400
        or not 15 <= duration_sec <= 300
        or not 0.1 <= threshold <= 0.8
    ):
        return jsonify({"error": "Invalid segment interval or threshold"}), 400
    if _JOB_BACKEND == "unconfigured":
        return _job_no_store_response()

    end_sec = round(start_sec + duration_sec, 3)
    task_key = f"{file_id}:{_segment_time_key(start_sec)}:{_segment_time_key(end_sec)}:{threshold}"
    details = {
        "file_id": file_id,
        "segment_start_sec": round(start_sec, 3),
        "segment_end_sec": end_sec,
        "segment_duration_sec": duration_sec,
        "threshold": threshold,
        "confirmed_checkpoint_missing": data.get("confirmed_checkpoint_missing") is True
    }
    try:
        outcome, state = _job_create("segment", task_key, details)
    except Exception:
        return _job_store_error()

    if outcome == "created":
        thread = _scene_threading.Thread(
            target=_segment_scene_worker,
            args=(state["job_id"], file_id, start_sec, duration_sec, threshold),
            daemon=True
        )
        try:
            thread.start()
        except Exception:
            _segment_scene_update(state["job_id"], status="failed", error="thread_start_failed")
            return jsonify({"error": "Could not start worker", "job_id": state["job_id"]}), 503
        app.logger.info("movie_job_started kind=segment job_id=%s", state["job_id"])
    return _job_api_response(outcome, state, "/drive-segment-job")


@app.get("/drive-segment-job")
def drive_segment_job_status():
    if _JOB_BACKEND == "unconfigured":
        return _job_no_store_response()

    job_id = request.args.get("job_id")
    task_key = request.args.get("task_key")
    if job_id and task_key:
        return jsonify({"error": "Provide only one of job_id or task_key"}), 400
    if task_key and (
        len(task_key) > 256
        or not _scene_re.fullmatch(
            r"[A-Za-z0-9_-]{10,200}:[0-9]+(?:\.[0-9]{1,3})?:"
            r"[0-9]+(?:\.[0-9]{1,3})?:0\.[0-9]+",
            task_key,
        )
    ):
        return jsonify({"error": "Invalid task_key"}), 400

    try:
        state = _job_lookup("segment", job_id=job_id, task_key=task_key)
    except Exception:
        return _job_store_error()
    if state is None and (job_id or task_key):
        return jsonify({"status": "not_found", "error": "Unknown job_id or task_key"}), 404
    return jsonify(_job_public(state))


@app.post('/drive-edit-preview')
def drive_edit_preview():
    """從 Google Drive 直接遠端 seek，拼接最多 4 段小片，回傳 MP4。

    不下載整支電影、不修改 Google Drive、不寫入 FFmpeg 工作狀態。
    POST JSON {"file_id":"...", "clips":[{"start_sec":30,"end_sec":45},...]}
    必須由 check_api_key() 驗證 X-Editor-Key。
    """
    import re
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'json_object_required'}), 400
    fid = payload.get('file_id')
    if not isinstance(fid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', fid):
        return jsonify({'error': 'invalid_file_id'}), 400
    clips = payload.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= 4:
        return jsonify({'error': 'clips_must_have_1_to_4_items'}), 400
    intervals = []
    total = 0.0
    for index, clip in enumerate(clips, 1):
        if not isinstance(clip, dict):
            return jsonify({'error': 'invalid_clip', 'clip_index': index}), 400
        a, b = clip.get('start_sec'), clip.get('end_sec')
        if type(a) not in (int, float) or type(b) not in (int, float):
            return jsonify({'error': 'invalid_clip_number', 'clip_index': index}), 400
        if not math.isfinite(a) or not math.isfinite(b) or not 0 <= a < b <= 86400 or not 3 <= b-a <= 20:
            return jsonify({'error': 'invalid_clip_interval', 'clip_index': index}), 400
        total += b - a
        intervals.append((float(a), float(b)))
    if not 3 <= total <= 75:
        return jsonify({'error': 'preview_duration_must_be_3_to_75_sec'}), 400
    if any(intervals[i][0] < intervals[i-1][1] - 0.001 for i in range(1, len(intervals))):
        return jsonify({'error': 'clips_must_be_sorted_and_non_overlapping'}), 400

    cid = os.environ.get('GOOGLE_CLIENT_ID', '')
    secret = os.environ.get('GOOGLE_CLIENT_SECRET', '')
    refresh = os.environ.get('GOOGLE_REFRESH_TOKEN', '')
    if not all((cid, secret, refresh)):
        return jsonify({'error': 'oauth_environment_missing'}), 503
    try:
        creds = Credentials(
            token=None, refresh_token=refresh,
            token_uri='https://oauth2.googleapis.com/token',
            client_id=cid, client_secret=secret,
            scopes=['https://www.googleapis.com/auth/drive.readonly'])
        creds.refresh(GoogleAuthRequest())
    except Exception:
        return jsonify({'error': 'oauth_refresh_failed'}), 502

    root = 'https://www.googleapis.com/drive/v3/files/' + fid
    try:
        metadata = google_requests.get(
            root, params={'fields': 'id,mimeType,size', 'supportsAllDrives': 'true'},
            headers={'Authorization': 'Bearer ' + creds.token},
            timeout=(8, 15))
        if metadata.status_code != 200:
            return jsonify({'error': 'drive_metadata_failed'}), 502
        if metadata.json().get('mimeType') != 'video/mp4':
            return jsonify({'error': 'source_is_not_mp4'}), 400
    except Exception:
        return jsonify({'error': 'drive_metadata_unavailable'}), 502

    # 不可在任何日誌打印 cmd，因為暫時性的 Google Bearer Token 會在 headers 參數中。
    header = 'Authorization: Bearer ' + creds.token + '\r\nAccept-Encoding: identity\r\n'
    with TemporaryDirectory(prefix='drive_preview_') as folder_name:
        folder = Path(folder_name)
        # Remote seeks can exceed 34 seconds. Limit concurrency to two small
        # encodes to keep a four-clip preview within the HTTP request budget.
        from concurrent.futures import ThreadPoolExecutor

        def render_clip(index_and_interval):
            idx, (start, end) = index_and_interval
            dest = folder / ('part_%02d.mp4' % idx)
            cmd = [
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-threads', '1', '-filter_threads', '1',
                '-rw_timeout', '60000000', '-seekable', '1',
                '-headers', header, '-ss', ('%.3f' % start),
                '-i', root + '?alt=media',
                '-t', ('%.3f' % (end-start)),
                '-map', '0:v:0', '-map', '0:a:0',
                '-vf', 'scale=640:-2', '-r', '24',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '25',
                '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-ar', '44100', '-ac', '2', '-b:a', '96k',
                '-movflags', '+faststart', str(dest)
            ]
            # Do not log cmd; its headers contain short-lived OAuth credentials.
            try:
                proc = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, timeout=70, check=False)
            except subprocess.TimeoutExpired:
                return idx, 'remote_clip_timeout', None
            except OSError:
                return idx, 'ffmpeg_unavailable', None
            if proc.returncode != 0 or not dest.is_file() or dest.stat().st_size < 1024:
                return idx, 'remote_clip_failed', None
            return idx, None, dest

        # At most two FFmpeg jobs: four parallel encoders could exceed RAM.
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(render_clip, enumerate(intervals, 1)))
        segments = []
        for idx, error, dest in results:
            if error:
                return jsonify({'error': error, 'clip_index': idx}), (504 if error == 'remote_clip_timeout' else 502)
            segments.append(dest)
        playlist = folder / 'concat.txt'
        playlist.write_text(''.join("file '%s'\n" % p.name for p in segments), encoding='utf-8')
        output = folder / 'preview.mp4'
        try:
            proc = subprocess.run([
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'concat', '-safe', '0', '-i', str(playlist),
                '-c', 'copy', '-movflags', '+faststart', str(output)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, check=False)
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'preview_concat_timeout'}), 504
        if proc.returncode != 0 or not output.exists():
            return jsonify({'error': 'preview_concat_failed'}), 502
        result = output.read_bytes()
        if not 2048 < len(result) <= 70 * 1024 * 1024:
            return jsonify({'error': 'preview_size_invalid'}), 502
        return send_file(BytesIO(result), mimetype='video/mp4',
                         as_attachment=True, download_name='movie_line_001_preview.mp4')



# ==========================================
# 自然說書試聽：不固定字幕時槽，不補靜音。
# POST /tts-story-preview: {text, voice, rate, pitch}
# 僅供內部試聽；不變更既有 /tts-timeline 與字幕輸出。
# ==========================================
@app.post('/tts-story-preview')
def tts_story_preview():
    import re
    import time
    try:
        import edge_tts
    except ImportError:
        return jsonify({'error': 'edge_tts_not_installed'}), 503

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'json_object_required'}), 400
    speech = data.get('text')
    if not isinstance(speech, str) or not 15 <= len(speech.strip()) <= 450:
        return jsonify({'error': 'invalid_story_text_length'}), 400
    voice = data.get('voice', 'zh-TW-YunJheNeural')
    if voice not in ('zh-TW-YunJheNeural', 'zh-TW-HsiaoChenNeural'):
        return jsonify({'error': 'voice_not_supported'}), 400
    rate = data.get('rate', '-5%')
    if not isinstance(rate, str) or not re.fullmatch(r'[+-]\d{1,2}%', rate) or not -50 <= int(rate[:-1]) <= 50:
        return jsonify({'error': 'invalid_tts_rate'}), 400
    pitch = data.get('pitch', '-18Hz')
    if not isinstance(pitch, str) or not re.fullmatch(r'[+-]\d{1,2}Hz', pitch) or not -50 <= int(pitch[:-2]) <= 50:
        return jsonify({'error': 'invalid_tts_pitch'}), 400
    if data.get('publication_allowed') is True:
        return jsonify({'error': 'story_preview_is_internal_only'}), 400

    with TemporaryDirectory(prefix='tts_story_') as name:
        folder = Path(name)
        raw = folder / 'story_raw.mp3'
        out = folder / 'story_preview.mp3'
        last_exc = None
        for attempt in range(1, 5):
            try:
                raw.unlink(missing_ok=True)
                edge_tts.Communicate(speech.strip(), voice=voice, rate=rate).save_sync(str(raw))
                if not raw.is_file() or raw.stat().st_size < 1024:
                    raise ValueError('tts_audio_missing_or_too_small')
                break
            except Exception as exc:
                last_exc = exc
                app.logger.warning('Story TTS failed attempt %s/4 type=%s',
                                   attempt, type(exc).__name__, exc_info=True)
                if attempt < 4:
                    time.sleep((2, 4, 8)[attempt - 1])
        else:
            return jsonify({'error': 'tts_synthesis_failed', 'attempts': 4,
                            'exception_type': type(last_exc).__name__}), 502

        pitch_delta = int(pitch[:-2])
        pitch_ratio = 1.0 + pitch_delta / 180.0
        # 與 v29b 相同的 FFmpeg 音高近似處理，補償語速以維持自然語句時長。
        audio_filter = ('asetrate=24000*%.6f,aresample=24000,atempo=%.6f'
                        % (pitch_ratio, 1.0 / pitch_ratio)) if pitch_delta else 'anull'
        try:
            proc = subprocess.run([
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-i', str(raw), '-af', audio_filter,
                '-ar', '24000', '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '128k',
                str(out)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
               check=False, timeout=60)
            if proc.returncode != 0 or not out.is_file() or out.stat().st_size < 1024:
                return jsonify({'error': 'story_audio_processing_failed'}), 502
            return send_file(BytesIO(out.read_bytes()), mimetype='audio/mpeg',
                             as_attachment=True, download_name='story_preview.mp3')
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'story_audio_processing_timeout'}), 504


@app.post('/tts-timeline')
def tts_timeline():
    """分段男聲 TTS，各段不足時以靜音補足，禁止截斷旁白。

    POST JSON {"captions":[{"clip_id":1,"text":"...","start_sec":0,"end_sec":15},...],
               "target_duration_sec":60,"voice":"zh-TW-YunJheNeural","rate":"+0%"}
    回傳對齊單一 MP3，供現有 /audio-info 與 /mix-narration 直接使用。
    """
    import re
    try:
        import edge_tts
    except ImportError:
        return jsonify({'error': 'edge_tts_not_installed'}), 503
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'json_object_required'}), 400
    caps = data.get('captions')
    if not isinstance(caps, list) or not 1 <= len(caps) <= 8:
        return jsonify({'error': 'captions_must_have_1_to_8_items'}), 400
    voice = data.get('voice', 'zh-TW-YunJheNeural')
    if voice not in ('zh-TW-YunJheNeural', 'zh-TW-HsiaoChenNeural'):
        return jsonify({'error': 'voice_not_supported'}), 400
    rate = data.get('rate', '+0%')
    if not isinstance(rate, str) or not re.fullmatch(r'[+-]\d{1,2}%', rate) or not -50 <= int(rate[:-1]) <= 50:
        return jsonify({'error': 'invalid_tts_rate'}), 400
    # 可選的 Edge TTS 音高，預設不改變既有工作流行為。
    pitch = data.get('pitch', '+0Hz')
    if not isinstance(pitch, str) or not re.fullmatch(r'[+-]\d{1,2}Hz', pitch) or not -50 <= int(pitch[:-2]) <= 50:
        return jsonify({'error': 'invalid_tts_pitch'}), 400
    target = data.get('target_duration_sec')
    if type(target) not in (int, float) or not math.isfinite(target) or not 3 <= target <= 80:
        return jsonify({'error': 'invalid_target_duration'}), 400
    last_end = 0.0
    specs = []
    for i, cap in enumerate(caps, 1):
        if not isinstance(cap, dict) or cap.get('clip_id') != i:
            return jsonify({'error': 'clip_id_not_sequential', 'clip_index': i}), 400
        txt, start, end = cap.get('text'), cap.get('start_sec'), cap.get('end_sec')
        if not isinstance(txt, str) or not txt.strip() or len(txt) > 110:
            return jsonify({'error': 'invalid_caption', 'clip_index': i}), 400
        if type(start) not in (int, float) or type(end) not in (int, float):
            return jsonify({'error': 'invalid_caption_interval', 'clip_index': i}), 400
        if not all(math.isfinite(z) for z in (start, end)) or abs(start-last_end) > 0.005 or not end > start or end-start > 25:
            return jsonify({'error': 'caption_timeline_gap_or_overlap', 'clip_index': i}), 400
        specs.append((txt.strip(), float(end-start)))
        last_end = float(end)
    if abs(last_end-float(target)) > 0.005:
        return jsonify({'error': 'caption_duration_total_mismatch'}), 400

    with TemporaryDirectory(prefix='tts_timeline_') as name:
        folder = Path(name)
        wav_files = []
        for i, (txt, slot) in enumerate(specs, 1):
            mp3 = folder / ('part_%02d.mp3' % i)
            wav = folder / ('part_%02d.wav' % i)
            # 逐段 TTS 對遠端錯誤做有限重試並回報錯誤種類。
            generated = False
            last_tts_exception = None
            for attempt in range(1, 5):
                try:
                    mp3.unlink(missing_ok=True)
                    edge_tts.Communicate(txt, voice=voice, rate=rate, pitch='+0Hz').save_sync(str(mp3))
                    if not mp3.is_file() or mp3.stat().st_size < 1024:
                        raise ValueError('tts_audio_missing_or_too_small')
                    generated = True
                    break
                except Exception as exc:
                    last_tts_exception = exc
                    app.logger.warning(
                        'Timeline TTS synthesis failed: clip=%s attempt=%s/4 type=%s',
                        i, attempt, type(exc).__name__, exc_info=True,
                    )
                    if attempt < 4:
                        import time
                        time.sleep((2, 4, 8)[attempt - 1])
            if not generated:
                # 不回傳例外原文（可能含遠端 URL／請求細節），
                # 只暴露可供定位的異常類型；詳細堆疊仍留在 Render Logs。
                return jsonify({
                    'error': 'tts_synthesis_failed',
                    'clip_index': i, 'attempts': 4, 'stage': 'edge_tts',
                    'exception_type': type(last_tts_exception).__name__
                }), 502
            try:
                info = probe_media_info(mp3)
            except Exception:
                app.logger.exception('Timeline TTS audio probe failed: clip=%s', i)
                return jsonify({
                    'error': 'tts_audio_probe_failed',
                    'clip_index': i, 'stage': 'ffprobe'
                }), 502
            spoken = info['duration_sec']
            if spoken > slot - 0.10:
                return jsonify({'error': 'caption_audio_exceeds_slot',
                                'clip_index': i, 'audio_duration_sec': spoken,
                                'slot_duration_sec': round(slot, 3)}), 422
            # 對非零音高使用本機 FFmpeg 處理，避免 Edge TTS 遠端 pitch 請求失敗。
            # 換算為近似比例（-18Hz -> 0.90）；維持朗讀長度不變。
            pitch_delta = int(pitch[:-2])
            pitch_ratio = 1.0 + pitch_delta / 180.0
            if pitch_delta:
                pitch_filter = (
                    'asetrate=24000*%.6f,aresample=24000,atempo=%.6f,'
                    'apad,atrim=duration=%.3f'
                ) % (pitch_ratio, 1.0 / pitch_ratio, slot)
            else:
                pitch_filter = 'apad,atrim=duration=%.3f' % slot
            try:
                proc = subprocess.run([
                    'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-i', str(mp3), '-af', pitch_filter,
                    '-ar', '24000', '-ac', '1', '-c:a', 'pcm_s16le', str(wav)
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False)
            except subprocess.TimeoutExpired:
                return jsonify({'error': 'tts_padding_timeout', 'clip_index': i}), 504
            if proc.returncode != 0 or not wav.exists():
                return jsonify({'error': 'tts_padding_failed', 'clip_index': i}), 502
            wav_files.append(wav)
        playlist = folder / 'audio_concat.txt'
        playlist.write_text(''.join("file '%s'\n" % p.name for p in wav_files), encoding='utf-8')
        result = folder / 'narration_timeline.mp3'
        try:
            proc = subprocess.run([
                'ffmpeg', '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'concat', '-safe', '0', '-i', str(playlist),
                '-c:a', 'libmp3lame', '-b:a', '128k', '-ar', '24000', str(result)
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=40, check=False)
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'tts_concat_timeout'}), 504
        if proc.returncode != 0 or not result.is_file():
            return jsonify({'error': 'tts_concat_failed'}), 502
        final_info = probe_media_info(result)
        if abs(final_info['duration_sec'] - float(target)) > 0.15:
            return jsonify({'error': 'tts_timeline_duration_mismatch',
                            'actual_sec': final_info['duration_sec']}), 502
        return send_file(BytesIO(result.read_bytes()), mimetype='audio/mpeg',
                         as_attachment=True, download_name='narration_timeline.mp3')

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )

