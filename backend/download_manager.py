import shutil
import threading
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal

from . import DATA_DIR, Config
from .engine_resolver import Cancelled, EngineResolver
from .processor import MediaProcessor
from .rate_limiter import RateLimiter


class DownloadWorker(QRunnable):
    def __init__(self, manager, item, worker_id):
        super().__init__()
        self.manager = manager
        self.item = item
        self.worker_id = worker_id
        self.setAutoDelete(True)

    def run(self):
        m = self.manager
        item = self.item
        item_id = item["id"]
        title = item.get("title") or item["url"]
        tmp_root = Path(DATA_DIR) / "tmp"
        try:
            if m.db.is_cancelled(item_id):
                m._on_cancelled(item_id)
                return
            m.limiter.wait()
            progress_cb = lambda pct, cum_bytes: m.report_progress(
                item_id, pct, cum_bytes)
            cancel_check = lambda: m.db.is_cancelled(item_id)
            paths, engine, metadata = m.resolver.resolve(
                item, progress_cb=progress_cb, cancel_check=cancel_check,
                log_signal=m.log, tmp_root=tmp_root,
            )
            final = None
            total = len(paths)
            for p in paths:
                if cancel_check():
                    raise Cancelled()
                meta = metadata if total == 1 else dict(metadata)
                final = m.processor.process(p, item["quality"],
                                            bool(m.config.get("normalize_audio")),
                                            meta)
            warn = metadata.pop("_warn_resolucion", None)
            if warn:
                m.log.emit(f"⚠ {warn}", "fallback")
            m.clear_progress(item_id)
            m.db.update_status(item_id, "completed", engine_used=engine,
                               file_path=str(final), progress=100)
            m.notify_completion()
            m.mark_dirty(item_id)
            m.item_finished.emit(title, True)
            m.log.emit(f"✅ Completado [{engine}]: {title}", "success")
        except Cancelled:
            m.clear_progress(item_id)
            m._on_cancelled(item_id)
        except Exception as e:
            m.clear_progress(item_id)
            result = m.db.register_failure(item_id, f"{type(e).__name__}: {e}")
            m.mark_dirty(item_id)
            if isinstance(result, tuple) and result[0] == "retrying":
                delay = int(result[1])
                m.log.emit(f"🔁 Reintento en {delay}s: {title} — {e}", "warn")
            else:
                m.item_finished.emit(title, False)
                m.log.emit(f"❌ Falló definitivo: {title} — {e}", "error")
        finally:
            shutil.rmtree(tmp_root / str(item_id), ignore_errors=True)
            m._worker_done()


class DownloadManager(QObject):
    log = Signal(str, str)
    finished_all = Signal()
    item_finished = Signal(str, bool)

    def __init__(self, db, config: Config, limiter: RateLimiter,
                 resolver: EngineResolver, processor: MediaProcessor):
        super().__init__()
        self.db = db
        self.config = config
        self.limiter = limiter
        self.resolver = resolver
        self.processor = processor
        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(int(config.get("max_concurrent", 3)))
        self._running = False
        self._paused = False
        self._active = 0
        self._worker_seq = 0
        self._lock = threading.Lock()
        self._dirty = set()
        self._progress = {}
        self._progress_dirty = set()
        self._bytes = {}
        self._bytes_total = 0
        self._speed_samples = deque(maxlen=15)
        self._completions = deque(maxlen=300)
        self._announced_done = False

        self._fill_timer = QTimer(self)
        self._fill_timer.setInterval(500)
        self._fill_timer.timeout.connect(self._fill)
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)

    def set_max_threads(self, n):
        self.pool.setMaxThreadCount(int(n))

    @property
    def is_running(self):
        return self._running and not self._paused

    def start(self):
        if not self._running:
            self._running = True
            self._paused = False
            self._announced_done = False
            self._fill_timer.start()
            self._tick_timer.start()
            self.log.emit("▶ Cola iniciada", "info")
        else:
            self.resume()

    def pause(self):
        self._paused = True
        self.log.emit("⏸ Pausa: los ítems en curso terminan, no se toman nuevos",
                      "warn")

    def resume(self):
        self._paused = False
        self.log.emit("▶ Reanudado", "info")

    def stop(self):
        self._running = False
        self._paused = True
        self._fill_timer.stop()
        self._tick_timer.stop()

    def _fill(self):
        if not self._running or self._paused:
            return
        free = self.pool.maxThreadCount() - self._active
        if free <= 0:
            return
        self.db.promote_due_retries()
        with self._lock:
            self._worker_seq += 1
            wid = f"w{self._worker_seq}"
        items = self.db.claim_pending_items(wid, free)
        for it in items:
            with self._lock:
                self._active += 1
            self.mark_dirty(it["id"])
            self.pool.start(DownloadWorker(self, it, wid))

    def _worker_done(self):
        with self._lock:
            self._active = max(0, self._active - 1)

    def report_progress(self, item_id, pct, cum_bytes=0):
        with self._lock:
            prev = self._bytes.get(item_id, 0)
            if cum_bytes > prev:
                self._bytes_total += cum_bytes - prev
                self._bytes[item_id] = cum_bytes
            best = max(pct, self._progress.get(item_id, 0))
            self._progress[item_id] = min(best, 99.9)
            self._progress_dirty.add(item_id)

    def notify_completion(self):
        self._completions.append(time.time())

    def mark_dirty(self, item_id):
        with self._lock:
            self._dirty.add(item_id)

    def clear_progress(self, item_id):
        with self._lock:
            self._progress.pop(item_id, None)
            self._bytes.pop(item_id, None)
            self._progress_dirty.discard(item_id)

    def pop_dirty(self):
        with self._lock:
            ids = list(self._dirty)
            self._dirty.clear()
            return ids

    def flush_progress(self):
        with self._lock:
            dirty = list(self._progress_dirty)
            snapshot = {i: self._progress[i] for i in dirty
                        if i in self._progress}
            self._progress_dirty.clear()
        for item_id, pct in snapshot.items():
            self.db.update_progress(item_id, round(pct, 1))

    def speed_bytes_per_sec(self):
        samples = list(self._speed_samples)
        if len(samples) < 2:
            return 0
        dt = samples[-1][0] - samples[0][0]
        if dt <= 0:
            return 0
        return max(0, int((samples[-1][1] - samples[0][1]) / dt))

    def get_global_stats(self):
        stats = self.db.get_stats()
        remaining = (stats.get("pending", 0) + stats.get("retrying", 0)
                     + self._active)
        eta_sec = None
        now = time.time()
        recent = [t for t in self._completions if now - t <= 300]
        if recent and remaining:
            rate = len(recent) / 300.0
            eta_sec = int(remaining / rate)
        stats["active"] = self._active
        stats["remaining"] = remaining
        stats["eta_sec"] = eta_sec
        return stats

    def _tick(self):
        with self._lock:
            self._speed_samples.append((time.time(), self._bytes_total))
        self.flush_progress()
        stats = self.db.get_stats()
        busy = (stats.get("pending", 0) + stats.get("retrying", 0)
                + self._active)
        if self._running and stats.get("total", 0) > 0 and busy == 0 and \
                not self._announced_done:
            self._announced_done = True
            self.stop()
            self.finished_all.emit()

    def _on_cancelled(self, item_id):
        self.db.update_status(item_id, "cancelled")
        self.mark_dirty(item_id)
        self.log.emit("🛑 Ítem cancelado", "warn")
