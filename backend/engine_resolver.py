import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from . import COOKIES_FILE, DATA_DIR, FFMPEG_FILE, Config
from .processor import file_is_media
from .rate_limiter import RateLimiter
from .update_manager import yt_dlp_command

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MEDIA_EXTS = (".mp4", ".mp3", ".m4a", ".webm", ".mkv", ".ogg", ".opus", ".aac")
MAX_EXPAND = 20000
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://pipedapi.adminforge.de",
    "https://api.piped.private.coffee",
    "https://pipedapi.drgns.space",
    "https://piped-api.lunar.icu",
]


class Cancelled(Exception):
    pass


class EngineError(Exception):
    pass


def detect_platform(url):
    low = url.lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if "open.spotify.com" in low or "spotify.com" in low:
        return "spotify"
    if "facebook.com" in low or "fb.watch" in low:
        return "facebook"
    if "tiktok.com" in low:
        return "tiktok"
    return "generic"


def classify_error(exc):
    text = str(exc).lower()
    if "429" in text or "too many requests" in text:
        return "rate"
    if "403" in text or "forbidden" in text:
        return "forbidden"
    if "confirm your age" in text or "agerestricted" in text or "age-restricted" in text:
        return "age"
    if "sign in to confirm" in text or ("cookies" in text and "bot" in text):
        return "bot"
    if "private" in text or "unavailable" in text or "not found" in text or "404" in text:
        return "unavailable"
    if "geo" in text and "restrict" in text:
        return "geo"
    return "generic"


def friendly_error(exc):
    code = classify_error(exc) if exc is not None else "generic"
    raw = str(exc or "")
    if code == "bot":
        return ("YouTube está pidiendo verificación anti-bot para tu "
                "conexión (bloqueo temporal del lado de YouTube, no es un "
                "fallo del programa). Solución: ⚙️ Configuración → Cookies "
                "→ 'Importar desde Firefox' y vuelve a intentarlo.")
    if code == "age":
        return ("El contenido tiene restricción de edad. Exporta cookies "
                "en ⚙️ Configuración → Cookies e inténtalo de nuevo.")
    if code == "rate":
        return ("YouTube está limitando tu IP por demasiadas peticiones "
                "(429). Espera unos minutos; el delay anti-baneo se "
                "incrementó automáticamente.")
    if code == "unavailable":
        return f"El contenido no está disponible: {raw[:300]}"
    return f"No se pudo resolver: {raw[:400]}"


def ffmpeg_location():
    return str(FFMPEG_FILE) if FFMPEG_FILE.exists() else shutil.which("ffmpeg") or "ffmpeg"


