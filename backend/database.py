import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT DEFAULT '',
    platform TEXT DEFAULT 'generic',
    quality TEXT DEFAULT 'MP3 320kbps',
    file_path TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    progress REAL DEFAULT 0,
    current_retry INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 3,
    worker_id TEXT DEFAULT '',
    next_attempt_at REAL DEFAULT 0,
    error_log TEXT DEFAULT '',
    metadata_json TEXT DEFAULT '{}',
    engine_used TEXT DEFAULT '',
    created_at REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
"""

BACKOFFS = [5, 15, 45]


class DatabaseManager:
    def __init__(self, db_path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def recover_orphans(self):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET status='pending', worker_id='' WHERE status='downloading'"
            )
            self._conn.commit()
            return cur.rowcount

    def add_items(self, items):
        with self._lock:
            added = updated = 0
            for it in items:
                url = it["url"]
                q = it.get("quality", "MP3 320kbps")
                row = self._conn.execute(
                    "SELECT id, quality, status FROM queue WHERE url=?",
                    (url,)).fetchone()
                if row is None:
                    cur = self._conn.execute(
                        """INSERT INTO queue
                           (url, title, platform, quality, status,
                            created_at)
                           VALUES (?,?,?,?, 'pending', ?)""",
                        (
                            url,
                            it.get("title", ""),
                            it.get("platform", "generic"),
                            q,
                            time.time(),
                        ))
                    added += cur.rowcount
                elif row["quality"] != q:
                    if row["status"] in ("failed", "cancelled"):
                        self._conn.execute(
                            "UPDATE queue SET quality=?, status='pending', "
                            "current_retry=0, next_attempt_at=0, "
                            "error_log='', worker_id='' WHERE id=?",
                            (q, row["id"]))
                    else:
                        self._conn.execute(
                            "UPDATE queue SET quality=? WHERE id=?",
                            (q, row["id"]))
                    updated += 1
            self._conn.commit()
            return added, updated

    def update_quality(self, item_id, quality):
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM queue WHERE id=?", (item_id,)
            ).fetchone()
            if not row or row["status"] == "downloading":
                return False
            if row["status"] == "completed":
                self._conn.execute(
                    "UPDATE queue SET quality=?, status='pending', "
                    "file_path='', current_retry=0, next_attempt_at=0, "
                    "error_log='', worker_id='' WHERE id=?",
                    (quality, item_id))
            elif row["status"] in ("failed", "cancelled"):
                self._conn.execute(
                    "UPDATE queue SET quality=?, status='pending', "
                    "current_retry=0, next_attempt_at=0, error_log='', "
                    "worker_id='' WHERE id=?",
                    (quality, item_id))
            else:
                self._conn.execute(
                    "UPDATE queue SET quality=? WHERE id=?",
                    (quality, item_id))
            self._conn.commit()
            return True

    def bulk_change_quality(self, quality, include_completed=False):
        with self._lock:
            statuses = "('pending','retrying','failed','cancelled')"
            if include_completed:
                statuses += ",'completed'"
            cur = self._conn.execute(
                f"""UPDATE queue SET quality=?, status='pending',
                   file_path='', current_retry=0, next_attempt_at=0,
                   error_log='', worker_id=''
                   WHERE status IN {statuses}""",
                (quality,))
            self._conn.commit()
            return cur.rowcount

    def _claim_returning(self, worker_id, limit):
        cur = self._conn.execute(
            """UPDATE queue SET status='downloading', worker_id=?
               WHERE id IN (
                   SELECT id FROM queue WHERE status='pending'
                   ORDER BY created_at LIMIT ?
               )
               RETURNING *""",
            (worker_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
        self._conn.commit()
        return rows

    def _claim_fallback(self, worker_id, limit):
        cur = self._conn.execute(
            "SELECT id FROM queue WHERE status='pending' ORDER BY created_at LIMIT ?",
            (limit,),
        )
        ids = [r["id"] for r in cur.fetchall()]
        rows = []
        for i in ids:
            cur = self._conn.execute(
                "UPDATE queue SET status='downloading', worker_id=? "
                "WHERE id=? AND status='pending'",
                (worker_id, i),
            )
            if cur.rowcount == 1:
                row = self._conn.execute("SELECT * FROM queue WHERE id=?", (i,))
                rows.append(dict(row.fetchone()))
        self._conn.commit()
        return rows

    def claim_pending_items(self, worker_id, limit):
        with self._lock:
            try:
                return self._claim_returning(worker_id, limit)
            except sqlite3.OperationalError:
                return self._claim_fallback(worker_id, limit)

    def update_status(self, item_id, status, error_log=None, engine_used=None,
                      file_path=None, progress=None):
        with self._lock:
            sets = ["status=?"]
            vals = [status]
            if error_log is not None:
                sets.append("error_log=?")
                vals.append(error_log[:4000])
            if engine_used is not None:
                sets.append("engine_used=?")
                vals.append(engine_used)
            if file_path is not None:
                sets.append("file_path=?")
                vals.append(str(file_path))
            if progress is not None:
                sets.append("progress=?")
                vals.append(float(progress))
            if status != "downloading":
                sets.append("worker_id=''")
            vals.append(item_id)
            self._conn.execute(f"UPDATE queue SET {', '.join(sets)} WHERE id=?", vals)
            self._conn.commit()

    def update_progress(self, item_id, progress):
        with self._lock:
            self._conn.execute(
                "UPDATE queue SET progress=? WHERE id=?", (float(progress), item_id)
            )
            self._conn.commit()

    def register_failure(self, item_id, error_log):
        with self._lock:
            row = self._conn.execute(
                "SELECT current_retry, max_retries FROM queue WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return "failed"
            retry = row["current_retry"] + 1
            if retry < row["max_retries"]:
                delay = BACKOFFS[min(retry - 1, len(BACKOFFS) - 1)]
                self._conn.execute(
                    "UPDATE queue SET current_retry=?, status='retrying', "
                    "worker_id='', next_attempt_at=?, error_log=? WHERE id=?",
                    (retry, time.time() + delay, error_log[:4000], item_id),
                )
                self._conn.commit()
                return "retrying", delay
            self._conn.execute(
                "UPDATE queue SET current_retry=?, status='failed', worker_id='', "
                "error_log=? WHERE id=?",
                (retry, error_log[:4000], item_id),
            )
            self._conn.commit()
            return "failed"

    def promote_due_retries(self, now=None):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET status='pending' "
                "WHERE status='retrying' AND next_attempt_at<=?",
                (now if now is not None else time.time(),),
            )
            self._conn.commit()
            return cur.rowcount

    def cancel_item(self, item_id):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE queue SET status='cancelled', worker_id='' "
                "WHERE id=? AND status IN ('pending','downloading','retrying')",
                (item_id,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def is_cancelled(self, item_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM queue WHERE id=?", (item_id,)
            ).fetchone()
            if row is None:
                return True
            return row["status"] == "cancelled"

    def clear_completed(self):
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM queue WHERE status IN "
                "('completed','failed','cancelled')")
            self._conn.commit()
            return cur.rowcount

    def clear_all(self):
        with self._lock:
            cur = self._conn.execute("DELETE FROM queue")
            self._conn.commit()
            return cur.rowcount

    def get_item(self, item_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM queue WHERE id=?", (item_id,)
            ).fetchone()
            return dict(row) if row else None

    def rows_page(self, offset, limit):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue ORDER BY created_at, id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_total(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM queue").fetchone()
            return row["c"]

    def get_stats(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS c FROM queue GROUP BY status"
            ).fetchall()
            stats = {r["status"]: r["c"] for r in rows}
            stats["total"] = sum(stats.values())
            return stats

    def get_failed_items_for_csv(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, url, title, platform, engine_used, error_log "
                "FROM queue WHERE status='failed' ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def close(self):
        with self._lock:
            self._conn.close()
