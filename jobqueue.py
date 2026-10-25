"""Single-worker queue; each job owns a subprocess and a private working folder."""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
TERMINAL = {"done", "error", "cancelled"}


@dataclass
class Job:
    id: str
    filename: str
    mode: str
    options: dict
    folder: Path
    source: Path
    output: Path
    state: str = "queued"
    stage: str = "В очереди"
    progress: float | None = None
    processed: float = 0
    total: float | None = None
    device: str | None = None
    error: str = ""
    started: float | None = None
    elapsed: float = 0
    cancelled: threading.Event = field(default_factory=threading.Event)

    @property
    def output_name(self):
        return Path(self.filename).stem + self.output.suffix


class TaskQueue:
    def __init__(self, root, processing_lock, history=None):
        root = Path(root).resolve()
        root.mkdir(exist_ok=True, parents=True)
        self.storage = tempfile.TemporaryDirectory(prefix="session-", dir=root)
        self.root = Path(self.storage.name).resolve()
        self.processing_lock = processing_lock
        self.history = history
        self.lock = threading.RLock()
        self.jobs = {}
        self.cancelled_ids = set()
        self.uploading = set()
        self.pending = queue.Queue()
        self.closing = threading.Event()
        self.thread = threading.Thread(target=self._worker, name="whisper-queue", daemon=True)
        self.thread.start()

    def enqueue(self, filename, mode, stream, identity=None, **options):
        filename = Path(filename.replace("\\", "/")).name or "audio"
        if mode == "convert" and Path(filename).suffix.lower() != ".mp4":
            raise ValueError("Для конвертации выберите MP4.")
        suffix = Path(filename).suffix
        if len(suffix) > 20:
            suffix = ".audio"
        identity = uuid.UUID(identity).hex if identity else uuid.uuid4().hex
        folder = self.root / identity
        with self.lock:
            if identity in self.jobs:
                return self._snapshot(self.jobs[identity])
            if identity in self.uploading:
                raise ValueError("Этот файл уже загружается.")
            folder.mkdir()
            self.uploading.add(identity)
        source = folder / ("source" + suffix)
        output = folder / ("result.mp3" if mode == "convert" else "result.txt")
        try:
            with source.open("wb") as target:
                shutil.copyfileobj(stream, target, length=1024 * 1024)
            if not source.stat().st_size:
                raise ValueError("Файл пустой.")
            job = Job(identity, filename, mode, options, folder, source, output)
            with self.lock:
                if self.closing.is_set():
                    raise ValueError("Сервер останавливается.")
                if identity in self.cancelled_ids:
                    job.cancelled.set()
                    job.state, job.stage = "cancelled", "Отменено"
                self.jobs[identity] = job
                self.pending.put(job)
                return self._snapshot(job)
        except BaseException:
            self._remove_folder(folder)
            raise
        finally:
            with self.lock:
                self.uploading.discard(identity)

    def _remove_folder(self, folder):
        # Every recursive removal is confined to this queue's own session directory.
        folder = folder.resolve()
        if folder.parent != self.root:
            raise ValueError("Unexpected job directory")
        shutil.rmtree(folder)

    def _snapshot(self, job):
        return {"id": job.id, "filename": job.filename, "mode": job.mode, "state": job.state,
                "stage": job.stage, "progress": job.progress, "processed": job.processed,
                "total": job.total, "device": job.device, "error": job.error,
                "elapsed": round(time.monotonic() - job.started, 1) if job.started and job.state not in TERMINAL else job.elapsed,
                "output_name": job.output_name}

    def snapshots(self):
        with self.lock:
            return [self._snapshot(job) for job in self.jobs.values()]

    def get(self, identity):
        with self.lock:
            return self.jobs[identity]

    def cancel(self, identity):
        with self.lock:
            self.cancelled_ids.add(identity)
            if identity not in self.jobs:
                # Remember cancellation even if the HTTP upload hasn't reached enqueue yet.
                return {"id": identity, "state": "cancelled"}
            job = self.jobs[identity]
            if job.state not in TERMINAL:
                job.cancelled.set()
                job.state = "cancelling" if job.started else "cancelled"
                job.stage = "Отмена…" if job.started else "Отменено"
            return self._snapshot(job)

    def cancel_all(self):
        with self.lock:
            self.cancelled_ids.update(self.uploading)
            for identity in self.jobs:
                self.cancel(identity)

    def _command(self, job):
        command = [sys.executable, "-X", "utf8", "-u", str(ROOT / "job_worker.py"), str(job.source),
                   str(job.output), "--progress", str(job.folder / "progress.json"), "--mode", job.mode]
        for option in ("model", "language", "device"):
            command.extend(["--" + option, job.options.get(option, {"model": "small", "language": "auto", "device": "auto"}[option])])
        if job.options.get("offline", True):
            command.append("--offline")
        return command

    @staticmethod
    def _stop_process(process):
        if process.poll() is not None:
            return
        if os.name == "nt":
            # The Windows venv launcher has a Python child; terminate the whole tree.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=3)

    def _read_progress(self, job):
        try:
            values = json.loads((job.folder / "progress.json").read_text(encoding="utf-8"))
            with self.lock:
                if job.state != "running":
                    return
                for key in ("stage", "progress", "processed", "total", "device"):
                    if key in values:
                        setattr(job, key, values[key])
                if job.progress is not None:
                    job.progress = max(0, min(99, float(job.progress)))
        except (OSError, ValueError, TypeError):
            pass

    def _run(self, job):
        process = None
        try:
            with self.lock:
                if job.cancelled.is_set():
                    return
                job.state, job.stage = "running", "Подготовка"
                job.started = time.monotonic()
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            with (job.folder / "worker.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(self._command(job), cwd=ROOT, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, **flags)
                while process.poll() is None:
                    if job.cancelled.wait(0.15):
                        self._stop_process(process)
                        break
                    self._read_progress(job)
            with self.lock:
                if job.cancelled.is_set():
                    job.state, job.stage = "cancelled", "Отменено"
                elif process.returncode == 0 and job.output.is_file():
                    job.elapsed = round(time.monotonic() - job.started, 1)
                    if self.history is not None:
                        job.output = self.history.save(job)
                    job.state, job.stage, job.progress = "done", "Готово", 100
                else:
                    job.state, job.stage = "error", "Ошибка"
                    job.error = (job.folder / "worker.log").read_text(encoding="utf-8", errors="replace")[-2500:].strip() or "Не удалось обработать файл."
        except Exception as error:
            if process and process.poll() is None:
                self._stop_process(process)
            with self.lock:
                job.state = "cancelled" if job.cancelled.is_set() else "error"
                job.stage = "Отменено" if job.cancelled.is_set() else "Ошибка"
                job.error = str(error)
        finally:
            with self.lock:
                job.elapsed = round(time.monotonic() - job.started, 1) if job.started else 0
                # Delete source and partial results only after the worker has exited.
                if job.state != "done":
                    self._remove_folder(job.folder)
                else:
                    job.source.unlink(missing_ok=True)
                    for extra in job.folder.iterdir():
                        if extra != job.output:
                            extra.unlink(missing_ok=True)

    def _worker(self):
        while True:
            job = self.pending.get()
            if job is None:
                return
            acquired = False
            try:
                while not job.cancelled.is_set() and not self.closing.is_set():
                    acquired = self.processing_lock.acquire(timeout=0.1)
                    if acquired:
                        break
                if not acquired:
                    with self.lock:
                        job.state, job.stage = "cancelled", "Отменено"
                    self._remove_folder(job.folder)
                else:
                    self._run(job)
            except Exception as error:
                with self.lock:
                    job.state, job.stage, job.error = "error", "Ошибка", str(error)
            finally:
                if acquired:
                    self.processing_lock.release()

    def archive(self):
        with self.lock:
            completed = [(job.output, job.output_name) for job in self.jobs.values() if job.state == "done"]
        if not completed:
            raise ValueError("Пока нет готовых файлов.")
        path = self.root / (uuid.uuid4().hex + ".zip")
        names = set()
        try:
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for output, name in completed:
                    candidate, number = name, 2
                    while candidate.casefold() in names:
                        candidate = f"{Path(name).stem} ({number}){Path(name).suffix}"
                        number += 1
                    names.add(candidate.casefold())
                    with self.lock:
                        # History deletion uses the same lock: don't remove a file during ZIP creation.
                        if output.is_file():
                            archive.write(output, arcname=candidate)
            return path
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def close(self):
        self.closing.set()
        self.cancel_all()
        self.pending.put(None)
        self.thread.join(timeout=15)
        if not self.thread.is_alive():
            # TemporaryDirectory owns the verified session directory under root.
            self.storage.cleanup()
