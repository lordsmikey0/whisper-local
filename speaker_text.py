"""Speaker alignment and rendering, independent of ML dependencies."""
import math


def align_words(units, turns):
    turns = sorted((t for t in turns if t['end'] > t['start']), key=lambda t: t['start'])
    groups, labels, index = [], {}, 0
    for unit in sorted(units, key=lambda u: u['start']):
        if not unit['text'].strip():
            continue
        start, end = float(unit['start']), max(float(unit['end']), float(unit['start']) + .001)
        while index < len(turns) and turns[index]['end'] <= start:
            index += 1
        best, overlap, cursor = None, 0, index
        while cursor < len(turns) and turns[cursor]['start'] < end:
            turn = turns[cursor]
            current = max(0, min(end, turn['end']) - max(start, turn['start']))
            if current > overlap:
                best, overlap = turn['speaker'], current
            cursor += 1
        if best is None:
            nearby = turns[max(0, index - 1):index + 1]
            if nearby:
                closest = min(nearby, key=lambda t: max(t['start'] - end, start - t['end'], 0))
                if max(closest['start'] - end, start - closest['end'], 0) <= .35:
                    best = closest['speaker']
        if best is None:
            speaker = 'unknown'
        else:
            if best not in labels:
                labels[best] = f'speaker_{len(labels) + 1}'
            speaker = labels[best]
        text = unit['text']
        if not text.strip():
            continue
        if groups and groups[-1]['speaker'] == speaker and start - groups[-1]['end'] <= 1.5:
            groups[-1]['text'] += text
            groups[-1]['end'] = max(groups[-1]['end'], end)
        else:
            groups.append({'start': start, 'end': end, 'speaker': speaker, 'text': text})
    for group in groups:
        group['text'] = group['text'].strip()
    speakers = [{'id': value, 'label': f'Говорящий {number}'} for number, value in enumerate(labels.values(), 1)]
    if any(group['speaker'] == 'unknown' for group in groups):
        speakers.append({'id': 'unknown', 'label': 'Не определён'})
    return {'version': 1, 'speakers': speakers, 'names': {}, 'segments': groups}


def validate_data(data):
    if not isinstance(data, dict) or not isinstance(data.get('speakers'), list) or not isinstance(data.get('segments'), list):
        raise ValueError('Неверный формат данных говорящих.')
    ids = {speaker['id'] for speaker in data['speakers']}
    if len(ids) != len(data['speakers']) or len(ids) > 100:
        raise ValueError('Неверный список говорящих.')
    for segment in data['segments']:
        if segment['speaker'] not in ids or not isinstance(segment['text'], str):
            raise ValueError('Неверная реплика.')
        if not all(math.isfinite(float(segment[key])) and float(segment[key]) >= 0 for key in ('start', 'end')):
            raise ValueError('Неверные временные метки.')
    data['names'] = validate_names(data, data.get('names', {}))
    return data


def validate_names(data, names):
    if not isinstance(names, dict):
        raise ValueError('Нужна таблица имён.')
    ids = {speaker['id'] for speaker in data['speakers']}
    if set(names) - ids:
        raise ValueError('В таблице есть неизвестный говорящий.')
    cleaned = {}
    for identity, value in names.items():
        if not isinstance(value, str) or len(value) > 80 or any(ord(char) < 32 for char in value):
            raise ValueError('Имя должно быть одной строкой длиной до 80 символов.')
        if value.strip():
            cleaned[identity] = value.strip()
    return cleaned


def render_transcript(data):
    labels = {speaker['id']: speaker['label'] for speaker in data['speakers']}
    names = data.get('names', {})
    lines = []
    for segment in data['segments']:
        seconds = max(0, int(segment['start']))
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        stamp = f'{hours:02d}:{minutes:02d}:{seconds:02d}' if hours else f'{minutes:02d}:{seconds:02d}'
        label = names.get(segment['speaker']) or labels[segment['speaker']]
        lines.append(f"[{stamp}] {label}: {segment['text']}")
    return '\n'.join(lines) + ('\n' if lines else '')
