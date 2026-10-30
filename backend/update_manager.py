import io
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests
from PySide6.QtCore import QThread, Signal

from . import ASSETS_DIR, FFMPEG_FILE

YT_DLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YT_DLP_RELEASES_API = ("https://api.github.com/repos/yt-dlp/"
                       "yt-dlp/releases/latest")
YT_DLP_PYPI_API = "https://pypi.org/pypi/yt-dlp/json"
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"


def latest_yt_dlp_version():
    r = requests.get(YT_DLP_RELEASES_API,
                     headers={"User-Agent": "TitanDownloader"}, timeout=15)
    r.raise_for_status()
    return str(r.json().get("tag_name", "")).strip()


def latest_pypi_version(pkg):
    r = requests.get(f"https://pypi.org/pypi/{pkg}/json", timeout=15)
    r.raise_for_status()
    return str(r.json()["info"]["version"]).strip()


def binary_version(exe_path):
    try:
        proc = subprocess.run(
            [str(exe_path), "--version"], capture_output=True, text=True,
            timeout=45,
            creationflags=(0x08000000 if sys.platform == "win32" else 0))
        return proc.stdout.strip()
    except Exception:
        return None


def yt_dlp_command():
    if getattr(sys, "frozen", False):
        exe = ASSETS_DIR / ("yt-dlp.exe" if sys.platform == "win32" else "yt-dlp")
        if exe.exists():
            return [str(exe)]
        return None
    return [sys.executable, "-m", "yt_dlp"]


class UpdateManager(QThread):
    output = Signal(str)
    update_finished = Signal(bool)

    def __init__(self, force_ffmpeg=False, parent=None):
        super().__init__(parent)
        self._force_ffmpeg = force_ffmpeg
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        ok = True
        try:
            if getattr(sys, "frozen", False):
                self._update_binaries()
            else:
                ok = self._pip_upgrade()
        except Exception as e:
            self.output.emit(f"✖ Error actualizando motores: {e}")
            ok = False
        self.update_finished.emit(ok)

    def _emit(self, line):
        self.output.emit(line)

    def _check_cancel(self):
        if self._cancelled:
            raise RuntimeError("Actualización cancelada por el usuario")

    def _pip_upgrade(self):
        ok = True
        try:
            local = __import__("importlib.metadata", fromlist=["version"]) \
                .version("yt-dlp")
        except Exception:
            local = None
        latest = None
        if local:
            try:
                latest = latest_pypi_version("yt-dlp")
            except Exception:
                pass
            if latest and local.strip() == latest:
                self._emit(f"✔ yt-dlp ya está en la última versión "
                           f"({latest}); no es necesario actualizar.")
                return True
            if latest:
                self._emit(f"Actualizando yt-dlp: {local} → {latest}")
        for pkg in ("yt-dlp",):
            self._check_cancel()
            self._emit(f"$ pip install --upgrade {pkg}")
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "--upgrade", pkg],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                self._emit(line.rstrip())
            proc.wait()
            if proc.returncode != 0:
                ok = False
                self._emit(f"✖ pip devolvió código {proc.returncode} para {pkg}")
            else:
                self._emit(f"✔ {pkg} actualizado")
        try:
            import yt_dlp
            self._emit(f"Versión yt-dlp: {yt_dlp.version.__version__}")
        except Exception:
            pass
        return ok

    def _download_stream(self, url, dst: Path, label):
        with requests.get(url, stream=True, timeout=30,
                          headers={"User-Agent": "TitanDownloader"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length", 0) or 0)
            done = 0
            buf = io.BytesIO()
            last_pct = -1
            for chunk in r.iter_content(262144):
                self._check_cancel()
                buf.write(chunk)
                done += len(chunk)
                if total:
                    pct = int(done * 100 / total)
                    if pct != last_pct and pct % 5 == 0:
                        last_pct = pct
                        self._emit(f"{label}: {pct}% ({done // 1048576}/"
                                   f"{total // 1048576} MB)")
            dst.write_bytes(buf.getvalue())
        self._emit(f"✔ Descargado {label} ({done // 1048576} MB)")

    def _update_binaries(self):
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        ytdlp_dst = ASSETS_DIR / "yt-dlp.exe"
        latest = None
        try:
            latest = latest_yt_dlp_version()
        except Exception as e:
            self._emit(f"⚠ No pude consultar la última versión "
                       f"({e}); descargaré por si acaso.")
        if ytdlp_dst.exists():
            current = binary_version(ytdlp_dst)
            cur_disp = current or "versión desconocida"
            self._emit(f"yt-dlp instalado: {cur_disp}")
            if latest:
                self._emit(f"Última disponible: {latest}")
            if current is None:
                self._emit("⚠ El binario no responde; lo reemplazaré.")
                need_ytdlp = True
            elif latest and current.strip() != latest.strip():
                need_ytdlp = True
            else:
                need_ytdlp = False
                if latest is None:
                    self._emit("✔ No se pudo verificar la última versión "
                               "(sin conexión o límite de la API); se "
                               "mantiene la instalada.")
                else:
                    self._emit("✔ yt-dlp ya está actualizado. No es "
                               "necesario descargar nada.")
        else:
            need_ytdlp = True

        tmp = ytdlp_dst.with_suffix(".new")
        if need_ytdlp:
            self._check_cancel()
            self._emit("Descargando yt-dlp (binario standalone)...")
            self._download_stream(YT_DLP_URL, tmp, "yt-dlp")
            tmp.replace(ytdlp_dst)
            ver = subprocess.run(
                [str(ytdlp_dst), "--version"], capture_output=True,
                text=True, timeout=60,
                creationflags=(0x08000000 if sys.platform == "win32"
                               else 0))
            self._emit(f"✔ yt-dlp actualizado a v{ver.stdout.strip()}")
        else:
            tmp.unlink(missing_ok=True)

        need_ffmpeg = self._force_ffmpeg or not FFMPEG_FILE.exists()
        if not need_ffmpeg:
            self._emit("FFmpeg ya está presente en assets/ (omitido).")
            return
        self._check_cancel()
        self._emit("Descargando FFmpeg (~80 MB, puede tardar)...")
        zip_path = ASSETS_DIR / "ffmpeg.zip"
        self._download_stream(FFMPEG_URL, zip_path, "ffmpeg")
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                base = Path(name).name.lower()
                if base in ("ffmpeg.exe", "ffprobe.exe"):
                    target = ASSETS_DIR / base
                    with zf.open(name) as srcfh, open(target, "wb") as outfh:
                        shutil.copyfileobj(srcfh, outfh)
                    self._emit(f"✔ Extraído {base}")
        zip_path.unlink(missing_ok=True)
