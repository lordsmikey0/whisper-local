from fractions import Fraction
from pathlib import Path
import tempfile
import unittest

import av
from fastapi.testclient import TestClient
import numpy as np

import web


def make_mp4(path, with_audio=True):
    """One second of actual MP4 video, optionally with 48 kHz mono AAC."""
    with av.open(str(path), 'w') as target:
        video = target.add_stream('mpeg4', rate=10)
        video.width = video.height = 64
        video.pix_fmt = 'yuv420p'
        if with_audio:
            audio = target.add_stream('aac', rate=48000)
            audio.layout = 'mono'
        for index in range(10):
            frame = av.VideoFrame.from_ndarray(np.zeros((64, 64, 3), dtype=np.uint8), format='rgb24')
            frame.pts = index
            for packet in video.encode(frame):
                target.mux(packet)
        for packet in video.encode():
            target.mux(packet)
        if with_audio:
            samples = (0.2 * np.sin(2 * np.pi * 440 * np.arange(48000) / 48000)).astype(np.float32)
            frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format='fltp', layout='mono')
            frame.sample_rate = 48000
            frame.time_base = Fraction(1, 48000)
            frame.pts = 0
            for packet in audio.encode(frame):
                target.mux(packet)
            for packet in audio.encode():
                target.mux(packet)


class ConversionTests(unittest.TestCase):
    def test_real_mp4_to_mp3_and_response_cleanup(self):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            source = Path(folder) / 'видео.MP4'
            make_mp4(source)
            response = TestClient(web.app).post('/api/convert', files={
                'file': (source.name, source.read_bytes(), 'video/mp4')})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['content-type'], 'audio/mpeg')
            self.assertIn('.mp3', response.headers['content-disposition'])
            output = Path(folder) / 'result.mp3'
            output.write_bytes(response.content)
            with av.open(str(output)) as audio:
                self.assertEqual(len(audio.streams.audio), 1)
                self.assertEqual(len(audio.streams.video), 0)
                stream = audio.streams.audio[0]
                self.assertEqual(stream.rate, 44100)
                frames = list(audio.decode(audio=0))
                duration = sum(frame.samples for frame in frames) / stream.rate
                self.assertAlmostEqual(duration, 1, delta=0.12)
                self.assertGreater(np.max(np.abs(frames[len(frames) // 2].to_ndarray())), 0.05)
            self.assertEqual(list(web.WORK.iterdir()), [])
            self.assertFalse(web.BUSY.locked())

    def test_video_without_audio_has_clear_error(self):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            source = Path(folder) / 'silent.mp4'
            make_mp4(source, with_audio=False)
            response = TestClient(web.app).post('/api/convert', files={'file': (source.name, source.read_bytes())})
            self.assertEqual(response.status_code, 422)
            self.assertIn('нет аудиодорожки', response.json()['detail'])
            self.assertEqual(list(web.WORK.iterdir()), [])
            self.assertFalse(web.BUSY.locked())

    def test_invalid_uploads_are_cleaned(self):
        client = TestClient(web.app)
        for filename, data, status in [('a.mp4', b'broken', 422), ('a.mp3', b'wrong type', 400), ('a.mp4', b'', 400)]:
            with self.subTest(filename=filename, data=data):
                response = client.post('/api/convert', files={'file': (filename, data)})
                self.assertEqual(response.status_code, status)
                self.assertFalse(web.BUSY.locked())
                self.assertEqual(list(web.WORK.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
