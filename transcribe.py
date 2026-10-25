"""Local Whisper audio-to-text utility. Python 3.10+."""

import argparse
import gc
import os
from pathlib import Path
import sys
import tempfile
import time
import warnings

ROOT = Path(__file__).resolve().parent
DLL_HANDLES = []
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "30")
# CTranslate2 4.6 still uses this API; requirements pins a compatible setuptools.
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.*", category=UserWarning)


def register_nvidia_dlls():
    """Expose NVIDIA runtime wheels without a system CUDA installation."""
    if sys.platform != "win32":
        return
    import sysconfig

    site = Path(sysconfig.get_paths()["purelib"])
    folders = [site / "nvidia" / name / "bin" for name in ("cublas", "cudnn")]
    for folder in folders:
        if folder.is_dir():
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
            DLL_HANDLES.append(os.add_dll_directory(str(folder)))


def parser():
    p = argparse.ArgumentParser(description="Локальное распознавание аудио в TXT с Whisper.")
    p.add_argument("audio", nargs="?", type=Path, help="Путь к MP3, WAV, M4A, FLAC и др.")
    p.add_argument("-o", "--output", type=Path, help="По умолчанию: рядом с аудио, расширение .txt")
    p.add_argument("--model", default="small", choices=("tiny", "base", "small", "medium", "large-v3", "turbo"))
    p.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    p.add_argument("--language", default="auto", help="auto, ru, en и другие коды языков")
    p.add_argument("--offline", action="store_true", help="Использовать только уже скачанную модель")
    p.add_argument("--download-only", action="store_true", help="Скачать модель без распознавания")
    p.add_argument("--overwrite", action="store_true", help="Разрешить замену существующего TXT")
    return p


def gpu_error(error):
    message = str(error).lower()
    return any(word in message for word in ("cuda", "cudnn", "cublas", "out of memory", "gpu"))


def report(args, stage, **values):
    callback = getattr(args, "progress_callback", None)
    if callback:
        callback(stage=stage, **values)


def transcribe(model, args, output):
    report(args, "Подготовка аудио", progress=None, processed=0, total=None)
    segments, info = model.transcribe(
        str(args.audio), language=None if args.language == "auto" else args.language,
        task="transcribe", beam_size=5, vad_filter=True,
    )
    print(f"Язык: {info.language}. Обработка записи…", flush=True)
    duration = getattr(info, "duration", None)
    report(args, "Распознавание", progress=0 if duration else None, processed=0, total=duration)
    # Keep an existing transcript intact if decoding/inference fails halfway through.
    temp_path = None
    count = 0
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8-sig", newline="\n",
                                         dir=output.parent, suffix=".partial", delete=False) as f:
            temp_path = Path(f.name)
            for segment in segments:
                text = segment.text.strip()
                if text:
                    f.write(text + "\n")
                    count += 1
                print(f"\rОбработано до {segment.end:.0f} сек.", end="", flush=True)
                report(args, "Распознавание", processed=segment.end, total=duration,
                       progress=min(99, segment.end / duration * 100) if duration else None)
        print()
        if args.overwrite:
            os.replace(temp_path, output)
        elif sys.platform == "win32":
            # Windows rename is atomic and refuses to replace an existing file.
            os.rename(temp_path, output)
        else:
            # Hard link publishes the completed file without overwriting a racing writer.
            os.link(temp_path, output)
            temp_path.unlink()
        if not count:
            print("Речь не обнаружена: создан пустой TXT.")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def run(args):
    output = None
    if not args.download_only:
        if args.audio is None:
            value = input("Перетащите аудиофайл в это окно и нажмите Enter: ").strip().strip('"')
            if not value:
                raise ValueError("Не указан аудиофайл.")
            args.audio = Path(value)
        args.audio = args.audio.expanduser().resolve()
        if not args.audio.is_file():
            raise ValueError(f"Аудиофайл не найден: {args.audio}")
        output = (args.output or args.audio.with_suffix(".txt")).expanduser().resolve()
        if output == args.audio:
            raise ValueError("Пути аудио и результата не должны совпадать.")
        if output.suffix.lower() != ".txt":
            raise ValueError("Укажите для результата расширение .txt.")
        if output.exists() and not args.overwrite:
            raise ValueError(f"Файл уже существует: {output}. Укажите другой -o или --overwrite.")
        if not output.parent.is_dir():
            raise ValueError(f"Папка результата не найдена: {output.parent}")

    register_nvidia_dlls()
    try:
        import ctranslate2
        from faster_whisper import WhisperModel, download_model
    except ImportError as error:
        raise RuntimeError(f"Ошибка загрузки библиотек: {error}. Запустите setup.cmd.") from error

    cache = ROOT / "models"
    print(f"Модель: {args.model}. Кэш: {cache}", flush=True)
    if not args.offline:
        print("При первом запуске модель будет скачана. Аудиозапись не отправляется в сеть.", flush=True)
    if args.download_only:
        location = download_model(args.model, cache_dir=str(cache), local_files_only=args.offline)
        print(f"Модель готова: {location}")
        return

    device = args.device
    if device == "auto":
        try:
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except RuntimeError:
            device = "cpu"
    start = time.monotonic()
    while True:
        model = None
        try:
            compute = "int8_float16" if device == "cuda" else "int8"
            print(f"Устройство: {device}; режим: {compute}.", flush=True)
            report(args, "Загрузка модели", progress=None, processed=0, total=None, device=device)
            model = WhisperModel(args.model, device=device, compute_type=compute,
                                 download_root=str(cache), local_files_only=args.offline,
                                 num_workers=1)
            transcribe(model, args, output)
            break
        except RuntimeError as error:
            if args.device == "auto" and device == "cuda" and gpu_error(error):
                print(f"\nНе удалось использовать видеокарту: {error}\nПовторяю на процессоре.", flush=True)
                del model
                gc.collect()
                device = "cpu"
                report(args, "Повтор на процессоре после ошибки GPU", progress=None, processed=0, device="cpu")
                continue
            raise
    print(f"Готово за {time.monotonic() - start:.1f} сек.\nСтенограмма: {output}")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        run(args)
        return 0
    except KeyboardInterrupt:
        print("\nОтменено. Незавершённый результат не сохранён.", file=sys.stderr)
        return 130
    except Exception as error:
        print(f"\nОшибка: {error}", file=sys.stderr)
        if args.offline:
            print("Если модели нет в кэше, сначала запустите run.cmd --download-only без --offline.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
