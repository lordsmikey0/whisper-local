"""Local web UI; run with: python -m uvicorn web:app --host 127.0.0.1."""

from pathlib import Path
from contextlib import asynccontextmanager
import subprocess
import sys
import tempfile
import threading
import time
from typing import Annotated, Literal
from uuid import UUID
from pydantic import BaseModel, Field

import av
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from convert import mp4_to_mp3
from jobqueue import TaskQueue
from history import HistoryStore
from media_formats import FORMATS, result_mime
from diarization import ModelPreparation

ROOT = Path(__file__).resolve().parent
WORK = ROOT / ".web-tmp"
WORK.mkdir(exist_ok=True)
BUSY = threading.Lock()
HISTORY_ROOT = ROOT / "history"
@asynccontextmanager
async def lifespan(app):
    history = HistoryStore(HISTORY_ROOT)
    app.state.history = history
    preparation = ModelPreparation(ROOT)
    app.state.speaker_preparation = preparation
    manager = TaskQueue(ROOT / ".web-jobs", BUSY, history=history)
    app.state.jobs = manager
    try:
        yield
    finally:
        await run_in_threadpool(preparation.close)
        await run_in_threadpool(manager.close)


app = FastAPI(title="Whisper · Аудио в текст", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


def upload_name(file):
    return Path((file.filename or "audio").replace("\\", "/")).name


def save_upload(file, path):
    with path.open("wb") as target:
        while chunk := file.file.read(1024 * 1024):
            target.write(chunk)
    if not path.stat().st_size:
        raise HTTPException(400, "Файл пустой. Выберите другую запись.")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "busy": BUSY.locked()}


@app.get("/api/formats")
def formats():
    return {"formats": [{"id": key, **value} for key, value in FORMATS.items()]}


class ModelAccess(BaseModel):
    token: str = Field(min_length=1, max_length=1000, repr=False)


class SpeakerNames(BaseModel):
    names: dict[str, str] = Field(default_factory=dict)


@app.get('/api/diarization/status')
def speaker_status(request: Request):
    return request.app.state.speaker_preparation.status()


@app.post('/api/diarization/prepare', status_code=202)
def prepare_speaker_model(request: Request, access: ModelAccess):
    try:
        request.app.state.speaker_preparation.start(access.token)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return request.app.state.speaker_preparation.status()


def manager_for(request):
    return request.app.state.jobs


def find_job(request, identity):
    try:
        return manager_for(request).get(identity)
    except KeyError:
        raise HTTPException(404, "Задача не найдена. Возможно, сервер был перезапущен.") from None


@app.get("/api/jobs")
def list_jobs(request: Request):
    return {"jobs": manager_for(request).snapshots()}


@app.post("/api/jobs", status_code=202)
def enqueue_job(
    request: Request, file: Annotated[UploadFile, File()],
    mode: Annotated[Literal["transcribe", "convert"], Form()] = "transcribe",
    model: Annotated[Literal["tiny", "base", "small", "medium"], Form()] = "small",
    language: Annotated[Literal["auto", "ru", "en"], Form()] = "auto",
    device: Annotated[Literal["auto", "cpu", "cuda"], Form()] = "auto",
    offline: Annotated[bool, Form()] = True,
    client_id: Annotated[UUID | None, Form()] = None,
    output_format: Annotated[Literal["mp3", "wav", "flac", "m4a", "aac", "ogg", "opus", "mp4", "mkv", "mov", "webm"], Form()] = "mp3",
    quality: Annotated[Literal["compact", "balanced", "high"], Form()] = "balanced",
    resolution: Annotated[Literal["source", "1080", "720", "480"], Form()] = "source",
    fps: Annotated[Literal["source", "24", "25", "30", "60"], Form()] = "source",
    trim_start: Annotated[float, Form()] = 0,
    trim_end: Annotated[float | None, Form()] = None,
    mute: Annotated[bool, Form()] = False,
    diarize: Annotated[bool, Form()] = False,
    keep_audio: Annotated[bool, Form()] = False,
    num_speakers: Annotated[int | None, Form()] = None,
):
    try:
        return manager_for(request).enqueue(upload_name(file), mode, file.file,
                                            identity=client_id.hex if client_id else None,
                                            model=model, language=language, device=device, offline=offline,
                                            output_format=output_format, quality=quality, resolution=resolution,
                                            fps=fps, trim_start=trim_start, trim_end=trim_end, mute=mute,
                                            diarize=diarize, num_speakers=num_speakers, keep_audio=keep_audio)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    finally:
        file.file.close()


