from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import web


class WebTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(web.app)

    def test_page_and_assets(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 200)
        self.assertEqual(self.client.get('/static/style.css').status_code, 200)

    def test_upload_result_and_cleanup(self):
        def recognize(command, **kwargs):
            source = Path(command[4])
            self.assertEqual(source.read_bytes(), b'audio data')
            Path(command[command.index('-o') + 1]).write_text('Привет!\n', encoding='utf-8-sig')
            self.assertIn('--offline', command)
            return subprocess.CompletedProcess(command, 0, 'OK', '')
        with patch('web.subprocess.run', side_effect=recognize):
            response = self.client.post('/api/transcribe', files={'file': ('запись.mp3', b'audio data', 'audio/mpeg')})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['text'], 'Привет!\n')
        self.assertEqual(response.json()['filename'], 'запись.txt')
        self.assertFalse(web.BUSY.locked())
        self.assertEqual(list(web.WORK.iterdir()), [])

    def test_failed_inference_unlocks_and_cleans_upload(self):
        with patch('web.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'Ошибка декодирования')):
            response = self.client.post('/api/transcribe', files={'file': ('bad.mp3', b'not audio')})
        self.assertEqual(response.status_code, 422)
        self.assertIn('Ошибка декодирования', response.json()['detail'])
        self.assertFalse(web.BUSY.locked())
        self.assertEqual(list(web.WORK.iterdir()), [])

    def test_busy_and_invalid_requests(self):
        web.BUSY.acquire()
        try:
            response = self.client.post('/api/transcribe', files={'file': ('a.mp3', b'a')})
            self.assertEqual(response.status_code, 409)
            self.assertTrue(self.client.get('/api/health').json()['busy'])
        finally:
            web.BUSY.release()
        self.assertEqual(self.client.post('/api/transcribe', files={'file': ('a.mp3', b'')}).status_code, 400)
        self.assertEqual(self.client.post('/api/transcribe', files={'file': ('a.mp3', b'a')},
                                          data={'model': '../../bad'}).status_code, 422)


if __name__ == '__main__':
    unittest.main()
