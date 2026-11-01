"""Run only in .diarization-venv; no dependencies are imported by the web server."""
import argparse
import json
import os
import warnings
import subprocess
import sys
from pathlib import Path

os.environ['PYANNOTE_METRICS_ENABLED'] = '0'
os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS_WARNING', '1')
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
    args = parser.parse_args()
    try:
        if args.download:
            from huggingface_hub import snapshot_download
            snapshot_download('pyannote/speaker-diarization-community-1', local_dir=str(args.model_dir),
                              token=os.environ.get('HF_TOKEN'))
            env = os.environ.copy()
            env.update({'HF_HUB_OFFLINE': '1', 'CUDA_VISIBLE_DEVICES': '-1'})
            env.pop('HF_TOKEN', None)
            env.pop('HUGGING_FACE_HUB_TOKEN', None)
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
            return subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
                                   '--verify-model', '--model-dir', str(args.model_dir)], env=env, **flags).returncode
        os.environ['HF_HUB_OFFLINE'] = '1'
        from pyannote.audio import Pipeline
        import torch
        pipeline = Pipeline.from_pretrained(str(args.model_dir))
        pipeline.to(torch.device('cpu'))
        if args.verify_model:
            (args.model_dir / '.ready').write_text('community-1 verified locally', encoding='utf-8')
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
        # Download errors can contain request details: keep all credentials out of logs.
        print('Не удалось скачать модель: проверьте доступ на Hugging Face.' if args.download else f'Ошибка диаризации: {error}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
