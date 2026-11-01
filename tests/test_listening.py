import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
from history import HistoryStore
import transcribe
import web


class ListeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / 'data')
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, keep=True):
        output = self.root / 'result.txt'
        output.write_text('Первая реплика.\nВторая реплика.\n', encoding='utf-8-sig')
        output.with_suffix('.segments.json').write_text(json.dumps([
            {'start': 1.25, 'end': 3, 'text': 'Первая реплика.'},
            {'start': 4, 'end': 6, 'text': 'Вторая реплика.'},
        ]), encoding='utf-8')
        output.with_suffix('.playback.mp3').write_bytes(b'0123456789abcdef')
        return SimpleNamespace(id=uuid.uuid4().hex, filename='беседа.mp4', mode='transcribe',
                               output=output, elapsed=2, options={'keep_audio': keep})

    def test_audio_and_segments_survive_restart_and_delete_together(self):
        store = HistoryStore(self.root / 'history')
        job = self.job()
        store.save(job)
        store = HistoryStore(self.root / 'history')
        self.assertEqual(store.transcript(job.id)['segments'][1]['start'], 4)
        self.assertTrue(store.get(job.id)['audio_available'])
        self.assertEqual(store.get(job.id)['audio_size'], 16)
        store.delete(job.id)
        self.assertFalse(store.audio_path(job.id).exists())
        self.assertTrue(job.output.with_suffix('.playback.mp3').exists())
        with store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM playback_data').fetchone()[0], 0)

    def test_opt_out_and_old_results_have_no_saved_audio(self):
        store = HistoryStore(self.root / 'history')
        job = self.job(False)
        store.save(job)
        self.assertFalse(store.transcript(job.id)['audio_available'])
        self.assertEqual(store.transcript(job.id)['segments'], [])
        self.assertEqual(list(store.audio.iterdir()), [])

    def test_speaker_renaming_preserves_playback_and_returns_new_names(self):
        store = HistoryStore(self.root / 'history')
        job = self.job()
        data = {'speakers': [{'id': 'speaker_1', 'label': 'Говорящий 1'}], 'names': {},
                'segments': [{'speaker': 'speaker_1', 'start': 1.25, 'end': 3, 'text': 'Привет.'}]}
        job.output.with_suffix('.speakers.json').write_text(json.dumps(data), encoding='utf-8')
        store.save(job)
        renamed = store.rename_speakers(job.id, {'speaker_1': 'Джон Доу'})
        self.assertIn('Джон Доу', renamed['text'])
        self.assertTrue(renamed['audio_available'])
        self.assertEqual(renamed['segments'][0]['start'], 1.25)
        self.assertEqual(HistoryStore(store.root).transcript(job.id)['names']['speaker_1'], 'Джон Доу')

    def test_failed_audio_copy_is_not_published(self):
        store = HistoryStore(self.root / 'history')
        job = self.job()
        job.output.with_suffix('.playback.mp3').unlink()
        with self.assertRaises(FileNotFoundError):
            store.save(job)
        self.assertEqual(store.list(), [])
        self.assertEqual(list(store.files.iterdir()), [])
        self.assertEqual(list(store.audio.iterdir()), [])

    def test_audio_endpoint_supports_byte_ranges_and_missing_files(self):
        with patch.object(web, 'HISTORY_ROOT', self.root / 'history'), TestClient(web.app) as client:
            job = self.job()
            store = client.app.state.history
            store.save(job)
            response = client.get(f'/api/history/{job.id}/audio', headers={'Range': 'bytes=4-9'})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.content, b'456789')
            self.assertEqual(response.headers['content-range'], 'bytes 4-9/16')
            self.assertEqual(response.headers['content-type'], 'audio/mpeg')
            self.assertEqual(client.get(f'/api/history/{job.id}/result').json()['segments'][0]['start'], 1.25)
            total = client.get('/api/history').json()['bytes']
            self.assertEqual(total, job.output.stat().st_size + 16)
            store.audio_path(job.id).unlink()
            self.assertFalse(client.get(f'/api/history/{job.id}/result').json()['audio_available'])
            self.assertEqual(client.get(f'/api/history/{job.id}/audio').status_code, 404)
            self.assertEqual(client.get(f'/api/history/{uuid.uuid4().hex}/audio').status_code, 404)

    def test_plain_whisper_captures_timestamps_without_word_alignment(self):
        captured = {}
        def recognize(*args, **kwargs):
            captured.update(kwargs)
            return iter([SimpleNamespace(start=1.25, end=3, text=' Привет ')]), SimpleNamespace(language='ru', duration=4)
        output = self.root / 'asr.txt'
        transcribe.transcribe(SimpleNamespace(transcribe=recognize),
                              SimpleNamespace(audio='x', language='auto', overwrite=False, keep_audio=True), output)
        self.assertFalse(captured['word_timestamps'])
        self.assertEqual(json.loads(output.with_suffix('.segments.json').read_text(encoding='utf-8')),
                         [{'start': 1.25, 'end': 3.0, 'text': 'Привет'}])


if __name__ == '__main__':
    unittest.main()
