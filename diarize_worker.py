"""Run only in .diarization-venv; no dependencies are imported by the web server."""
import argparse
import json
import os
import warnings
import subprocess
import sys
from pathlib import Path
from speaker_errors import preparation_error

os.environ['PYANNOTE_METRICS_ENABLED'] = '0'
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
os.environ.setdefault('HF_HUB_ETAG_TIMEOUT', '30')
os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '120')
# Audio is decoded with PyAV and passed as a waveform. TorchCodec's optional
# shared-FFmpeg decoder is not used by this integration.
warnings.filterwarnings('ignore', message=r'\s*torchcodec is not installed correctly.*', category=UserWarning)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true')
    parser.add_argument('--verify-model', action='store_true')
    parser.add_argument('--model-dir', type=Path, required=True)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--progress', type=Path)
    parser.add_argument('--num-speakers', type=int)
    parser.add_argument('--status-file', type=Path)
    args = parser.parse_args()
    stage = 'verify' if args.verify_model else 'download' if args.download else 'inference'
    def status(message, error=''):
        if args.status_file:
            temporary = args.status_file.with_suffix('.partial')
            temporary.write_text(json.dumps({'stage': message, 'error': error}, ensure_ascii=False), encoding='utf-8')
            temporary.replace(args.status_file)
    try:
        if args.download:
            from huggingface_hub import HfApi, hf_hub_download, snapshot_download
            stage = 'token'
            status('Проверка токена Hugging Face')
            token = os.environ.get('HF_TOKEN')
            if not token:
                raise ValueError('Token required')
            HfApi().whoami(token=token)
            stage = 'access'
            status('Проверка доступа к Community-1')
            hf_hub_download('pyannote/speaker-diarization-community-1', 'config.yaml',
                            local_dir=str(args.model_dir), token=token)
            stage = 'download'
            status('Скачивание весов Community-1')
            snapshot_download('pyannote/speaker-diarization-community-1', local_dir=str(args.model_dir),
                              token=token, max_workers=4)
            env = os.environ.copy()
            env.update({'HF_HUB_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': '-1'})
            env.pop('HF_TOKEN', None)
            env.pop('HUGGING_FACE_HUB_TOKEN', None)
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            command = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                       '--verify-model', '--model-dir', str(args.model_dir)]
            if args.status_file:
                command += ['--status-file', str(args.status_file)]
            return subprocess.run(command, env=env, **flags).returncode
        os.environ['HF_HUB_OFFLINE'] = '1'
        if args.verify_model:
            status('Проверка локальной загрузки модели · CPU')
            (args.model_dir / '.ready').unlink(missing_ok=True)
        from pyannote.audio import Pipeline
        from pyannote.audio.core.task import Specifications, Problem, Resolution
        import torch
        # Community-1 checkpoints include these three pyannote metadata types.
        # Keep PyTorch's weights-only loader and scope its allowlist to loading.
        with torch.serialization.safe_globals([Specifications, Problem, Resolution]):
            pipeline = Pipeline.from_pretrained(str(args.model_dir))
        pipeline.to(torch.device('cpu'))
        if args.verify_model:
            (args.model_dir / '.ready').write_text('community-1 verified locally', encoding='utf-8')
            status('Модель готова')
            return 0
        import av
        import numpy as np
        from job_worker import ProgressWriter
        progress = ProgressWriter(args.progress)
        progress(stage='Подготовка аудио для говорящих', progress=65, processed=0, total=None, device='cpu')
        chunks = []
        with av.open(str(args.source), options={'protocol_whitelist': 'file,pipe,crypto,data'}) as audio:
            if not audio.streams.audio:
                raise ValueError('В записи нет аудиодорожки.')
            resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
            for frame in audio.decode(audio.streams.audio[0]):
                chunks.extend(f.to_ndarray() for f in resampler.resample(frame))
            chunks.extend(f.to_ndarray() for f in resampler.resample(None))
        if not chunks:
            raise ValueError('Аудиозапись пустая.')
        waveform = torch.from_numpy(np.concatenate(chunks, axis=1))
        last_progress = 65
        def hook(step_name, step_artifact=None, file=None, total=None, completed=None):
            nonlocal last_progress
            labels = {'segmentation': ('Поиск смены голоса', 66, 12),
                      'embeddings': ('Сравнение голосов', 78, 15), 'clustering': ('Группировка говорящих', 93, 4)}
            label, base, span = labels.get(step_name, ('Разделение по говорящим', 66, 0))
            fraction = min(1, completed / total) if total and completed is not None else 0
            last_progress = max(last_progress, base + span * fraction)
            progress(stage=label, progress=last_progress, device='cpu')
        kwargs = {'num_speakers': args.num_speakers} if args.num_speakers else {}
        result = pipeline({'waveform': waveform, 'sample_rate': 16000}, hook=hook, **kwargs)
        annotation = result.exclusive_speaker_diarization
        turns = [{'start': float(turn.start), 'end': float(turn.end), 'speaker': str(speaker)}
                 for turn, _, speaker in annotation.itertracks(yield_label=True)]
        args.output.write_text(json.dumps(turns, ensure_ascii=False), encoding='utf-8')
        return 0
    except Exception as error:
        if args.download or args.verify_model:
            message = preparation_error(error, stage)
            status('Ошибка подготовки модели', error=message)
            print(message, flush=True)
        else:
            print(f'Ошибка диаризации: {error}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
