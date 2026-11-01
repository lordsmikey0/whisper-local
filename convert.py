"""Extract the first audio track to MP3 using the bundled PyAV codecs.

The compatibility helper below is retained for older callers. The universal
converter uses an isolated FFmpeg process for audio, video, trim and resize.
"""

from fractions import Fraction
from pathlib import Path

import av
import os
import subprocess
import tempfile

from media_formats import FORMATS, normalize_options


def ffmpeg_executable():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as error:
        raise RuntimeError("Не найден комплект FFmpeg. Запустите setup.cmd ещё раз.") from error


def probe_media(source):
    try:
        with av.open(str(source), options={"protocol_whitelist": "file,pipe,crypto,data"}) as container:
            duration = float(container.duration / av.time_base) if container.duration is not None else None
            audio = container.streams.audio[0].codec_context if container.streams.audio else None
            fmt = audio.format if audio else None
            def stream_duration(streams):
                if streams and streams[0].duration is not None and streams[0].time_base:
                    return float(streams[0].duration * streams[0].time_base)
                return duration
            return {"audio": bool(container.streams.audio), "video": bool(container.streams.video), "duration": duration,
                    "audio_duration": stream_duration(container.streams.audio), "video_duration": stream_duration(container.streams.video),
                    "rate": audio.sample_rate if audio else 44100,
                    "channels": len(audio.layout.channels) if audio else 2,
                    "bits": fmt.bits if fmt else 16, "floating": bool(fmt and fmt.name.startswith(('flt', 'dbl')))}
    except av.FFmpegError as error:
        raise ValueError("Не удалось прочитать запись. Проверьте, что файл не повреждён и содержит аудио или видео.") from error


def convert_media(source, output, options=None, on_progress=None):
    options = normalize_options(options or {})
    profile = FORMATS[options["output_format"]]
    video = profile["kind"] == "video"
    media = probe_media(source)
    if video and not media["video"]:
        raise ValueError("В исходном файле нет видеодорожки. Выберите аудиоформат.")
    if not video and not media["audio"]:
        raise ValueError("В исходном файле нет аудиодорожки.")
    start, end = options["trim_start"], options["trim_end"]
    duration = media["video_duration"] if video else media["audio_duration"]
    if duration is not None and start >= duration:
        raise ValueError("Начало отрезка находится за пределами записи.")
    if end is not None:
        end = min(end, duration) if duration is not None else end
    total = (end if end is not None else duration)
    total = max(0, total - start) if total is not None else None
    command = [ffmpeg_executable(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-protocol_whitelist", "file,pipe,crypto,data"]
    if start:
        command += ["-ss", str(start)]
    command += ["-i", str(source)]
    if total is not None:
        command += ["-t", str(total)]
    command += ["-map_metadata", "-1", "-map_chapters", "-1"]
    quality = options["quality"]
    if video:
        command += ["-map", "0:v:0", "-c:v", profile["codec"]]
        if options["output_format"] == "webm":
            command += ["-crf", str({"compact": 38, "balanced": 32, "high": 26}[quality]),
                        "-b:v", "0", "-deadline", "good", "-cpu-used", "4"]
        else:
            command += ["-preset", "fast", "-crf", str({"compact": 28, "balanced": 23, "high": 18}[quality])]
        if options["resolution"] == "source":
            scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        else:
            width, height = {"1080": (1920, 1080), "720": (1280, 720), "480": (854, 480)}[options["resolution"]]
            scale = f"scale=w='min(iw,{width})':h='min(ih,{height})':force_original_aspect_ratio=decrease:force_divisible_by=2"
        command += ["-vf", scale, "-pix_fmt", "yuv420p"]
        if options["fps"] != "source":
            command += ["-r", options["fps"]]
        if options["mute"] or not media["audio"]:
            command += ["-an"]
        else:
            command += ["-map", "0:a:0", "-c:a", "libopus" if options["output_format"] == "webm" else "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000"]
    else:
        fmt = options["output_format"]
        lossless = fmt in ("wav", "flac")
        codec = profile["codec"]
        if fmt == "wav":
            codec = "pcm_f32le" if media["floating"] else "pcm_s32le" if media["bits"] > 16 else "pcm_s16le"
        command += ["-map", "0:a:0", "-vn", "-c:a", codec, "-ac", str(media["channels"] or 2) if lossless else "2"]
        command += ["-ar", str(media["rate"] or 44100) if lossless else "48000" if fmt == "opus" else "44100"]
        if fmt == "ogg":
            command += ["-q:a", str({"compact": 3, "balanced": 5, "high": 7}[quality])]
        elif fmt == "flac":
            command += ["-compression_level", "5", "-sample_fmt", "s32" if media["bits"] > 16 else "s16"]
        elif fmt == "opus":
            command += ["-b:a", {"compact": "64k", "balanced": "128k", "high": "192k"}[quality]]
        elif fmt != "wav":
            command += ["-b:a", {"compact": "128k", "balanced": "192k", "high": "256k"}[quality]]
    if options["output_format"] in ("mp4", "mov", "m4a"):
        command += ["-movflags", "+faststart"]
    command += ["-progress", "pipe:1", "-nostats", str(output)]
    stage = "Конвертация видео" if video else "Конвертация аудио"
    if on_progress:
        on_progress(stage=stage, progress=0 if total else None, processed=0, total=total)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    # A file-backed log avoids deadlocking on a full stderr pipe.
    with tempfile.TemporaryFile(dir=Path(output).parent) as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=log, text=True, encoding="utf-8", errors="replace", **flags)
        try:
            for line in process.stdout:
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms") and on_progress:
                    try:
                        processed = max(0, int(value) / 1_000_000)
                    except ValueError:
                        continue
                    on_progress(stage=stage, processed=min(processed, total) if total else processed,
                                total=total, progress=min(99, processed / total * 100) if total else None)
            code = process.wait()
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            process.stdout.close()
        if code:
            log.seek(0)
            detail = log.read().decode("utf-8", errors="replace")[-1800:].strip()
            raise ValueError("Не удалось конвертировать файл. " + detail)
    if not Path(output).is_file() or not Path(output).stat().st_size:
        raise ValueError("Конвертер не создал результат.")
    if on_progress:
        on_progress(stage="Сохранение результата", progress=99, processed=total or 0, total=total)


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
