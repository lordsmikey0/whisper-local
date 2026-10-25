import ctypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi.testclient import TestClient

from jobqueue import TaskQueue, TERMINAL
import web

FIXTURE = Path(__file__).resolve().parent / 'queue_fixture_worker.py'


def command(manager, job):
    return [sys.executable, '-X', 'utf8', str(FIXTURE), str(job.source), str(job.output),
            '--progress', str(job.folder / 'progress.json')]


def until(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.025)
    raise AssertionError('Queue operation did not finish in time')


class QueueTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        self.storage = tempfile.TemporaryDirectory(dir=root)
        self.lock = threading.Lock()
        self.manager = TaskQueue(Path(self.storage.name), self.lock)
        self.manager._command = lambda job: command(self.manager, job)

    def tearDown(self):
        self.manager.close()
        self.assertFalse(self.manager.thread.is_alive())
        self.storage.cleanup()

    def add(self, name='audio.mp3', content=b'fast', **kwargs):
        return self.manager.enqueue(name, 'transcribe', io.BytesIO(content), **kwargs)['id']

    def test_fifo_progress_failure_recovery_and_archive_names(self):
        first = self.add('запись.mp3')
        second = self.add('bad.mp3', b'fail')
        third = self.add('запись.mp3')
        until(lambda: self.manager.get(first).progress is not None)
        self.assertLess(self.manager.get(first).progress, 100)
        self.assertEqual(self.manager.get(third).state, 'queued')
        until(lambda: all(job['state'] in TERMINAL for job in self.manager.snapshots()))
        self.assertEqual([job['state'] for job in self.manager.snapshots()], ['done', 'error', 'done'])
        self.assertIn('broken test audio', self.manager.get(second).error)
        self.assertFalse(self.manager.get(first).source.exists())
        self.assertFalse(self.manager.get(second).folder.exists())
        archive = self.manager.archive()
        with zipfile.ZipFile(archive) as contents:
            self.assertEqual(contents.namelist(), ['запись.txt', 'запись (2).txt'])
        self.assertFalse(self.lock.locked())

    def test_cancel_running_kills_child_and_queue_continues(self):
        first = self.add(content=b'slow')
        queued = self.add('second.mp3')
        until(lambda: self.manager.get(first).processed > 0)
        info = json.loads((self.manager.get(first).folder / 'progress.json').read_text(encoding='utf-8'))
        child_pid = info['child_pid']
        self.manager.cancel(queued)
        self.manager.cancel(first)
        until(lambda: not self.manager.get(first).folder.exists())
        self.assertEqual(self.manager.get(first).state, 'cancelled')
        self.assertEqual(self.manager.get(queued).state, 'cancelled')
        if os.name == 'nt':
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, False, child_pid)
            if handle:
                code = ctypes.c_ulong()
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(handle)
                self.assertNotEqual(code.value, 259, 'Worker child is still running')
        next_job = self.add('next.mp3')
        until(lambda: self.manager.get(next_job).state == 'done')

    def test_cancel_before_upload_arrives_and_cancel_all(self):
        token = uuid.uuid4().hex
        self.manager.cancel(token)
        identity = self.add(identity=token)
        until(lambda: not self.manager.get(identity).folder.exists())
        self.assertEqual(self.manager.get(identity).state, 'cancelled')
        self.lock.acquire()
        try:
            ids = [self.add(), self.add()]
            self.manager.cancel_all()
            until(lambda: all(not self.manager.get(i).folder.exists() for i in ids))
            self.assertTrue(all(self.manager.get(i).state == 'cancelled' for i in ids))
        finally:
            self.lock.release()

    def test_shutdown_stops_running_job(self):
        identity = self.add(content=b'slow')
        until(lambda: self.manager.get(identity).processed > 0)
        self.manager.close()
        self.assertFalse(self.manager.thread.is_alive())
        self.assertFalse(self.manager.root.exists())


class QueueAPITests(unittest.TestCase):
    def test_upload_poll_result_download_archive_and_refresh(self):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder, \
                patch.object(web, 'HISTORY_ROOT', Path(folder) / 'history'), \
                patch.object(TaskQueue, '_command', command), TestClient(web.app) as client:
            response = client.post('/api/jobs', files={'file': ('речь.mp4', b'fast')}, data={'device': 'cpu'})
            self.assertEqual(response.status_code, 202, response.text)
            identity = response.json()['id']
            until(lambda: client.get('/api/jobs').json()['jobs'][0]['state'] in TERMINAL)
            self.assertEqual(client.get('/api/jobs').json()['jobs'][0]['state'], 'done', client.get('/api/jobs').text)
            self.assertIn('Готовая', client.get(f'/api/jobs/{identity}/result').json()['text'])
            self.assertEqual(client.get(f'/api/jobs/{identity}/download').status_code, 200)
            archive = client.get('/api/jobs/archive')
            self.assertEqual(archive.status_code, 200)
            self.assertEqual(zipfile.ZipFile(io.BytesIO(archive.content)).namelist(), ['речь.txt'])
            # A fresh client/page sees the same server queue.
            self.assertEqual(client.get('/api/jobs').json()['jobs'][0]['id'], identity)
            token = uuid.uuid4().hex
            self.assertEqual(client.post(f'/api/jobs/{token}/cancel').status_code, 200)
            cancelled = client.post('/api/jobs', files={'file': ('cancel.mp3', b'fast')}, data={'client_id': token})
            self.assertEqual(cancelled.json()['state'], 'cancelled')
            self.assertEqual(client.post('/api/jobs', files={'file': ('a.mp3', b'')}).status_code, 400)
            self.assertEqual(client.get('/api/jobs/missing/download').status_code, 404)
