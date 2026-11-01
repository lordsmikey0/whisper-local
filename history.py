"""Persistent results: SQLite metadata and local TXT/MP3 files."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import sqlite3
import threading
import uuid
from media_formats import result_format
from speaker_text import validate_data, validate_names, render_transcript


class HistoryStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.files = self.root / "files"
        self.files.mkdir(parents=True, exist_ok=True)
        self.audio = self.root / 'audio'
        self.audio.mkdir(exist_ok=True)
        self.database = self.root / "index.sqlite3"
        self.lock = threading.RLock()
        with self._connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS results (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, mode TEXT NOT NULL,
                created_at TEXT NOT NULL, elapsed REAL NOT NULL,
                size INTEGER NOT NULL, options TEXT NOT NULL)""")
            db.execute('CREATE TABLE IF NOT EXISTS speaker_data (result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE, data TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS playback_data (result_id TEXT PRIMARY KEY REFERENCES results(id) ON DELETE CASCADE, segments TEXT NOT NULL)')

    def audio_path(self, identity):
        return self.audio / (uuid.UUID(identity).hex + '.mp3')

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, record):
        identity = uuid.UUID(record["id"]).hex
        options = record["options"]
        if isinstance(options, str):
            options = json.loads(options)
        fmt = result_format(record["mode"], options)
        if fmt not in ("txt", "mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "mp4", "mkv", "mov", "webm"):
            raise ValueError("Неизвестный формат результата в истории.")
        return self.files / (identity + "." + fmt)

    def _record(self, row):
        record = dict(row)
        record["options"] = json.loads(record["options"])
        record["output_format"] = result_format(record["mode"], record["options"])
        record["output_name"] = Path(record["filename"]).stem + "." + record["output_format"]
        record["available"] = self.path(record).is_file()
        audio = self.audio_path(record['id'])
        record['audio_available'] = record['mode'] == 'transcribe' and audio.is_file()
        record['audio_size'] = audio.stat().st_size if record['audio_available'] else 0
        return record

    def save(self, job):
        record = {"id": job.id, "filename": job.filename, "mode": job.mode,
                  "created_at": datetime.now(timezone.utc).isoformat(),
                  "elapsed": round(job.elapsed, 1), "size": job.output.stat().st_size,
                  "options": json.dumps(job.options, ensure_ascii=False)}
        destination = self.path(record)
        temporary = destination.with_suffix(".partial")
        audio_destination = self.audio_path(job.id)
        audio_temporary = audio_destination.with_suffix('.partial')
        with self.lock, self._connection() as db:
            existing = db.execute("SELECT * FROM results WHERE id = ?", (job.id,)).fetchone()
            if existing and self.path(existing).is_file():
                return self.path(existing)
            try:
                # Publish the file before committing its metadata. Failed copies never appear in history.
                shutil.copyfile(job.output, temporary)
                temporary.replace(destination)
                db.execute("INSERT OR REPLACE INTO results VALUES (:id, :filename, :mode, :created_at, :elapsed, :size, :options)", record)
                sidecar = job.output.with_suffix('.speakers.json')
                if job.mode == 'transcribe' and sidecar.is_file():
                    data = validate_data(json.loads(sidecar.read_text(encoding='utf-8')))
                    db.execute('INSERT OR REPLACE INTO speaker_data VALUES (?, ?)', (job.id, json.dumps(data, ensure_ascii=False)))
                if job.mode == 'transcribe' and job.options.get('keep_audio'):
                    segments = json.loads(job.output.with_suffix('.segments.json').read_text(encoding='utf-8'))
                    if not isinstance(segments, list) or any(
                        not isinstance(s.get('text'), str) or not all(
                            math.isfinite(float(s[k])) and float(s[k]) >= 0 for k in ('start', 'end'))
                        or s['end'] < s['start'] for s in segments
                    ):
                        raise ValueError('Неверные временные метки аудио.')
                    shutil.copyfile(job.output.with_suffix('.playback.mp3'), audio_temporary)
                    audio_temporary.replace(audio_destination)
                    db.execute('INSERT OR REPLACE INTO playback_data VALUES (?, ?)', (job.id, json.dumps(segments, ensure_ascii=False)))
            except BaseException:
                destination.unlink(missing_ok=True)
                audio_destination.unlink(missing_ok=True)
                raise
            finally:
                temporary.unlink(missing_ok=True)
                audio_temporary.unlink(missing_ok=True)
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

    def transcript(self, identity):
        with self.lock, self._connection() as db:
            record = self.get(identity)
            row = db.execute('SELECT data FROM speaker_data WHERE result_id = ?', (identity,)).fetchone()
            playback = db.execute('SELECT segments FROM playback_data WHERE result_id = ?', (identity,)).fetchone()
            extra = {'audio_available': record['audio_available'],
                     'segments': json.loads(playback['segments']) if playback else []}
            if row:
                data = validate_data(json.loads(row['data']))
                return {**extra, 'text': render_transcript(data), 'speakers': data['speakers'], 'names': data['names'],
                        'segments': data['segments']}
            return {**extra, 'text': self.path(record).read_text(encoding='utf-8-sig'), 'speakers': [], 'names': {}}

    def rename_speakers(self, identity, names):
        with self.lock, self._connection() as db:
            record = self.get(identity)
            row = db.execute('SELECT data FROM speaker_data WHERE result_id = ?', (identity,)).fetchone()
            if row is None:
                raise ValueError('В этой стенограмме нет разметки по говорящим.')
            data = validate_data(json.loads(row['data']))
            data['names'] = validate_names(data, names)
            text = render_transcript(data)
            destination = self.path(record)
            if not destination.is_file():
                raise FileNotFoundError('Файл стенограммы не найден.')
            temporary = destination.with_suffix('.names.partial')
            try:
                temporary.write_text(text, encoding='utf-8-sig')
                temporary.replace(destination)
                db.execute('UPDATE speaker_data SET data = ? WHERE result_id = ?', (json.dumps(data, ensure_ascii=False), identity))
                db.execute('UPDATE results SET size = ? WHERE id = ?', (destination.stat().st_size, identity))
            finally:
                temporary.unlink(missing_ok=True)
            return {'text': text, 'speakers': data['speakers'], 'names': data['names'],
                    'segments': data['segments'], 'audio_available': self.audio_path(identity).is_file()}

    def delete(self, identity):
        with self.lock, self._connection() as db:
            row = db.execute("SELECT * FROM results WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            db.execute("DELETE FROM results WHERE id = ?", (identity,))
            self.audio_path(identity).unlink(missing_ok=True)
            # Only this result and its optional listening copy are removed.
            self.path(row).unlink(missing_ok=True)
