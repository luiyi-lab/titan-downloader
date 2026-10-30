import threading
import time

from PySide6.QtCore import QObject, Signal

MAX_DELAY_MS = 60000


class RateLimiter(QObject):
    warning = Signal(str)

    def __init__(self, delay_ms=1500):
        super().__init__()
        self._delay_ms = max(0, int(delay_ms))
        self._consecutive_429 = 0
        self._lock = threading.Lock()

    def set_delay(self, delay_ms):
        with self._lock:
            self._delay_ms = min(MAX_DELAY_MS, max(0, int(delay_ms)))
            self._consecutive_429 = 0

    @property
    def delay_ms(self):
        with self._lock:
            return self._delay_ms

    def wait(self):
        with self._lock:
            delay = self._delay_ms / 1000.0
        if delay > 0:
            time.sleep(delay)

    def report_429(self):
        with self._lock:
            self._consecutive_429 += 1
            count = self._consecutive_429
        if count >= 2:
            with self._lock:
                old = self._delay_ms
                self._delay_ms = min(MAX_DELAY_MS, self._delay_ms * 2)
                new = self._delay_ms
            self.warning.emit(
                f"⚠ Rate limit detectado (429 x{count}). Delay aumentado: "
                f"{old}ms → {new}ms"
            )

    def report_success(self):
        with self._lock:
            self._consecutive_429 = 0
