import json
import sys
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = base_dir()
ASSETS_DIR = BASE_DIR / "assets"
DATA_DIR = BASE_DIR / "data"
COOKIES_FILE = ASSETS_DIR / "cookies.txt"
FFMPEG_FILE = ASSETS_DIR / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")

DEFAULTS = {
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "proxy": "",
    "max_concurrent": 3,
    "request_delay_ms": 1500,
    "quality": "MP3 320kbps",
    "normalize_audio": False,
    "shutdown_after": False,
    "download_dir": str(Path.home() / "Music" / "Titan Downloader"),
}


class Config:
    def __init__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._path = DATA_DIR / "config.json"
        self._data = dict(DEFAULTS)
        if self._path.exists():
            try:
                self._data.update(json.loads(self._path.read_text(encoding="utf-8")))
            except Exception:
                pass
        self.save()

    def get(self, key, fallback=None):
        return self._data.get(key, DEFAULTS.get(key, fallback))

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def save(self):
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
