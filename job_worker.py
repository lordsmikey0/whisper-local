"""One isolated, cancellable job. Progress is written as atomic JSON snapshots."""
import argparse
import json
from pathlib import Path
import time
from types import SimpleNamespace


class ProgressWriter:
    def __init__(self, path):
        self.path = Path(path)
        self.last_write = 0
        self.stage = None

    def __call__(self, **values):
        now = time.monotonic()
        if values.get("stage") == self.stage and now - self.last_write < 0.15:
            return
        self.stage = values.get("stage")
        self.last_write = now
        temp = self.path.with_suffix(".tmp")
        try:
            temp.write_text(json.dumps(values, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.path)
        except PermissionError:
            # A briefly open reader on Windows must not interrupt inference.
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--progress", required=True, type=Path)
    parser.add_argument("--mode", choices=("transcribe", "convert"), required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--language", default="auto")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    progress = ProgressWriter(args.progress)
    try:
        progress(stage="Подготовка", progress=None, processed=0, total=None)
        if args.mode == "convert":
            from convert import mp4_to_mp3
            mp4_to_mp3(args.source, args.output, on_progress=progress)
        else:
            import transcribe
            transcribe.run(SimpleNamespace(audio=args.source, output=args.output, model=args.model,
                                          language=args.language, device=args.device, offline=args.offline,
                                          overwrite=False, download_only=False, progress_callback=progress))
        progress(stage="Готово", progress=100)
        return 0
    except Exception as error:
        print(f"Ошибка: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
