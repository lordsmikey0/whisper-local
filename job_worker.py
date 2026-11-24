"""One isolated, cancellable job. Progress is written as atomic JSON snapshots."""
import argparse
import json
from pathlib import Path
import time
import subprocess
import sys
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


def recognize(args, progress):
    import transcribe
    def asr_progress(**values):
        if args.diarize and values.get('progress') is not None:
            values['progress'] *= .65
        elif args.keep_audio and values.get('progress') is not None:
            values['progress'] *= .9
        progress(**values)
    transcribe.run(SimpleNamespace(audio=args.source, output=args.output, model=args.model,
                                  language=args.language, device=args.device, offline=args.offline,
                                  overwrite=False, download_only=False, progress_callback=asr_progress,
                                  diarize=args.diarize, keep_audio=args.keep_audio))


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
    parser.add_argument("--output-format", default="mp3")
    parser.add_argument("--quality", default="balanced")
    parser.add_argument("--resolution", default="source")
    parser.add_argument("--fps", default="source")
    parser.add_argument("--trim-start", type=float, default=0)
    parser.add_argument("--trim-end", type=float)
    parser.add_argument("--mute", action="store_true")
    parser.add_argument('--diarize', action='store_true')
    parser.add_argument('--keep-audio', action='store_true')
    parser.add_argument('--num-speakers', type=int)
    parser.add_argument('--asr-only', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    progress = ProgressWriter(args.progress)
    try:
        progress(stage="Подготовка", progress=None, processed=0, total=None)
        if args.mode == "convert":
            from convert import convert_media
            convert_media(args.source, args.output, vars(args), on_progress=progress)
        else:
            if args.diarize:
                from diarization import require_ready
                require_ready()
            if args.asr_only:
                recognize(args, progress)
                return 0
            if args.diarize:
                # Terminating the ASR child releases its CUDA context and allocator
                # before pyannote starts, including CTranslate2's cached VRAM.
                command = [sys.executable, '-X', 'utf8', '-u', str(Path(__file__).resolve()), *sys.argv[1:], '--asr-only']
                flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if sys.platform == 'win32' else {}
                if subprocess.run(command, stdout=sys.stdout, stderr=sys.stderr, **flags).returncode:
                    raise RuntimeError('Не удалось распознать запись. Подробности выше в журнале задачи.')
            else:
                recognize(args, progress)
            if args.diarize:
                from diarization import run_diarization
                from speaker_text import align_words, render_transcript, validate_data
                units = json.loads(args.output.with_suffix('.words.json').read_text(encoding='utf-8'))
                if units:
                    progress(stage='Загрузка модели говорящих · выбор устройства', progress=65, processed=0, total=None)
                    turns = run_diarization(args.source, args.output.with_suffix('.turns.json'), args.progress, args.num_speakers, device=args.device)
                else:
                    turns = []
                progress(stage='Разметка реплик', progress=98)
                data = validate_data(align_words(units, turns))
                args.output.with_suffix('.speakers.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
                temporary = args.output.with_suffix('.named.partial')
                temporary.write_text(render_transcript(data), encoding='utf-8-sig')
                temporary.replace(args.output)
            if args.keep_audio:
                from convert import convert_media
                progress(stage='Сохранение аудио для прослушивания', progress=98, processed=0, total=None)
                convert_media(args.source, args.output.with_suffix('.playback.mp3'),
                              {'output_format': 'mp3', 'quality': 'compact'})
        progress(stage="Готово", progress=100)
        return 0
    except Exception as error:
        print(f"Ошибка: {error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
