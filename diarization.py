"""Optional isolated pyannote runtime and model preparation."""
import json
import os
from pathlib import Path
import subprocess
import threading

ROOT = Path(__file__).resolve().parent
MODEL_ID = 'pyannote/speaker-diarization-community-1'


def runtime_python(root=ROOT):
    return Path(root) / '.diarization-venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def model_path(root=ROOT):
    return Path(root) / 'models' / 'pyannote-community-1'


def readiness(root=ROOT):
    runtime = runtime_python(root)
    libraries = runtime.is_file() and (runtime.parents[1] / '.ready').is_file()
    model = (model_path(root) / '.ready').is_file()
    return {'libraries_ready': libraries, 'model_ready': model, 'ready': libraries and model}


def require_ready(root=ROOT):
    state = readiness(root)
    if not state['libraries_ready']:
        raise ValueError('Для разделения по говорящим сначала запустите setup-speakers.cmd.')
    if not state['model_ready']:
        raise ValueError('Сначала скачайте модель в блоке «Настройка говорящих».')


def run_diarization(source, output, progress_file, num_speakers=None):
    require_ready()
    env = os.environ.copy()
    env.update({'HF_HUB_OFFLINE': '1', 'PYANNOTE_METRICS_ENABLED': '0', 'CUDA_VISIBLE_DEVICES': '-1', 'PYTHONUTF8': '1'})
    # Credentials are neither needed nor forwarded during offline inference.
    env.pop('HF_TOKEN', None)
    env.pop('HUGGING_FACE_HUB_TOKEN', None)
    command = [str(runtime_python()), '-X', 'utf8', str(ROOT / 'diarize_worker.py'),
               '--source', str(source), '--output', str(output), '--model-dir', str(model_path()),
               '--progress', str(progress_file)]
    if num_speakers:
        command += ['--num-speakers', str(num_speakers)]
    flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run(command, env=env, capture_output=True, encoding='utf-8', errors='replace', **flags)
    if result.returncode:
        detail = (result.stdout + '\n' + result.stderr).strip()[-2000:]
        raise RuntimeError('Не удалось разделить говорящих. ' + detail)
    return json.loads(Path(output).read_text(encoding='utf-8'))


class ModelPreparation:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.process = None
        self.thread = None
        self.error = ''
        self.closing = False

    def status(self):
        with self.lock:
            return {**readiness(self.root), 'preparing': bool(self.thread and self.thread.is_alive()),
                    'error': self.error, 'model_url': 'https://huggingface.co/' + MODEL_ID}

    def start(self, token):
        with self.lock:
            if self.closing:
                raise ValueError('Сервер останавливается.')
            if self.thread and self.thread.is_alive():
                raise ValueError('Модель уже скачивается.')
            if not readiness(self.root)['libraries_ready']:
                raise ValueError('Сначала запустите setup-speakers.cmd в папке проекта.')
            if not token or len(token) > 1000 or '\n' in token or '\r' in token:
                raise ValueError('Введите токен Hugging Face для скачивания модели.')
            self.error = ''
            self.thread = threading.Thread(target=self._prepare, args=(token,), daemon=True, name='speaker-model-download')
            self.thread.start()

    def _prepare(self, token):
        env = os.environ.copy()
        env.update({'HF_TOKEN': token, 'PYANNOTE_METRICS_ENABLED': '0', 'PYTHONUTF8': '1',
                    'HF_HUB_DISABLE_TELEMETRY': '1', 'HF_HUB_DISABLE_XET': '1'})
        env.pop('HF_HUB_OFFLINE', None)
        try:
            command = [str(runtime_python(self.root)), '-X', 'utf8', str(self.root / 'diarize_worker.py'),
                       '--download', '--model-dir', str(model_path(self.root))]
            flags = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            with self.lock:
                if self.closing:
                    return
                self.process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL,
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **flags)
            code = self.process.wait()
            if code or not readiness(self.root)['model_ready']:
                with self.lock:
                    self.error = 'Не удалось подготовить модель. Проверьте интернет, токен и принятые условия доступа к Community-1 на Hugging Face.'
        except Exception:
            with self.lock:
                self.error = 'Не удалось запустить подготовку модели. Повторите установку setup-speakers.cmd.'
        finally:
            env.pop('HF_TOKEN', None)
            with self.lock:
                self.process = None

    def close(self):
        from jobqueue import TaskQueue
        with self.lock:
            self.closing = True
            process = self.process
        if process and process.poll() is None:
            TaskQueue._stop_process(process)
        if self.thread:
            self.thread.join(timeout=10)
