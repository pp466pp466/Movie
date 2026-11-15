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