@app.post("/api/jobs/cancel-all")
def cancel_all_jobs(request: Request):
    manager_for(request).cancel_all()
    return {"jobs": manager_for(request).snapshots()}


@app.post("/api/jobs/{identity}/cancel")
def cancel_job(request: Request, identity: UUID):
    return manager_for(request).cancel(identity.hex)


@app.get("/api/jobs/archive")
def download_archive(request: Request):
    try:
        path = manager_for(request).archive()
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return FileResponse(path, filename="whisper-results.zip", media_type="application/zip",
                        background=BackgroundTask(path.unlink, missing_ok=True))


@app.get("/api/jobs/{identity}/download")
def download_job(request: Request, identity: str):
    job = find_job(request, identity)
    if job.state != "done":
        raise HTTPException(409, "Результат ещё не готов.")
    return FileResponse(job.output, filename=job.output_name,
                        media_type=result_mime(job.output.suffix))


@app.get("/api/jobs/{identity}/result")
def job_text(request: Request, identity: str):
    job = find_job(request, identity)
    if job.state != "done" or job.mode != "transcribe":
        raise HTTPException(409, "Стенограмма ещё не готова.")
    return request.app.state.history.transcript(identity)


def history_record(request, identity):
    try:
        return request.app.state.history.get(identity)
    except KeyError:
        raise HTTPException(404, "Результат не найден в истории.") from None


@app.get("/api/history")
def list_history(request: Request):
    items = request.app.state.history.list()
    return {"items": items, "count": len(items), "bytes": sum(item["size"] + item['audio_size'] for item in items)}


@app.get("/api/history/{identity}/download")
def download_history(request: Request, identity: UUID):
    record = history_record(request, identity.hex)
    if not record["available"]:
        raise HTTPException(404, "Файл результата не найден на диске.")
    return FileResponse(request.app.state.history.path(record), filename=record["output_name"],
                        media_type=result_mime(request.app.state.history.path(record).suffix))


@app.get("/api/history/{identity}/result")
def history_text(request: Request, identity: UUID):
    record = history_record(request, identity.hex)
    if record["mode"] != "transcribe":
        raise HTTPException(409, "Этот результат — аудиофайл, а не стенограмма.")
    if not record["available"]:
        raise HTTPException(404, "Файл результата не найден на диске.")
    return request.app.state.history.transcript(identity.hex)


@app.get('/api/history/{identity}/audio')
def transcript_audio(request: Request, identity: UUID):
    record = history_record(request, identity.hex)
    path = request.app.state.history.audio_path(identity.hex)
    if record['mode'] != 'transcribe' or not path.is_file():
        raise HTTPException(404, 'Аудио для этой стенограммы не сохранено.')
    return FileResponse(path, media_type='audio/mpeg', headers={'Cache-Control': 'no-cache'})


@app.put('/api/history/{identity}/speakers')
def update_speaker_names(request: Request, identity: UUID, body: SpeakerNames):
    record = history_record(request, identity.hex)
    if record['mode'] != 'transcribe':
        raise HTTPException(400, 'Имена можно задать только для стенограммы.')
    with manager_for(request).lock:
        try:
            return request.app.state.history.rename_speakers(identity.hex, body.names)
        except (ValueError, FileNotFoundError) as error:
            raise HTTPException(400, str(error)) from error
        except PermissionError:
            raise HTTPException(409, 'Файл сейчас скачивается. Повторите сохранение имён.') from None


