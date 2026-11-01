import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import av
from fastapi.testclient import TestClient

from convert import convert_media
from history import HistoryStore
from jobqueue import TERMINAL
from media_formats import FORMATS, normalize_options
from test_convert import make_mp4
from test_queue import until
import web


class UniversalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        cls.storage = tempfile.TemporaryDirectory(dir=root)
        cls.root = Path(cls.storage.name)
        cls.source = cls.root / 'видео с пробелом.mp4'
        make_mp4(cls.source)

    @classmethod
    def tearDownClass(cls):
        cls.storage.cleanup()

    def test_all_output_profiles_are_real_decodable_media(self):
        for fmt, profile in FORMATS.items():
            with self.subTest(format=fmt):
                output = self.root / ('result.' + fmt)
                events = []
                convert_media(self.source, output, {'output_format': fmt}, lambda **data: events.append(data))
                with av.open(str(output)) as media:
                    self.assertEqual(bool(media.streams.video), profile['kind'] == 'video')
                    self.assertTrue(media.streams.audio)
                    frames = list(media.decode(audio=0))
                    duration = sum(frame.samples for frame in frames) / frames[0].sample_rate
                    self.assertAlmostEqual(duration, 1, delta=.18)
                if profile['kind'] == 'video':
                    with av.open(str(output)) as media:
                        self.assertTrue(list(media.decode(video=0)))
                self.assertTrue(any((e.get('processed') or 0) > 0 for e in events))
                self.assertTrue(all(e.get('progress') is None or 0 <= e['progress'] <= 99 for e in events))

    def test_audio_inputs_trim_and_lossless_rate(self):
        wav = self.root / 'source.wav'
        convert_media(self.source, wav, {'output_format': 'wav'})
        flac = self.root / 'trimmed.flac'
        convert_media(wav, flac, {'output_format': 'flac', 'trim_start': .15, 'trim_end': .75})
        with av.open(str(flac)) as media:
            self.assertEqual(media.streams.audio[0].rate, 48000)
            frames = list(media.decode(audio=0))
            self.assertAlmostEqual(sum(f.samples for f in frames) / 48000, .6, delta=.04)
        mp3 = self.root / 'from-audio.mp3'
        convert_media(flac, mp3)
        with av.open(str(mp3)) as media:
            self.assertTrue(list(media.decode(audio=0)))

    def test_resize_fps_mute_and_no_upscale(self):
        large = self.root / 'large.mp4'
        make_mp4(large, width=1280, height=720)
        output = self.root / 'small.mp4'
        convert_media(large, output, {'output_format': 'mp4', 'resolution': '480', 'fps': '24',
                                      'mute': True, 'trim_start': .1, 'trim_end': .8})
        with av.open(str(output)) as media:
            self.assertFalse(media.streams.audio)
            stream = media.streams.video[0]
            self.assertLessEqual(stream.height, 480)
            self.assertLessEqual(stream.width, 854)
            self.assertEqual(float(stream.average_rate), 24)
            self.assertGreater(len(list(media.decode(video=0))), 10)
        convert_media(self.source, output, {'output_format': 'mp4', 'resolution': '1080'})
        with av.open(str(output)) as media:
            self.assertEqual(media.streams.video[0].width, 64)

    def test_invalid_combinations_and_intervals(self):
        for options in ({'output_format': '../../file'}, {'trim_start': float('nan')},
                        {'trim_end': float('inf')}, {'trim_start': 2, 'trim_end': 1},
                        {'mute': True, 'output_format': 'mp3'}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                normalize_options(options)
        silent = self.root / 'silent.mp4'
        make_mp4(silent, with_audio=False)
        with self.assertRaisesRegex(ValueError, 'нет аудиодорожки'):
            convert_media(silent, self.root / 'silent.wav', {'output_format': 'wav'})
        audio = self.root / 'audio.wav'
        convert_media(self.source, audio, {'output_format': 'wav'})
        with self.assertRaisesRegex(ValueError, 'нет видеодорожки'):
            convert_media(audio, self.root / 'invalid.mp4', {'output_format': 'mp4'})
        with self.assertRaisesRegex(ValueError, 'за пределами'):
            convert_media(audio, self.root / 'invalid.mp3', {'trim_start': 5})

    def test_real_queue_history_and_mime_survive_restart(self):
        history_root = self.root / 'history'
        with patch.object(web, 'HISTORY_ROOT', history_root):
            with TestClient(web.app) as client:
                identities = []
                for fmt in ('flac', 'webm'):
                    response = client.post('/api/jobs', files={'file': (self.source.name, self.source.read_bytes())},
                                           data={'mode': 'convert', 'output_format': fmt})
                    self.assertEqual(response.status_code, 202, response.text)
                    identities.append(response.json()['id'])
                until(lambda: all(j['state'] in TERMINAL for j in client.get('/api/jobs').json()['jobs']), timeout=25)
                items = client.get('/api/jobs').json()['jobs']
                self.assertEqual([j['state'] for j in items], ['done', 'done'], items)
                self.assertEqual([j['output_name'] for j in items], ['видео с пробелом.flac', 'видео с пробелом.webm'])
            with TestClient(web.app) as restarted:
                history = restarted.get('/api/history').json()['items']
                self.assertEqual({h['output_format'] for h in history}, {'flac', 'webm'})
                for identity, mime in zip(identities, ('audio/flac', 'video/webm')):
                    response = restarted.get(f'/api/history/{identity}/download')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers['content-type'], mime)
            # Old MP3 entries without output_format still resolve to the original path.
            store = HistoryStore(history_root)
            old = {'id': 'a' * 32, 'mode': 'convert', 'options': '{}'}
            self.assertEqual(store.path(old).suffix, '.mp3')
