"""Render build-time fetch of an official Noto CJK TC font (SIL OFL 1.1).
Run after pip install, before deployment: python download_fonts.py
"""
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
import os
import time

ROOT = Path(__file__).resolve().parent
FONTS = ROOT / "fonts"
FONT = FONTS / "NotoSansCJKtc-Regular.otf"
FONT_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/main/"
    "Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf"
)
LICENSE = FONTS / "OFL_LICENSE.txt"
LICENSE_URL = (
    "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Sans/LICENSE"
)


def download_file(url, target, minimum_bytes=1000):
    error = None
    for attempt in range(3):
        tmp = target.with_name(target.name + ".part")
        try:
            req = Request(url, headers={"User-Agent": "Movie-Editor-Font-Setup/1.0"})
            with urlopen(req, timeout=120) as reply, tmp.open("wb") as stream:
                while True:
                    chunk = reply.read(1024 * 256)
                    if not chunk:
                        break
                    stream.write(chunk)
            if tmp.stat().st_size < minimum_bytes:
                raise ValueError("下載檔案太小")
            tmp.replace(target)
            return
        except (OSError, URLError, ValueError) as exc:
            error = exc
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt < 2:
                time.sleep(2)
    raise RuntimeError(f"無法下載 {url}: {error}")


def main():
    FONTS.mkdir(parents=True, exist_ok=True)
    if not FONT.exists() or FONT.stat().st_size < 1000000:
        print("開始從 Noto 官方儲存庫下載台灣正體中文字型...")
        download_file(FONT_URL, FONT, minimum_bytes=1000000)
    with FONT.open("rb") as f:
        signature = f.read(4)
    if signature not in (b"OTTO", b"\x00\x01\x00\x00", b"ttcf"):
        raise RuntimeError("下載的檔案不是有效的 OpenType/TrueType 字型")
    if not LICENSE.exists():
        download_file(LICENSE_URL, LICENSE)
    print(f"字型已備妥：{FONT.name}, {FONT.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
