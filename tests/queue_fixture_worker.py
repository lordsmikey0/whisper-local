"""Tiny deterministic subprocess used to test queue isolation and cancellation."""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('source', type=Path)
parser.add_argument('output', type=Path)
parser.add_argument('--progress', type=Path)
args, _ = parser.parse_known_args()
kind = args.source.read_text(encoding='utf-8')
if kind == 'fail':
    print('Ошибка: broken test audio', flush=True)
    raise SystemExit(1)
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']) if kind == 'slow' else None
for progress in range(10, 100, 10):
    temp = args.progress.with_suffix('.tmp')
    temp.write_text(json.dumps({'stage': 'Распознавание', 'progress': progress,
                               'processed': progress / 10, 'total': 10,
                               'child_pid': child.pid if child else None}), encoding='utf-8')
    try:
        temp.replace(args.progress)
    except PermissionError:
        pass
    time.sleep(5 if kind == 'slow' else 0.04)
args.output.write_text('Готовая стенограмма\n', encoding='utf-8-sig')
if child:
    child.terminate()
    child.wait()
