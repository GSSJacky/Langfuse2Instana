import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, db_path: str = "./state.db", retention_days: int = 7):
        self.db_path = db_path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS exported_traces (
                        trace_id TEXT NOT NULL,
                        source_name TEXT NOT NULL,
                        exported_at TEXT NOT NULL,
                        PRIMARY KEY (trace_id, source_name)
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        source_name TEXT PRIMARY KEY,
                        last_timestamp TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_exported_traces_exported_at
                    ON exported_traces(exported_at)
                """)
                conn.commit()
            finally:
                conn.close()

    def is_trace_exported(self, trace_id: str, source_name: str) -> bool:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    "SELECT 1 FROM exported_traces WHERE trace_id = ? AND source_name = ?",
                    (trace_id, source_name),
                )
                return cursor.fetchone() is not None
            finally:
                conn.close()

    def mark_trace_exported(self, trace_id: str, source_name: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO exported_traces (trace_id, source_name, exported_at) VALUES (?, ?, ?)",
                    (trace_id, source_name, now),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_traces_exported(self, trace_ids: list[str], source_name: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.executemany(
                    "INSERT OR REPLACE INTO exported_traces (trace_id, source_name, exported_at) VALUES (?, ?, ?)",
                    [(tid, source_name, now) for tid in trace_ids],
                )
                conn.commit()
            finally:
                conn.close()

    def get_checkpoint(self, source_name: str) -> Optional[str]:
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    "SELECT last_timestamp FROM checkpoints WHERE source_name = ?",
                    (source_name,),
                )
                row = cursor.fetchone()
                return row[0] if row else None
            finally:
                conn.close()

    def set_checkpoint(self, source_name: str, timestamp: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO checkpoints (source_name, last_timestamp, updated_at) VALUES (?, ?, ?)",
                    (source_name, timestamp, now),
                )
                conn.commit()
            finally:
                conn.close()

    def cleanup_old_entries(self):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.retention_days)).isoformat()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    "DELETE FROM exported_traces WHERE exported_at < ?",
                    (cutoff,),
                )
                deleted = cursor.rowcount
                conn.commit()
                if deleted > 0:
                    logger.info("Cleaned up %d old trace entries", deleted)
            finally:
                conn.close()
