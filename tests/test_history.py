import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi.testclient import TestClient

from history import HistoryStore
from jobqueue import TaskQueue, TERMINAL
from test_queue import command, until
import web


class HistoryTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_persistence_same_names_and_safe_deletion(self):
        original = self.root / 'original.txt'
        original.write_text('Привет, мир!', encoding='utf-8-sig')
        store = HistoryStore(self.root / 'history')
        identities = [uuid.uuid4().hex, uuid.uuid4().hex]
        for identity in identities:
            store.save(SimpleNamespace(id=identity, filename='одинаковое имя.mp3', mode='transcribe',
                                       output=original, elapsed=2.5, options={'model': 'small'}))
        restored = HistoryStore(self.root / 'history')
        self.assertEqual(len(restored.list()), 2)
        for identity in identities:
            record = restored.get(identity)
            self.assertEqual(record['options']['model'], 'small')
            self.assertEqual(record['output_name'], 'одинаковое имя.txt')
            self.assertTrue(record['available'])
            self.assertEqual(restored.path(record).read_bytes(), original.read_bytes())
        removed_path = restored.path(restored.get(identities[0]))
        restored.delete(identities[0])
        self.assertFalse(removed_path.exists())
        self.assertTrue(original.exists())
        self.assertEqual(len(HistoryStore(self.root / 'history').list()), 1)

    def test_failed_copy_is_not_published_and_missing_file_can_be_removed(self):
        original = self.root / 'result.mp3'
        original.write_bytes(b'ID3example')
        store = HistoryStore(self.root / 'history')
        job = SimpleNamespace(id=uuid.uuid4().hex, filename='видео.mp4', mode='convert',
                              output=original, elapsed=1, options={})
        with patch('history.shutil.copyfile', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                store.save(job)
        self.assertEqual(store.list(), [])
        self.assertEqual(list(store.files.iterdir()), [])
        path = store.save(job)
        with patch.object(Path, 'unlink', side_effect=PermissionError('file in use')):
            with self.assertRaises(PermissionError):
                store.delete(job.id)
        self.assertTrue(store.get(job.id)['available'])
        path.unlink()
        self.assertFalse(store.get(job.id)['available'])
        store.delete(job.id)
        self.assertEqual(store.list(), [])

    def test_api_survives_server_restart_and_deletion_updates_queue(self):
        with patch.object(web, 'HISTORY_ROOT', self.root / 'history'), patch.object(TaskQueue, '_command', command):
            with TestClient(web.app) as client:
                ids = []
                for mode in ('transcribe', 'convert'):
                    response = client.post('/api/jobs', files={'file': ('запись.mp4', b'fast')}, data={'mode': mode})
                    self.assertEqual(response.status_code, 202)
                    ids.append(response.json()['id'])
                until(lambda: all(j['state'] in TERMINAL for j in client.get('/api/jobs').json()['jobs']))
                self.assertEqual([j['state'] for j in client.get('/api/jobs').json()['jobs']], ['done', 'done'])
                self.assertEqual(client.get('/api/history').json()['count'], 2)
                session_root = client.app.state.jobs.root
                # Deleting a current result removes the queue's corresponding download entry.
                self.assertEqual(client.delete(f'/api/history/{ids[1]}').status_code, 200)
                self.assertEqual(len(client.get('/api/jobs').json()['jobs']), 1)
                archive = client.get('/api/jobs/archive')
                self.assertEqual(zipfile.ZipFile(io.BytesIO(archive.content)).namelist(), ['запись.txt'])
            self.assertFalse(session_root.exists())
            with TestClient(web.app) as restarted:
                self.assertEqual(restarted.get('/api/jobs').json()['jobs'], [])
                history = restarted.get('/api/history').json()
                self.assertEqual(history['count'], 1)
                self.assertTrue(history['items'][0]['available'])
                self.assertIn('Готовая', restarted.get(f'/api/history/{ids[0]}/result').json()['text'])
                download = restarted.get(f'/api/history/{ids[0]}/download')
                self.assertEqual(download.status_code, 200)
                self.assertTrue(download.content.startswith(b'\xef\xbb\xbf'))
                self.assertEqual(restarted.delete(f'/api/history/{ids[0]}').status_code, 200)
                self.assertEqual(restarted.get(f'/api/history/{ids[0]}/download').status_code, 404)
                self.assertEqual(restarted.get('/api/history').json()['count'], 0)