class EngineResolver:
    STEP_TIMEOUT_S = 600

    def __init__(self, config: Config, limiter: RateLimiter):
        self.config = config
        self.limiter = limiter

    def _run_with_timeout(self, fn, name, timeout=None):
        timeout = timeout or self.STEP_TIMEOUT_S
        box = {}

        def target():
            try:
                box["res"] = fn()
            except BaseException as e:
                box["err"] = e

        th = threading.Thread(target=target, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            raise EngineError(
                f"el motor '{name}' superó {timeout}s sin responder "
                f"(posible bloqueo del servidor hacia tu conexión)")
        if "err" in box:
            raise box["err"]
        return box["res"]

    def _log(self, emitter, msg, color):
        if emitter is not None:
            emitter.emit(msg, color)

    NO_API_MSG = ("No se pudo acceder a la API de Spotify. Para listas y "
                  "álbumes configura Client ID y Secret propios en "
                  "⚙️ Configuración → pestaña Spotify")
    NO_CREDS_MSG = ("Faltan credenciales de Spotify. Configura Client ID y "
                    "Secret en ⚙️ Configuración → pestaña Spotify")

    def _spotify_creds(self):
        cid = self.config.get("spotify_client_id") or None
        cs = self.config.get("spotify_client_secret") or None
        if cid and cs:
            return cid, cs
        try:
            from spotdl.utils.config import DEFAULT_CONFIG
            return (cid or DEFAULT_CONFIG.get("client_id"),
                    cs or DEFAULT_CONFIG.get("client_secret"))
        except Exception:
            return cid, cs

    @staticmethod
    def _normalize_spotify_url(url):
        url = re.sub(r"/intl-[a-zA-Z-]+/", "/", url)
        return url.split("?")[0]

    @staticmethod
    def _spotify_track_id(url):
        m = re.search(r"/track/([A-Za-z0-9]+)", url)
        return m.group(1) if m else None

    def _spotify_embed_meta(self, url):
        tid = self._spotify_track_id(url)
        if not tid:
            return None
        try:
            resp = requests.get(
                f"https://open.spotify.com/embed/track/{tid}",
                headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
            m = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text, re.S)
            if not m:
                return None
            data = json.loads(m.group(1))
            entity = (data.get("props", {}).get("pageProps", {})
                          .get("state", {}).get("data", {}).get("entity", {}))
            name = entity.get("title") or entity.get("name") or ""
            if not name:
                return None
            artists = ", ".join(a.get("name", "") for a in
                                entity.get("artists") or [])
            rel = entity.get("releaseDate") or {}
            date = str(rel.get("isoString", "") or "")[:10]
            cover = ""
            vi = entity.get("visualIdentity") or {}
            img = vi.get("image") if isinstance(vi, dict) else None
            if isinstance(img, list) and img:
                best = max(img, key=lambda s: s.get("maxWidth") or 0)
                cover = best.get("url", "")
            elif isinstance(img, dict):
                srcs = img.get("sources") or []
                if srcs:
                    cover = srcs[0].get("url", "")
                elif img.get("url"):
                    cover = img.get("url")
            return {"title": name, "artist": artists, "album": "",
                    "date": date, "track": "", "cover_url": cover}
        except Exception:
            return None

    def _spotify_oembed_meta(self, url):
        try:
            r = requests.get("https://open.spotify.com/oembed",
                             params={"url": self._normalize_spotify_url(url)},
                             headers={"User-Agent": USER_AGENT}, timeout=15)
            r.raise_for_status()
            d = r.json()
            title = d.get("title", "")
            if not title:
                return None
            return {"title": title, "artist": "", "album": "", "date": "",
                    "track": "", "cover_url": d.get("thumbnail_url", "")}
        except Exception:
            return None

    def _spotipy_client(self):
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        cid, cs = self._spotify_creds()
        if not cid or not cs:
            raise EngineError(self.NO_CREDS_MSG)
        try:
            return spotipy.Spotify(
                auth_manager=SpotifyClientCredentials(cid, cs),
                requests_timeout=6, retries=0)
        except Exception as e:
            raise EngineError(f"Credenciales de Spotify inválidas: {e}")

    def _spotify_api_meta(self, url):
        sp = self._spotipy_client()
        t = sp.track(self._normalize_spotify_url(url))
        artists = ", ".join(a["name"] for a in t.get("artists", []))
        alb = t.get("album") or {}
        images = alb.get("images") or []
        return {
            "title": t.get("name", ""),
            "artist": artists,
            "album": alb.get("name", ""),
            "date": str(alb.get("release_date", "") or ""),
            "track": t.get("track_number", ""),
            "cover_url": images[0].get("url", "") if images else "",
        }

    def _spotify_track_meta(self, url):
        if self.config.get("spotify_client_id"):
            try:
                return self._spotify_api_meta(url)
            except Exception:
                pass
        meta = self._spotify_embed_meta(url)
        if meta and not meta.get("cover_url"):
            oe = self._spotify_oembed_meta(url)
            if oe and oe.get("cover_url"):
                meta["cover_url"] = oe["cover_url"]
        if meta is None:
            meta = self._spotify_oembed_meta(url)
        if meta:
            return meta
        try:
            return self._spotify_api_meta(url)
        except Exception:
            pass
        return None

    def expand_url(self, url):
        platform = detect_platform(url)
        if platform == "spotify":
            return self._expand_spotify(url)
        if platform == "tiktok":
            return [{"url": self._normalize_tiktok_url(url), "title": "",
                     "platform": "tiktok"}]
        return self._expand_ytdlp(url)

    @staticmethod
    def _normalize_tiktok_url(url):
        low = url.lower()
        if "//vm." in low or "//vt." in low or "/t/" in low:
            try:
                r = requests.get(url, headers={"User-Agent": USER_AGENT},
                                 timeout=10, allow_redirects=True)
                if r.url:
                    return r.url.split("?")[0]
            except Exception:
                pass
        return url.split("?")[0]

    def _expand_spotify(self, url):
        low = url.lower()
        if "/track/" in low:
            meta = self._spotify_track_meta(url)
            if meta is None:
                raise EngineError(
                    "No se pudieron obtener los metadatos de la pista. "
                    "Intenta de nuevo o configura credenciales propias en "
                    "⚙️ Configuración → Spotify")
            tid = self._spotify_track_id(url)
            label = f"{meta['artist']} - {meta['title']}".strip(" -")
            return [{
                "url": (f"https://open.spotify.com/track/{tid}" if tid
                        else self._normalize_spotify_url(url)),
                "title": label or meta["title"],
                "platform": "spotify",
                "metadata": meta,
            }]
        api_error = None
        if self.config.get("spotify_client_id"):
            try:
                return self._api_expand(url)
            except Exception as e:
                api_error = e
        embedded = self._spotify_collection_embed(url)
        if embedded:
            return embedded
        try:
            return self._api_expand(url)
        except EngineError:
            raise
        except Exception as e:
            api_error = e
        raise EngineError(self._spotify_expand_error(api_error))

    def _spotify_expand_error(self, exc):
        raw = str(exc or "")
        lowr = raw.lower()
        if "429" in raw or "too many 429" in lowr or "max retries" in lowr:
            return ("La API de Spotify devolvió 429: las credenciales "
                    "públicas compartidas están agotadas por hoy.\n\n"
                    "Solución gratuita (2 minutos):\n"
                    "1) Entra a developer.spotify.com/dashboard y crea una "
                    "app\n2) Copia el Client ID y el Client Secret\n"
                    "3) Pégalos en ⚙️ Configuración → Spotify → Validar\n\n"
                    "Con credenciales propias, listas y álbumes funcionan "
                    "sin límites.")
        return f"{self.NO_API_MSG} — detalle: {raw[:200]}"

    def _api_expand(self, url):
        sp = self._spotipy_client()
        low = url.lower()
        items = []

        def paged(fetch, unwrap):
            out = []
            offset = 0
            retries = 0
            while True:
                try:
                    page = fetch(offset)
                    retries = 0
                except Exception as e:
                    status = getattr(e, "http_status", None) or \
                        getattr(e, "code", None)
                    if status == 429 and retries < 3:
                        wait = min(5 * (retries + 1), 15)
                        time.sleep(wait)
                        retries += 1
                        continue
                    raise
                batch = unwrap(page)
                out.extend(batch)
                offset += len(batch)
                if not page.get("next") or not batch or \
                        len(out) >= MAX_EXPAND:
                    break
            return out

        if "/playlist/" in low:
            pid = url.split("/playlist/")[1].split("?")[0]
            all_items = paged(
                lambda off: sp.playlist_items(
                    pid, additional_types=["track"], limit=100, offset=off),
                lambda page: [it.get("track") or {}
                              for it in page.get("items", [])])
            for t in all_items:
                if t.get("id"):
                    items.append(self._spotify_track_dict(t))
        elif "/album/" in low:
            aid = url.split("/album/")[1].split("?")[0]
            album = sp.album(aid)
            alb_info = {"name": album.get("name"),
                        "images": album.get("images", []),
                        "release_date": album.get("release_date")}
            all_tracks = paged(
                lambda off: sp.album_tracks(aid, limit=50, offset=off),
                lambda page: page.get("items", []))
            for t in all_tracks:
                t["album"] = alb_info
                if t.get("id"):
                    items.append(self._spotify_track_dict(t))
        else:
            raise EngineError("URL de Spotify no reconocida")
        if not items:
            raise EngineError("Spotify no devolvió pistas")
        return items[:MAX_EXPAND]

    def _spotify_collection_embed(self, url):
        m = re.search(r"/(playlist|album)/([A-Za-z0-9]+)", url)
        if not m:
            return None
        kind, sid = m.groups()
        try:
            resp = requests.get(
                f"https://open.spotify.com/embed/{kind}/{sid}",
                headers={"User-Agent": USER_AGENT}, timeout=15)
            resp.raise_for_status()
            m2 = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">'
                r'(.*?)</script>', resp.text, re.S)
            if not m2:
                return None
            data = json.loads(m2.group(1))
            entity = (data.get("props", {}).get("pageProps", {})
                          .get("state", {}).get("data", {}).get("entity", {}))
            track_list = entity.get("trackList") or []
            items = []
            for t in track_list:
                uri = str(t.get("uri") or "")
                tid = uri.split(":")[-1] if ":" in uri else ""
                if not tid:
                    continue
                artists = str(t.get("subtitle") or "").strip()
                title = str(t.get("title") or "").strip()
                label = f"{artists} - {title}".strip(" -") or title
                items.append({
                    "url": f"https://open.spotify.com/track/{tid}",
                    "title": label,
                    "platform": "spotify",
                })
            if not items:
                return None
            return items[:MAX_EXPAND]
        except Exception:
            return None

    @staticmethod
    def _spotify_track_dict(track):
        artists = ", ".join(a["name"] for a in track.get("artists", []))
        title = f"{artists} - {track.get('name', '')}".strip(" -")
        cover = ""
        album = ""
        date = ""
        alb = track.get("album") or {}
        images = alb.get("images") or []
        if images:
            cover = images[0].get("url", "")
        album = alb.get("name", "")
        date = str(alb.get("release_date", "") or "")
        return {
            "url": f"https://open.spotify.com/track/{track['id']}",
            "title": title,
            "platform": "spotify",
            "metadata": {
                "title": track.get("name", ""),
                "artist": artists,
                "album": album,
                "date": date,
                "track": track.get("track_number", ""),
                "cover_url": cover,
            },
        }

    def _expand_ytdlp(self, url):
        import yt_dlp
        base = {
            "quiet": True, "no_warnings": True, "noplaylist": False,
            "extract_flat": "in_playlist", "skip_download": True,
            "socket_timeout": 20,
        }
        if self.config.get("proxy"):
            base["proxy"] = self.config.get("proxy")
        if COOKIES_FILE.exists():
            base["cookiefile"] = str(COOKIES_FILE)
        attempts = [
            ("clientes por defecto", None),
            ("cliente android", {"youtube": {"player_client": ["android"]}}),
            ("cliente tv", {"youtube": {"player_client": ["tv"]}}),
            ("cliente mweb", {"youtube": {"player_client": ["mweb"]}}),
        ]
        info = None
        last_error = None
        for round_ in range(3):
            for label, ex_args in attempts:
                opts = dict(base)
                if ex_args:
                    opts["extractor_args"] = ex_args
                try:
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                    break
                except Exception as e:
                    last_error = e
            if info is not None:
                break
            text = str(last_error or "").lower()
            if round_ < 2 and any(t in text for t in (
                    "unable to extract", "universal data", "timed out")):
                time.sleep(2 * (round_ + 1))
                continue
            break
        if info is None:
            raise EngineError(friendly_error(last_error))
        src_platform = detect_platform(url)
        entries = info.get("entries") if info else None
        if not entries:
            entries = [info] if info else []
        items = []
        for entry in entries:
            if entry is None:
                continue
            eurl = entry.get("url") or entry.get("webpage_url")
            if src_platform == "youtube":
                plat = "youtube"
                if entry.get("id"):
                    eurl = f"https://www.youtube.com/watch?v={entry['id']}"
                elif isinstance(eurl, str) and not eurl.startswith(
                        ("http://", "https://")):
                    eurl = f"https://www.youtube.com/watch?v={eurl}"
            elif entry.get("id") and not eurl:
                eurl = f"https://www.youtube.com/watch?v={entry['id']}"
                plat = "youtube"
            else:
                plat = detect_platform(eurl) if eurl else "generic"
            if not eurl:
                continue
            items.append({
                "url": eurl,
                "title": entry.get("title") or "",
                "platform": plat,
            })
            if len(items) >= MAX_EXPAND:
                break
        return items

    def resolve(self, item, progress_cb=None, cancel_check=None, log_signal=None,
                tmp_root=None):
        item_id = item["id"]
        platform = item.get("platform") or detect_platform(item["url"])
        tmp_dir = Path(tmp_root or (Path(DATA_DIR) / "tmp")) / str(item_id)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        metadata = {}
        if platform == "spotify":
            steps = [
                ("meta→yt-dlp", lambda: self._via_spotify_search(
                    item, tmp_dir, metadata, progress_cb, cancel_check)),
            ]
        else:
            steps = [
                ("yt-dlp", lambda: self._via_ytdlp(item, tmp_dir, metadata,
                                                   progress_cb, cancel_check,
                                                   secondary=False)),
            ]
            if platform == "youtube":
                steps.append(("yt-dlp#2", lambda: self._via_ytdlp(
                    item, tmp_dir, metadata, progress_cb, cancel_check,
                    secondary=True)))
            if platform == "tiktok":
                steps.insert(1, ("tikwm", lambda: self._via_tikwm(
                    item, tmp_dir, metadata, progress_cb, cancel_check)))
            if self._youtube_id(item["url"]):
                steps.insert(1, ("piped", lambda: self._via_piped(
                    item, tmp_dir, metadata, progress_cb, cancel_check)))
            steps.append(("scraper", lambda: self._via_scraper(
                item, tmp_dir, metadata, progress_cb, cancel_check)))
        last_error = None
        for name, fn in steps:
            if cancel_check and cancel_check():
                raise Cancelled()
            try:
                paths = self._run_with_timeout(fn, name)
                if not paths:
                    raise EngineError("el motor no produjo archivos")
                self.limiter.report_success()
                self._log(log_signal,
                          f"✔ Motor '{name}' completó: {item.get('title') or item['url']}",
                          "success")
                return paths, name, metadata
            except Cancelled:
                raise
            except Exception as e:
                code = classify_error(e)
                if code == "rate":
                    self.limiter.report_429()
                last_error = e
                detail = " ".join(str(e).split())[:140]
                self._log(log_signal,
                          f"⚠ Fallback: '{name}' falló ({code}) en "
                          f"{item.get('title') or item['url']} — {detail}",
                          "fallback")
        raise EngineError(
            f"Todos los motores fallaron. {friendly_error(last_error)}")

    def _base_ydl_opts(self, tmp_dir, hook, secondary):
        opts = {
            "outtmpl": str(tmp_dir / "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True,
            "noplaylist": True, "retries": 2, "fragment_retries": 3,
            "socket_timeout": 20, "progress_hooks": [hook],
            "hls_use_native": True,
            "ffmpeg_location": ffmpeg_location(),
        }
        proxy = self.config.get("proxy")
        if proxy:
            opts["proxy"] = proxy
        if COOKIES_FILE.exists():
            opts["cookiefile"] = str(COOKIES_FILE)
        if secondary:
            opts["extractor_args"] = {
                "youtube": {"player_client": ["android", "web_safari"]}
            }
        quality = self.config.get("quality", "MP3 320kbps")
        if quality.startswith("Video"):
            mh = re.search(r"(\d+)\s*p\b", quality, re.IGNORECASE)
            h = mh.group(1) if mh else "1080"
            opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
            opts["merge_output_format"] = "mp4"
        else:
            opts["format"] = "bestaudio/best"
        return opts

    def _make_hook(self, progress_cb, cancel_check):
        def hook(d):
            if cancel_check and cancel_check():
                raise Cancelled()
            if d.get("status") == "downloading" and progress_cb:
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes", 0)
                pct = (done / total * 100) if total else 0
                progress_cb(pct, done)
        return hook

    def _ydl_download(self, url, opts, tmp_dir):
        import yt_dlp
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info is None:
            raise EngineError("sin información del video")
        rd = info.get("requested_downloads") or []
        if rd and rd[0].get("filepath"):
            return [Path(rd[0]["filepath"])], info
        files = sorted(tmp_dir.glob("*"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
        media = [f for f in files if f.suffix.lower() in MEDIA_EXTS]
        if not media:
            raise EngineError("archivo descargado no encontrado")
        return [media[0]], info

    def _info_metadata(self, info):
        return {
            "title": info.get("title", ""),
            "artist": info.get("uploader") or info.get("channel") or "",
            "album": info.get("playlist_title") or "",
            "date": info.get("release_date") or info.get("upload_date") or "",
            "cover_url": info.get("thumbnail", ""),
        }

    def _via_ytdlp(self, item, tmp_dir, metadata, progress_cb, cancel_check,
                   secondary):
        cmd = yt_dlp_command()
        if getattr(sys, "frozen", False) and cmd and len(cmd) == 1:
            return self._via_ytdlp_process(item, tmp_dir, metadata,
                                           progress_cb, cancel_check, cmd,
                                           secondary)
        transient = ("unable to extract", "universal data",
                     "temporarily unavailable", "timed out",
                     "unable to download webpage")
        last_exc = None
        for attempt in range(3):
            if cancel_check and cancel_check():
                raise Cancelled()
            try:
                hook = self._make_hook(progress_cb, cancel_check)
                opts = self._base_ydl_opts(tmp_dir, hook, secondary)
                paths, info = self._ydl_download(item["url"], opts, tmp_dir)
                if not metadata:
                    metadata.update(self._info_metadata(info))
                qcfg = str(self.config.get("quality", ""))
                if qcfg.startswith("Video"):
                    mh = re.search(r"(\d+)\s*p\b", qcfg, re.IGNORECASE)
                    want = int(mh.group(1)) if mh else 0
                    got = info.get("height") or (
                        (info.get("requested_downloads") or
                         [{}])[0].get("height"))
                    if want and got and int(got) + 60 < want:
                        metadata["_warn_resolucion"] = (
                            f"Solo se obtuvo {int(got)}p de {want}p "
                            f"solicitados — la plataforma limita formatos "
                            f"altos sin cookies (⚙️ Configuración → Cookies)")
                return paths
            except Cancelled:
                raise
            except Exception as e:
                last_exc = e
                text = str(e).lower()
                if attempt < 2 and any(t in text for t in transient):
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise last_exc

    def _ydl_process_args(self, tmp_dir, secondary):
        quality = self.config.get("quality", "MP3 320kbps")
        if quality.startswith("Video"):
            h = re.search(r"(\d+)\s*p\b", quality, re.IGNORECASE)
            height = h.group(1) if h else "1080"
            fmt = f"bv*[height<={height}]+ba/b[height<={height}]"
            merge_args = ["--merge-output-format", "mp4"]
        else:
            fmt = "bestaudio/best"
            merge_args = []
        args = [
            "--quiet", "--no-warnings", "--no-playlist",
            "--retries", "2", "--fragment-retries", "3",
            "--socket-timeout", "20",
            "-f", fmt,
            *merge_args,
            "-o", str(Path(tmp_dir) / "%(id)s.%(ext)s"),
            "--newline",
            "--progress-template",
            "download:PROG|%(progress.downloaded_bytes)s|%(progress.total_bytes)s",
            "--print", "after_move:filepath",
            "--print", "after_move:META|%(title)s|%(uploader,channel|)s"
                       "|%(thumbnail|)s",
            "--ffmpeg-location", ffmpeg_location(),
        ]
        proxy = self.config.get("proxy")
        if proxy:
            args += ["--proxy", proxy]
        if COOKIES_FILE.exists():
            args += ["--cookies", str(COOKIES_FILE)]
        if secondary:
            args += ["--extractor-args",
                     "youtube:player_client=android,web_safari"]
        return args

    def _via_ytdlp_process(self, item, tmp_dir, metadata, progress_cb,
                           cancel_check, cmd, secondary):
        args = self._ydl_process_args(tmp_dir, secondary)
        full_cmd = list(cmd) + args + [item["url"]]
        env = dict(os.environ)
        # Forzar UTF-8 en el hijo: los títulos con emojis/acentos deben
        # sobrevivir el viaje por el pipe; si no, la ruta impresa no coincide
        # con el archivo real y exists() falla.
        env.setdefault("PYTHONUTF8", "1")
        env["PYTHONIOENCODING"] = "utf-8:replace"
        proc = subprocess.Popen(
            full_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=(0x08000000 if sys.platform == "win32" else 0),
        )
        filepath = None
        try:
            for line in proc.stdout:
                if cancel_check and cancel_check():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise Cancelled()
                line = line.strip()
                if line.startswith("PROG|"):
                    try:
                        _tag, done_s, total_s = line.split("|", 2)
                        done = float(done_s or 0)
                        total = float(total_s or 0)
                        if progress_cb and total > 0:
                            progress_cb(min(done / total * 100, 99.5), done)
                    except ValueError:
                        pass
                elif line.startswith("META|"):
                    parts = line.split("|", 3)
                    if len(parts) == 4:
                        metadata.update({
                            "title": parts[1], "artist": parts[2],
                            "cover_url": parts[3],
                        })
                elif line and not line.startswith("["):
                    filepath = line
        finally:
            if cancel_check and cancel_check() and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise EngineError("yt-dlp no terminó a tiempo")
        if proc.returncode != 0 or not filepath:
            raise EngineError(
                f"yt-dlp (binario actualizado) terminó con código "
                f"{proc.returncode}: {item['url']}")
        final_path = Path(filepath)
        if not final_path.exists():
            # La ruta impresa pudo dañarse por el pipe (emojis/acentos) o
            # diferir del sanitizador: buscar el medio más reciente del tmp.
            media = sorted(
                (f for f in Path(tmp_dir).glob("*")
                 if f.is_file() and f.suffix.lower() in MEDIA_EXTS),
                key=lambda p: p.stat().st_mtime, reverse=True)
            if media:
                return [media[0]]
            raise EngineError(f"archivo no encontrado tras descargar: {filepath}")
        return [final_path]

    def _via_spotify_search(self, item, tmp_dir, metadata, progress_cb,
                            cancel_check):
        meta = self._spotify_track_meta(item["url"])
        if meta is None:
            base_title = (item.get("title") or "").strip()
            if " - " in base_title:
                artist, _, name = base_title.partition(" - ")
                meta = {"title": name.strip(), "artist": artist.strip(),
                        "album": "", "date": "", "track": "",
                        "cover_url": ""}
            elif base_title:
                meta = {"title": base_title, "artist": "", "album": "",
                        "date": "", "track": "", "cover_url": ""}
            else:
                raise EngineError(
                    "sin metadatos de Spotify para buscar en YouTube")
        query = f"{meta['artist']} - {meta['title']}".strip(" -")
        if not query:
            raise EngineError("sin metadatos de Spotify para buscar en YouTube")
        hook = self._make_hook(progress_cb, cancel_check)
        opts = self._base_ydl_opts(tmp_dir, hook, secondary=True)
        paths, _info = self._ydl_download(f"ytsearch1:{query}", opts, tmp_dir)
        metadata.update(meta)
        return paths

    def _via_tikwm(self, item, tmp_dir, metadata, progress_cb, cancel_check):
        api = "https://www.tikwm.com/api/"
        r = requests.get(
            api, params={"url": item["url"], "hd": "1"},
            headers={"User-Agent": USER_AGENT,
                     "Referer": "https://www.tikwm.com/",
                     "Accept": "application/json"},
            timeout=20)
        r.raise_for_status()
        payload = r.json()
        if payload.get("code") not in (0, "0") or not payload.get("data"):
            raise EngineError(
                f"tikwm: {payload.get('msg') or 'sin datos'}")
        d = payload["data"]
        want_video = str(item.get("quality") or
                         self.config.get("quality", "")).startswith("Video")
        candidates = []
        if want_video:
            for key in ("hdplay", "play", "wmplay"):
                if d.get(key):
                    candidates.append((d[key], ".mp4"))
        else:
            if d.get("music"):
                candidates.append((d["music"], ".mp3"))
            for key in ("play", "hdplay"):
                if d.get(key):
                    candidates.append((d[key], ".mp4"))
        if not candidates:
            raise EngineError("tikwm sin enlaces de medios")
        headers = {"User-Agent": USER_AGENT,
                   "Referer": "https://www.tikwm.com/"}
        last = None
        for link, ext in candidates:
            if cancel_check and cancel_check():
                raise Cancelled()
            out = Path(tmp_dir) / f"tikwm{ext}"
            try:
                with requests.get(link, headers=headers, timeout=30,
                                  stream=True) as dl:
                    dl.raise_for_status()
                    total = int(dl.headers.get("Content-Length", 0) or 0)
                    done = 0
                    with open(out, "wb") as fh:
                        for chunk in dl.iter_content(262144):
                            if cancel_check and cancel_check():
                                raise Cancelled()
                            fh.write(chunk)
                            done += len(chunk)
                            if progress_cb and total:
                                progress_cb(done / total * 100, done)
            except Cancelled:
                raise
            except Exception as e:
                last = e
                continue
            author = ((d.get("author") or {}).get("nickname")) or ""
            metadata.update({
                "title": d.get("title", "") or "",
                "artist": author,
                "album": "", "date": "",
                "cover_url": (d.get("origin_cover") or d.get("cover") or ""),
            })
            return [out]
        raise EngineError(f"tikwm descarga fallida: {last}")

    @staticmethod
    def _youtube_id(url):
        m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/)([\w-]{6,})", url)
        return m.group(1) if m else None

    def _via_piped(self, item, tmp_dir, metadata, progress_cb, cancel_check):
        vid = self._youtube_id(item["url"])
        if not vid:
            raise EngineError("piped: no es un enlace de video de YouTube")
        last = None
        for base in PIPED_INSTANCES:
            if cancel_check and cancel_check():
                raise Cancelled()
            try:
                r = requests.get(f"{base}/streams/{vid}", headers={
                    "User-Agent": USER_AGENT}, timeout=8)
                if r.status_code != 200:
                    last = f"{base} → HTTP {r.status_code}"
                    continue
                data = r.json()
                streams = [s for s in (data.get("audioStreams") or [])
                           if s.get("url")]
                if not streams:
                    last = f"{base} → sin streams de audio"
                    continue
                best = max(streams, key=lambda s: s.get("bitrate") or 0)
                mime = (best.get("mimeType") or "").lower()
                ext = ".weba" if "webm" in mime else (
                    ".m4a" if ("mp4" in mime or "aac" in mime or
                               "m4a" in mime) else ".mp3")
                out = tmp_dir / f"piped{ext}"
                total = int(best.get("contentLength") or 0)
                done = 0
                with requests.get(best["url"], stream=True, timeout=30,
                                  headers={"User-Agent": USER_AGENT}) as dl:
                    dl.raise_for_status()
                    with open(out, "wb") as fh:
                        for chunk in dl.iter_content(262144):
                            if cancel_check and cancel_check():
                                raise Cancelled()
                            fh.write(chunk)
                            done += len(chunk)
                            if progress_cb and total:
                                progress_cb(done / total * 100, done)
                metadata.update({
                    "title": data.get("title", "") or "",
                    "artist": data.get("uploader", "") or "",
                    "album": "", "date": "",
                    "cover_url": ((data.get("thumbnailUrl") or "")
                                  if isinstance(data.get("thumbnailUrl"),
                                                str) else ""),
                })
                return [out]
            except Cancelled:
                raise
            except Exception as e:
                last = f"{base} → {e}"
        raise EngineError(f"ninguna instancia Piped respondió ({last})")

    def _via_scraper(self, item, tmp_dir, metadata, progress_cb, cancel_check):
        url = item["url"]
        headers = {"User-Agent": USER_AGENT}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        candidates = []
        for prop in ("og:video:secure_url", "og:video:url", "og:video"):
            tag = soup.find("meta", property=prop)
            if tag and tag.get("content"):
                candidates.append(tag["content"])
        for source in soup.find_all(["source", "video", "audio"]):
            srcv = source.get("src")
            if srcv:
                candidates.append(srcv)
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().split("?")[0].endswith(MEDIA_EXTS):
                candidates.append(href)

        absolutos = []
        for cand in candidates:
            if cand.startswith("//"):
                cand = "https:" + cand
            cand = cand.strip()
            if cand.startswith(("http://", "https://")):
                absolutos.append(cand)
        # En páginas de YouTube, og:video apunta a una página embed/watch,
        # no a un archivo de medio: descartarlas explícitamente.
        absolutos = [c for c in absolutos if not re.search(
            r"youtube\.com/(v/|embed/|watch)|youtu\.be/", c, re.I)]
        if not absolutos:
            raise EngineError("scraper: no se encontró enlace directo de medios")

        last_err = None
        for final_url in absolutos:
            if cancel_check and cancel_check():
                raise Cancelled()
            m = re.search(r"(mp4|m4a|mp3|webm|ogg|opus|aac|mkv)",
                          final_url.lower())
            if m:
                ext = "." + m.group(1)
            else:
                guess = Path(final_url.split("?")[0]).suffix.lower()
                ext = guess if guess in MEDIA_EXTS else ".mp4"
            out = tmp_dir / f"rescate{ext}"
            try:
                dl = requests.get(final_url, headers=headers, timeout=30,
                                  stream=True)
                dl.raise_for_status()
                ctype = (dl.headers.get("Content-Type") or "").lower()
                if "text/html" in ctype or "application/xhtml" in ctype:
                    last_err = f"el enlace sirve una página HTML ({ctype})"
                    continue
                total = int(dl.headers.get("Content-Length", 0) or 0)
                done = 0
                with open(out, "wb") as fh:
                    for chunk in dl.iter_content(262144):
                        if cancel_check and cancel_check():
                            raise Cancelled()
                        fh.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(done / total * 100, done)
                if not file_is_media(out):
                    last_err = "lo descargado no es un archivo de medio válido"
                    out.unlink(missing_ok=True)
                    continue
            except Cancelled:
                raise
            except Exception as e:
                last_err = str(e)
                out.unlink(missing_ok=True)
                continue
            og_title = soup.find("meta", property="og:title")
            metadata.update({
                "title": (og_title.get("content") if og_title else "") or out.stem,
                "artist": "", "album": "", "date": "", "cover_url": "",
            })
            return [out]
        raise EngineError(
            f"scraper: {last_err or 'ningún enlace entregó un medio válido'}")
