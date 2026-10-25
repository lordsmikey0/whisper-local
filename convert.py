"""Extract the first audio track to MP3 using the bundled PyAV codecs."""

from fractions import Fraction
from pathlib import Path

import av


def mp4_to_mp3(source: Path, output: Path, on_progress=None):
    with av.open(str(source)) as video:
        if not video.streams.audio:
            raise ValueError("В MP4 нет аудиодорожки.")
        audio = video.streams.audio[0]
        duration = float(audio.duration * audio.time_base) if audio.duration is not None and audio.time_base else None
        if on_progress:
            on_progress(stage="Извлечение аудио", progress=0 if duration else None, processed=0, total=duration)
        with av.open(str(output), mode="w", format="mp3") as target:
            stream = target.add_stream("libmp3lame", rate=44100)
            stream.layout = "stereo"
            stream.bit_rate = 192000
            stream.codec_context.format = "fltp"
            resampler = av.AudioResampler(format="fltp", layout="stereo", rate=44100)
            samples = 0

            def encode(frames):
                nonlocal samples
                for frame in frames:
                    # MP4 can start at a nonzero timestamp. MP3 should start at zero.
                    frame.pts = samples
                    frame.time_base = Fraction(1, 44100)
                    samples += frame.samples
                    for packet in stream.encode(frame):
                        target.mux(packet)
                    if on_progress:
                        processed = samples / 44100
                        on_progress(stage="Извлечение аудио", processed=processed, total=duration,
                                    progress=min(99, processed / duration * 100) if duration else None)

            for frame in video.decode(video.streams.audio[0]):
                encode(resampler.resample(frame))
            encode(resampler.resample(None))
            for packet in stream.encode(None):
                target.mux(packet)
            if not samples:
                raise ValueError("Аудиодорожка MP4 пустая.")
