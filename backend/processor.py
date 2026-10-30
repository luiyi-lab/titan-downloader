import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TRCK

from . import FFMPEG_FILE


def ffmpeg_path():
    if FFMPEG_FILE.exists():
        return str(FFMPEG_FILE)
    found = shutil.which("ffmpeg")
    return found or "ffmpeg"


def sanitize(name):
    clean = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(name)).strip().rstrip(".")
    return clean[:150] or "descarga"


MEDIA_SIGNATURES = (
    b"ftyp",            # mp4 / m4a / mov
    b"\x1a\x45\xdf\xa3",  # webm / mkv (EBML)
    b"ID3",             # mp3 con etiquetas
    b"\xff\xfb", b"\xff\xf3", b"\xff\xf1",  # mp3 / aac ADTS
    b"OggS",            # ogg / opus
    b"fLaC",
    b"RIFF",            # avi / wav
)


def file_is_media(path, min_size=16 * 1024):
    """Comprueba tamaño mínimo y firma binaria: evita que un motor de
    rescate 'complete' con una página HTML descargada en vez del medio."""
    try:
        if path.stat().st_size < min_size:
            return False
        with open(path, "rb") as fh:
            head = fh.read(16)
    except OSError:
        return False
    return any(sig in head for sig in MEDIA_SIGNATURES)


class MediaProcessor:
    def __init__(self, download_dir):
        self.download_dir = Path(download_dir)

    def process(self, src, quality, normalize, metadata):
        self.download_dir.mkdir(parents=True, exist_ok=True)
        src = Path(src)
        if not src.exists():
            raise RuntimeError(f"Archivo temporal no encontrado: {src}")
        if not file_is_media(src):
            raise RuntimeError(
                "El archivo descargado no es un medio válido (firma binaria "
                "incorrecta) — probable bloqueo anti-bot de la plataforma. "
                "Prueba ⚙️ Configuración → Cookies e inténtalo de nuevo")
        metadata = metadata or {}
        base = self._target_base(src, metadata)
        is_video = quality.startswith("Video")
        suffix = ".mp4" if is_video else ".mp3"
        out = self._reserve_unique(base.with_suffix(suffix))
        try:
            if is_video:
                self._convert_video(src, out, normalize)
            else:
                m = re.search(r"(\d+)\s*kbps", quality, re.IGNORECASE)
                bitrate = m.group(1) if m else "320"
                self._convert_audio(src, out, bitrate, normalize)
            if suffix == ".mp3":
                self._tag_mp3(out, metadata)
        except Exception:
            try:
                out.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
        return out

    def _reserve_unique(self, base):
        candidate = base
        n = 1
        while True:
            try:
                with open(candidate, "x"):
                    pass
                return candidate
            except FileExistsError:
                candidate = base.with_name(f"{base.stem} ({n}){base.suffix}")
                n += 1

    def _ffmpeg(self, src, out_args):
        cmd = [ffmpeg_path(), "-y", "-i", str(src)] + out_args
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            creationflags=(0x08000000 if sys.platform == "win32" else 0),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or "")[-800:]
            raise RuntimeError(f"FFmpeg falló: {tail}")

    def _target_base(self, src, metadata):
        title = str(metadata.get("title") or "").strip()
        stem = sanitize(title) if title else sanitize(src.stem)
        return self.download_dir / stem

    def _convert_audio(self, src, out, bitrate, normalize):
        args = ["-vn", "-codec:a", "libmp3lame", "-b:a", f"{bitrate}k"]
        if normalize:
            args += ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
        args += ["-id3v2_version", "3", str(out)]
        self._ffmpeg(src, args)

    def _convert_video(self, src, out, normalize):
        if normalize:
            args = ["-c:v", "copy", "-c:a", "aac", "-af",
                    "loudnorm=I=-14:TP=-1.5:LRA=11", "-movflags", "+faststart",
                    str(out)]
        else:
            args = ["-c", "copy", "-movflags", "+faststart", str(out)]
        self._ffmpeg(src, args)

    def _tag_mp3(self, path, meta):
        meta = meta or {}
        try:
            tags = ID3(str(path))
        except Exception:
            tags = ID3()
        if meta.get("title"):
            tags.add(TIT2(encoding=3, text=str(meta["title"])))
        if meta.get("artist"):
            tags.add(TPE1(encoding=3, text=str(meta["artist"])))
        if meta.get("album"):
            tags.add(TALB(encoding=3, text=str(meta["album"])))
        if meta.get("date"):
            tags.add(TDRC(encoding=3, text=str(meta["date"])[:10]))
        if meta.get("track"):
            tags.add(TRCK(encoding=3, text=str(meta["track"])))
        cover_url = meta.get("cover_url") or meta.get("thumbnail")
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=15, stream=True)
                data = b""
                for chunk in resp.iter_content(65536):
                    data += chunk
                    if len(data) > 5 * 1024 * 1024:
                        break
                mime = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
                if data:
                    tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover",
                                  data=data))
            except Exception:
                pass
        tags.save(str(path), v2_version=3)
