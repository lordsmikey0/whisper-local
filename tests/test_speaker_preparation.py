import errno
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import diarization
import diarize_worker
from speaker_errors import preparation_error


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'data')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def http_error(self, status):
        error = RuntimeError('sensitive Authorization: Bearer hf_never_expose_this_token')
        error.response = SimpleNamespace(status_code=status)
        return error

    def test_authentication_and_model_permission_errors_are_distinct(self):
        invalid = preparation_error(self.http_error(401), 'token')
        denied = preparation_error(self.http_error(403), 'access')
        self.assertIn('недействителен', invalid)
        self.assertIn('Fine-grained', denied)
        self.assertIn('[token/401]', invalid)
        self.assertIn('[access/403]', denied)
        self.assertNotIn('hf_never', invalid + denied)

    def test_connection_disk_and_loader_failures_do_not_blame_token(self):
        class ConnectTimeout(Exception):
            pass
        for error, stage, expected in [(ConnectTimeout('secret'), 'download', 'Соединение'),
                                       (OSError(errno.ENOSPC, 'secret'), 'download', 'места'),
                                       (RuntimeError('secret'), 'verify', 'локально'),
                                       (ImportError('secret'), 'verify', 'библиотеки')]:
            message = preparation_error(error, stage)
            self.assertIn(expected, message)
            self.assertNotIn('secret', message)

    def test_download_preflight_reports_bad_token_without_saving_it(self):
        status = self.root / 'status.json'
        api = SimpleNamespace(whoami=lambda **kwargs: (_ for _ in ()).throw(self.http_error(401)))
        hub = SimpleNamespace(HfApi=lambda: api, hf_hub_download=lambda *a, **kw: self.fail('download before authentication'),
                              snapshot_download=lambda *a, **kw: self.fail('snapshot before authentication'))
        args = ['worker', '--download', '--model-dir', str(self.root/'model'), '--status-file', str(status)]
        with patch.dict('sys.modules', {'huggingface_hub': hub}), patch('sys.argv', args), \
                patch.dict('os.environ', {'HF_TOKEN':'hf_never_expose_this_token'}), patch('sys.stdout', new_callable=io.StringIO) as log:
            self.assertEqual(diarize_worker.main(), 1)
        report = status.read_text(encoding='utf-8')
        self.assertIn('[token/401]', report)
        self.assertNotIn('hf_never', report + log.getvalue())
        self.assertFalse((self.root/'model/.ready').exists())

    def test_verified_download_passes_no_token_to_offline_loader(self):
        calls = []
        hub = SimpleNamespace(HfApi=lambda: SimpleNamespace(whoami=lambda **kw: calls.append('token')),
                              hf_hub_download=lambda *a, **kw: calls.append('access'),
                              snapshot_download=lambda *a, **kw: calls.append('download'))
        def verify(command, **kwargs):
            calls.append('verify')
            self.assertIn('--verify-model', command)
            self.assertIn('--status-file', command)
            self.assertNotIn('HF_TOKEN', kwargs['env'])
            self.assertNotIn('HUGGING_FACE_HUB_TOKEN', kwargs['env'])
            self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'], '1')
            return SimpleNamespace(returncode=0)
        args = ['worker', '--download', '--model-dir', str(self.root/'model'), '--status-file', str(self.root/'status.json')]
        with patch.dict('sys.modules', {'huggingface_hub':hub}), patch('sys.argv', args), \
                patch.dict('os.environ', {'HF_TOKEN':'hf_test', 'HUGGING_FACE_HUB_TOKEN':'other'}), \
                patch('diarize_worker.subprocess.run', side_effect=verify):
            self.assertEqual(diarize_worker.main(), 0)
        self.assertEqual(calls, ['token', 'access', 'download', 'verify'])

    def test_server_keeps_safe_worker_error_and_cleans_temporary_report(self):
        report_paths = []
        def launch(command, **kwargs):
            path = Path(command[command.index('--status-file') + 1])
            report_paths.append(path)
            def finish():
                path.write_text(json.dumps({'stage':'Ошибка', 'error':'Нет разрешения [access/403]'}), encoding='utf-8')
                return 1
            return SimpleNamespace(wait=finish, poll=lambda:1)
        preparation = diarization.ModelPreparation(self.root)
        with patch('diarization.readiness', return_value={'libraries_ready':True,'model_ready':False,'ready':False}), \
                patch('diarization.subprocess.Popen', side_effect=launch):
            preparation.start('hf_not_saved')
            preparation.thread.join(timeout=3)
            self.assertEqual(preparation.status()['error'], 'Нет разрешения [access/403]')
        self.assertFalse(report_paths[0].exists())
        preparation.close()

    def test_local_verification_scopes_allowlist_to_three_metadata_types(self):
        state = {'active': False}
        task = SimpleNamespace(Specifications=type('Specifications', (), {}),
                               Problem=type('Problem', (), {}), Resolution=type('Resolution', (), {}))
        @contextmanager
        def safe_globals(types):
            self.assertEqual(types, [task.Specifications, task.Problem, task.Resolution])
            state['active'] = True
            try:
                yield
            finally:
                state['active'] = False
        def load(path):
            self.assertTrue(state['active'])
            return SimpleNamespace(to=lambda device: self.assertEqual(device, 'cpu'))
        model = self.root/'model'
        model.mkdir()
        modules = {'pyannote.audio': SimpleNamespace(Pipeline=SimpleNamespace(from_pretrained=load)),
                   'pyannote.audio.core.task': task,
                   'torch': SimpleNamespace(serialization=SimpleNamespace(safe_globals=safe_globals), device=lambda name:name)}
        with patch.dict('sys.modules', modules), patch('sys.argv', ['worker', '--verify-model', '--model-dir', str(model)]):
            self.assertEqual(diarize_worker.main(), 0)
        self.assertFalse(state['active'])
        self.assertTrue((model/'.ready').is_file())


if __name__ == '__main__':
    unittest.main()
