"""Persistent results: SQLite metadata and local TXT/MP3 files."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import uuid


class HistoryStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "index.sqlite3"
        self.lock = threading.RLock()
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS results (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, mode TEXT NOT NULL,
                created_at TEXT NOT NULL, elapsed REAL NOT NULL,
                size INTEGER NOT NULL, options TEXT NOT NULL)""")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, record):
        identity = uuid.UUID(record["id"]).hex
        return self.files / (identity + (".mp3" if record["mode"] == "convert" else ".txt"))

    def _record(self, row):
        record = dict(row)
        record["options"] = json.loads(record["options"])
        record["output_name"] = Path(record["filename"]).stem + (".mp3" if record["mode"] == "convert" else ".txt")
        record["available"] = self.path(record).is_file()
        return record

    def save(self, job):
        record = {"id": job.id, "filename": job.filename, "mode": job.mode,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "elapsed": round(job.elapsed, 1), "size": job.output.stat().st_size,
                  "options": json.dumps(job.options, ensure_ascii=False)}
        destination = self.path(record)
        temporary = destination.with_suffix(".partial")
        with self.lock, self._connection() as db:
            existing = db.execute("SELECT * FROM results WHERE id = ?", (job.id,)).fetchone()
            if existing and self.path(existing).is_file():
                return self.path(existing)
            try:
                # Publish the file before committing its metadata. Failed copies never appear in history.
                shutil.copyfile(job.output, temporary)
                temporary.replace(destination)
                db.execute("INSERT OR REPLACE INTO results VALUES (:id, :filename, :mode, :created_at, :elapsed, :size, :options)", record)
            except BaseException:
                destination.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
        return destination

    def list(self):
        with self.lock, self._connection() as db:
            return [self._record(row) for row in db.execute("SELECT * FROM results ORDER BY created_at DESC, id DESC")]

    def get(self, identity):
        with self.lock, self._connection() as db:
            row = db.execute("SELECT * FROM results WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            return self._record(row)

    def delete(self, identity):
        with self.lock, self._connection() as db:
            row = db.execute("SELECT * FROM results WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            db.execute("DELETE FROM results WHERE id = ?", (identity,))
            # Only the UUID-named result file is removed; original recordings are never stored here.
            self.path(row).unlink(missing_ok=True)
