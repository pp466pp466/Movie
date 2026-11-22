"""movie-line v35: bounded Google Drive MP4 concatenation (read-only sources)."""
from flask import request, jsonify, send_file
from pathlib import Path
from tempfile import TemporaryDirectory
from io import BytesIO
import os
import re
import json
import math
import subprocess
import hmac

import requests as google_requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest


def register_concat_routes(app):
    @app.post("/drive-concat-group")
    def drive_concat_group():
        expected_key = os.environ.get("VIDEO_EDITOR_API_KEY", "")
        provided_key = request.headers.get("X-Editor-Key", "")
        if not expected_key or not hmac.compare_digest(expected_key, provided_key):
            return jsonify({"error": "Unauthorized"}), 401

        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"error": "json_object_required"}), 400
        items = body.get("files")
        if not isinstance(items, list) or not 2 <= len(items) <= 4:
            return jsonify({"error": "files_must_contain_2_to_4_items"}), 400
        group = body.get("group")
        if not isinstance(group, str) or not re.fullmatch(r"(chapters_0[1-4]_part|chapters_0[5-8]_part|chapters_09_12_part|full_12_chapters)", group):
            return jsonify({"error": "invalid_group"}), 400
        ids = []
        for i, obj in enumerate(items, 1):
            if not isinstance(obj, dict) or not isinstance(obj.get("file_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", obj["file_id"]):
                return jsonify({"error": "invalid_file_id", "index": i}), 400
            ids.append(obj["file_id"])
        if len(set(ids)) != len(ids):
            return jsonify({"error": "duplicate_file_ids"}), 400
        if (group == "full_12_chapters" and len(ids) != 3) or (group != "full_12_chapters" and len(ids) != 4):
            return jsonify({"error": "unexpected_group_size"}), 400

        cid, secret, refresh = (os.environ.get(k, "") for k in
                                ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"))
        if not all((cid, secret, refresh)):
            return jsonify({"error": "oauth_environment_missing"}), 503
        try:
            credentials = Credentials(token=None, refresh_token=refresh,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=cid, client_secret=secret,
                scopes=["https://www.googleapis.com/auth/drive.readonly"])
            credentials.refresh(GoogleAuthRequest())
        except Exception:
            app.logger.exception("concat oauth refresh failed")
            return jsonify({"error": "oauth_refresh_failed"}), 502

        headers = {"Authorization": "Bearer " + credentials.token}
        limit_per_file = 100 * 1024 * 1024
        limit_total = 240 * 1024 * 1024
        total_size = 0
        sizes = []
        try:
            for fid in ids:
                res = google_requests.get("https://www.googleapis.com/drive/v3/files/" + fid,
                    params={"fields": "id,mimeType,size,name", "supportsAllDrives": "true"},
                    headers=headers, timeout=(8, 20))
                if res.status_code != 200:
                    return jsonify({"error": "drive_file_metadata_failed"}), 502
                info = res.json()
                if info.get("mimeType") != "video/mp4":
                    return jsonify({"error": "not_mp4"}), 400
                size = int(info.get("size") or 0)
                if not 2048 < size <= limit_per_file:
                    return jsonify({"error": "invalid_source_file_size"}), 400
                sizes.append(size)
                total_size += size
            if total_size > limit_total:
                return jsonify({"error": "concat_total_source_size_too_large"}), 413
        except (ValueError, KeyError, google_requests.RequestException):
            return jsonify({"error": "drive_metadata_unavailable"}), 502

        with TemporaryDirectory(prefix="movie_v35_concat_") as tmp:
            folder = Path(tmp)
            parts = []
            for idx, (fid, advertised_size) in enumerate(zip(ids, sizes), 1):
                target = folder / ("part_%02d.mp4" % idx)
                media_url = "https://www.googleapis.com/drive/v3/files/" + fid
                try:
                    with google_requests.get(media_url,
                        params={"alt":"media", "supportsAllDrives":"true"},
                        headers=headers, timeout=(15, 120), stream=True) as stream:
                        if stream.status_code != 200:
                            return jsonify({"error":"drive_download_failed", "index":idx}), 502
                        length = 0
                        with target.open("wb") as output:
                            for chunk in stream.iter_content(chunk_size=262144):
                                if not chunk:
                                    continue
                                length += len(chunk)
                                if length > advertised_size or length > limit_per_file:
                                    return jsonify({"error":"download_size_overflow", "index":idx}), 502
                                output.write(chunk)
                        if length != advertised_size:
                            return jsonify({"error":"download_size_mismatch", "index":idx}), 502
                except (OSError, google_requests.RequestException):
                    return jsonify({"error":"drive_download_unavailable", "index":idx}), 502
                parts.append(target)

            durations = []
            video_fingerprints = []
            try:
                for idx, part in enumerate(parts, 1):
                    probe = subprocess.run(["ffprobe","-v","error","-show_streams",
                        "-show_format","-of","json",str(part)],
                        capture_output=True,text=True,check=True,timeout=25)
                    obj = json.loads(probe.stdout)
                    duration = float(obj["format"]["duration"])
                    streams = obj.get("streams", [])
                    vs = next((x for x in streams if x.get("codec_type")=="video"),None)
                    aus = next((x for x in streams if x.get("codec_type")=="audio"),None)
                    if not vs or not aus or not math.isfinite(duration) or duration <= 1:
                        return jsonify({"error":"invalid_av_streams", "index":idx}), 422
                    durations.append(duration)
                    video_fingerprints.append((vs.get("codec_name"), vs.get("width"), vs.get("height"),
                        vs.get("pix_fmt"), aus.get("codec_name"), aus.get("sample_rate"),
                        aus.get("channels")))
            except (OSError, ValueError, KeyError, subprocess.SubprocessError, json.JSONDecodeError):
                return jsonify({"error":"ffprobe_failed"}), 502
            if len(set(video_fingerprints)) != 1:
                return jsonify({"error":"incompatible_chapter_streams",
                    "hint":"Video/audio stream formats differ; re-encoding is required before copy concatenation"}), 422

            playlist = folder / "concat.txt"
            playlist.write_text("".join("file '%s'\n" % p.name for p in parts), encoding="utf-8")
            output = folder / "joined.mp4"
            try:
                result = subprocess.run(["ffmpeg","-nostdin","-hide_banner","-loglevel","error","-y",
                    "-f","concat","-safe","0","-i",str(playlist),
                    "-map","0:v:0","-map","0:a:0","-c","copy",
                    "-movflags","+faststart",str(output)],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False,timeout=120)
                if result.returncode != 0 or not output.exists():
                    return jsonify({"error":"concat_failed"}), 502
                p = subprocess.run(["ffprobe","-v","error","-show_entries",
                    "format=duration","-of","default=noprint_wrappers=1:nokey=1",str(output)],
                    capture_output=True,text=True,check=True,timeout=25)
                actual = float(p.stdout.strip())
            except (OSError, ValueError, subprocess.SubprocessError):
                return jsonify({"error":"concat_or_probe_timeout"}), 504
            expected = sum(durations)
            if not math.isfinite(actual) or abs(actual - expected) > max(1.5, 0.25 * len(parts)):
                return jsonify({"error":"concat_duration_mismatch",
                    "expected_sec":round(expected,3),"actual_sec":round(actual,3)}), 422
            if not 2048 < output.stat().st_size <= limit_total:
                return jsonify({"error":"output_size_invalid"}), 413
            response = send_file(BytesIO(output.read_bytes()),mimetype="video/mp4",
                as_attachment=True, download_name="movie_line_v35_%s.mp4" % group)
            response.headers["X-Movie-Line-Group"] = group
            response.headers["X-Movie-Line-Duration-Sec"] = "%.3f" % actual
            response.headers["X-Movie-Line-Source-Count"] = str(len(parts))
            return response
