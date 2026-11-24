"""Run only in .diarization-venv; no dependencies are imported by the web server."""
import argparse
import json
import os
import warnings
import subprocess
import sys
import tempfile
from contextlib import contextmanager
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


@contextmanager
def audio_waveform(source, work):
    """Decode onto a disk-backed array, avoiding two full copies in RAM."""
    import av
    import numpy as np
    import torch
    with tempfile.TemporaryDirectory(prefix='speaker-audio-', dir=work) as folder:
        path = Path(folder) / 'mono.f32'
        samples = 0
        with path.open('wb') as target, av.open(str(source), options={'protocol_whitelist': 'file,pipe,crypto,data'}) as audio:
            if not audio.streams.audio:
                raise ValueError('В записи нет аудиодорожки.')
            resampler = av.AudioResampler(format='flt', layout='mono', rate=16000)
            for frame in audio.decode(audio.streams.audio[0]):
                for converted in resampler.resample(frame):
                    data = converted.to_ndarray()
                    target.write(data.tobytes())
                    samples += data.size
            for converted in resampler.resample(None):
                data = converted.to_ndarray()
                target.write(data.tobytes())
                samples += data.size
        if not samples:
            raise ValueError('Аудиозапись пустая.')
        mapped = np.memmap(path, dtype='float32', mode='r+', shape=(1, samples))
        waveform = torch.from_numpy(mapped)
        try:
            yield waveform
        finally:
            del waveform
            mapped._mmap.close()


def choose_device(torch, requested):
    if requested == 'cpu':
        return 'cpu'
    if torch.cuda.is_available():
        return 'cuda'
    if requested == 'cuda':
        raise RuntimeError('CUDA для говорящих недоступна. Запустите setup-speakers.cmd -Device cuda.')
    return 'cpu'


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
    parser.add_argument('--device', choices=['auto', 'cuda', 'cpu'], default='auto')
    parser.add_argument('--batch-size', type=int, choices=[1, 2, 4, 8, 16, 32], default=16)
    parser.add_argument('--vram-gib', type=float, default=4)
    parser.add_argument('--ram-gib', type=float, default=4)
    args = parser.parse_args()
    actual_device = 'cpu'
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
        else:
            from speaker_resources import limit_ram
            limit_ram(args.ram_gib)
        from pyannote.audio import Pipeline
        from pyannote.audio.core.task import Specifications, Problem, Resolution
        import torch
        # Community-1 checkpoints include these three pyannote metadata types.
        # Keep PyTorch's weights-only loader and scope its allowlist to loading.
        with torch.serialization.safe_globals([Specifications, Problem, Resolution]):
            pipeline = Pipeline.from_pretrained(str(args.model_dir))
        if args.verify_model:
            pipeline.to(torch.device('cpu'))
            (args.model_dir / '.ready').write_text('community-1 verified locally', encoding='utf-8')
            status('Модель готова')
            return 0
        from speaker_resources import configure_gpu, memory_usage
        from job_worker import ProgressWriter
        progress = ProgressWriter(args.progress)
        actual_device = choose_device(torch, args.device)
        if actual_device == 'cuda':
            gpu_budget = configure_gpu(torch, args.vram_gib)
            print(f'Говорящие: NVIDIA GPU; бюджет аллокатора {gpu_budget / 1024**3:.2f} ГиБ; RAM {args.ram_gib:g} ГиБ; пакет {args.batch_size}.', flush=True)
        else:
            args.batch_size = min(args.batch_size, 4)
            print('Говорящие: CPU (CUDA недоступна или выбран процессор).', flush=True)
        pipeline.segmentation_batch_size = args.batch_size
        pipeline.embedding_batch_size = args.batch_size
        pipeline.to(torch.device(actual_device))
        progress(stage='Подготовка аудио для говорящих', progress=65, processed=0, total=None, device=actual_device)
        last_progress = 65
        def hook(step_name, step_artifact=None, file=None, total=None, completed=None):
            nonlocal last_progress
            labels = {'segmentation': ('Поиск смены голоса', 66, 12),
                      'embeddings': ('Сравнение голосов', 78, 15), 'clustering': ('Группировка говорящих', 93, 4)}
            label, base, span = labels.get(step_name, ('Разделение по говорящим', 66, 0))
            fraction = min(1, completed / total) if total and completed is not None else 0
            last_progress = max(last_progress, base + span * fraction)
            progress(stage=f'{label} · {"NVIDIA" if actual_device == "cuda" else "CPU"} · пакет {args.batch_size}',
                     progress=last_progress, device=actual_device)
        kwargs = {'num_speakers': args.num_speakers} if args.num_speakers else {}
        with audio_waveform(args.source, args.output.parent) as waveform, torch.inference_mode():
            result = pipeline({'waveform': waveform, 'sample_rate': 16000}, hook=hook, **kwargs)
            del waveform
        annotation = result.exclusive_speaker_diarization
        turns = [{'start': float(turn.start), 'end': float(turn.end), 'speaker': str(speaker)}
                 for turn, _, speaker in annotation.itertracks(yield_label=True)]
        args.output.write_text(json.dumps(turns, ensure_ascii=False), encoding='utf-8')
        metrics = {**memory_usage(), 'device': actual_device, 'batch_size': args.batch_size,
                   'vram_peak_bytes': torch.cuda.max_memory_reserved(0) if actual_device == 'cuda' else 0}
        args.output.with_suffix('.resources.json').write_text(json.dumps(metrics), encoding='utf-8')
        print('Ресурсы: ' + json.dumps(metrics), flush=True)
        return 0
    except Exception as error:
        if actual_device == 'cuda' and (type(error).__name__ == 'OutOfMemoryError' or any(
                text in str(error) for text in ('CUDA out of memory', 'CUDA error: out of memory',
                                                'CUDNN_STATUS_ALLOC_FAILED', 'CUBLAS_STATUS_ALLOC_FAILED'))):
            print('Недостаточно видеопамяти для этого пакета. [CUDA_OOM]', flush=True)
            return 20
        if isinstance(error, MemoryError):
            print(f'Недостаточно памяти в пределах лимита RAM {args.ram_gib:g} ГиБ. [RAM_LIMIT]', flush=True)
            return 21
        if args.download or args.verify_model:
            message = preparation_error(error, stage)
            status('Ошибка подготовки модели', error=message)
            print(message, flush=True)
        else:
            print(f'Ошибка диаризации: {error}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