@app.delete("/api/history/{identity}")
def delete_history(request: Request, identity: UUID):
    manager = manager_for(request)
    with manager.lock:
        try:
            request.app.state.history.delete(identity.hex)
        except KeyError:
            raise HTTPException(404, "Результат уже удалён.") from None
        except PermissionError:
            raise HTTPException(409, "Файл сейчас используется. Закройте его и повторите удаление.") from None
        # The current-session queue must not retain a download link to a deleted result.
        job = manager.jobs.get(identity.hex)
        if job is not None and job.state == "done":
            del manager.jobs[identity.hex]
    return {"deleted": identity.hex}


@app.post("/api/transcribe")
def recognize(
    file: Annotated[UploadFile, File()],
    model: Annotated[Literal["tiny", "base", "small", "medium"], Form()] = "small",
    language: Annotated[Literal["auto", "ru", "en"], Form()] = "auto",
    device: Annotated[Literal["auto", "cpu", "cuda"], Form()] = "auto",
    offline: Annotated[bool, Form()] = True,
):
    # A single inference at a time keeps GPU memory use predictable.
    if not BUSY.acquire(blocking=False):
        file.file.close()
        raise HTTPException(409, "Уже идёт обработка файла. Дождитесь её завершения.")
    started = time.monotonic()
    try:
        filename = upload_name(file)
        suffix = Path(filename).suffix
        if len(suffix) > 20:
            suffix = ".audio"
        with tempfile.TemporaryDirectory(dir=WORK) as folder:
            audio = Path(folder) / ("audio" + suffix)
            output = Path(folder) / "transcript.txt"
            save_upload(file, audio)
            command = [sys.executable, "-X", "utf8", str(ROOT / "transcribe.py"), str(audio),
                       "-o", str(output), "--model", model, "--language", language, "--device", device]
            if offline:
                command.append("--offline")
            # Reuse the CLI and release all model memory after each request.
            result = subprocess.run(command, cwd=ROOT, capture_output=True,
                                    encoding="utf-8", errors="replace")
            print(result.stdout, flush=True)
            if result.returncode:
                print(result.stderr, file=sys.stderr, flush=True)
                detail = result.stderr.strip() or "Не удалось распознать файл. Подробности в окне сервера."
                raise HTTPException(422, detail[-2500:])
            return {"text": output.read_text(encoding="utf-8-sig"),
                    "filename": Path(filename).stem + ".txt",
                    "elapsed": round(time.monotonic() - started, 1)}
    finally:
        file.file.close()
        BUSY.release()


@app.post("/api/convert")
def convert(file: Annotated[UploadFile, File()]):
    if not BUSY.acquire(blocking=False):
        file.file.close()
        raise HTTPException(409, "Уже идёт обработка файла. Дождитесь её завершения.")
    folder = None
    try:
        filename = upload_name(file)
        if Path(filename).suffix.lower() != ".mp4":
            raise HTTPException(400, "Для конвертации выберите файл MP4.")
        folder = tempfile.TemporaryDirectory(dir=WORK)
        source = Path(folder.name) / "source.mp4"
        output = Path(folder.name) / "audio.mp3"
        save_upload(file, source)
        try:
            mp4_to_mp3(source, output)
        except (av.FFmpegError, ValueError) as error:
            raise HTTPException(422, f"Не удалось извлечь аудио: {error}") from error
        # Keep files alive until the MP3 response has finished streaming.
        response = FileResponse(output, media_type="audio/mpeg",
                                filename=Path(filename).stem + ".mp3",
                                background=BackgroundTask(folder.cleanup))
        folder = None
        return response
    finally:
        if folder is not None:
            folder.cleanup()
        file.file.close()
        BUSY.release()
