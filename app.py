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

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=False
    )
