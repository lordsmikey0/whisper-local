"""Output profiles shared by the queue, history, API and converter."""
import math

FORMATS = {
    "mp3": {"label": "MP3", "kind": "audio", "codec": "libmp3lame", "mime": "audio/mpeg"},
    "wav": {"label": "WAV · без потерь", "kind": "audio", "codec": "pcm_s16le", "mime": "audio/wav", "lossless": True},
    "flac": {"label": "FLAC · без потерь", "kind": "audio", "codec": "flac", "mime": "audio/flac", "lossless": True},
    "m4a": {"label": "M4A / AAC", "kind": "audio", "codec": "aac", "mime": "audio/mp4"},
    "aac": {"label": "AAC", "kind": "audio", "codec": "aac", "mime": "audio/aac"},
    "ogg": {"label": "OGG / Vorbis", "kind": "audio", "codec": "libvorbis", "mime": "audio/ogg"},
    "opus": {"label": "Opus", "kind": "audio", "codec": "libopus", "mime": "audio/ogg"},
    "mp4": {"label": "MP4 · H.264", "kind": "video", "codec": "libx264", "mime": "video/mp4"},
    "mkv": {"label": "MKV · H.264", "kind": "video", "codec": "libx264", "mime": "video/x-matroska"},
    "mov": {"label": "MOV · H.264", "kind": "video", "codec": "libx264", "mime": "video/quicktime"},
    "webm": {"label": "WebM · VP9", "kind": "video", "codec": "libvpx-vp9", "mime": "video/webm"},
}


def normalize_options(options):
    result = dict(options)
    fmt = result.get("output_format", "mp3")
    if fmt not in FORMATS:
        raise ValueError("Выберите поддерживаемый выходной формат.")
    result["output_format"] = fmt
    for key, default, allowed in (
        ("quality", "balanced", ("compact", "balanced", "high")),
        ("resolution", "source", ("source", "1080", "720", "480")),
        ("fps", "source", ("source", "24", "25", "30", "60")),
    ):
        result[key] = str(result.get(key, default))
        if result[key] not in allowed:
            raise ValueError(f"Недопустимое значение параметра {key}.")
    for key, default in (("trim_start", 0), ("trim_end", None)):
        value = result.get(key, default)
        if value is not None:
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError("Время обрезки должно быть конечным числом от нуля.")
        result[key] = value
    if result["trim_start"] is None:
        result["trim_start"] = 0
    if result["trim_end"] is not None and result["trim_end"] <= result["trim_start"]:
        raise ValueError("Конец отрезка должен быть позже начала.")
    if not isinstance(result.get("mute", False), bool):
        raise ValueError("Параметр удаления звука должен быть логическим.")
    result["mute"] = result.get("mute", False)
    if FORMATS[fmt]["kind"] == "audio" and result["mute"]:
        raise ValueError("Удалить звук можно только при сохранении видео.")
    return result


def result_format(mode, options):
    return options.get("output_format", "mp3") if mode == "convert" else "txt"


def result_mime(suffix):
    return FORMATS.get(suffix.lstrip("."), {}).get("mime", "text/plain; charset=utf-8")
