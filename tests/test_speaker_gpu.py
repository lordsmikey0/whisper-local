import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import diarization
import job_worker
from diarize_worker import choose_device
from speaker_resources import configure_gpu, GIB, limit_ram


class SpeakerGPUTests(unittest.TestCase):
    def setUp(self):
        (Path(__file__).parent/'data').mkdir(exist_ok=True)

    def test_auto_prefers_cuda_and_explicit_cuda_never_silently_uses_cpu(self):
        torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: True))
        self.assertEqual(choose_device(torch, 'auto'), 'cuda')
        self.assertEqual(choose_device(torch, 'cpu'), 'cpu')
        torch.cuda.is_available = lambda: False
        self.assertEqual(choose_device(torch, 'auto'), 'cpu')
        with self.assertRaisesRegex(RuntimeError, 'CUDA'):
            choose_device(torch, 'cuda')

    def test_allocator_budget_accounts_for_free_memory_and_context(self):
        recorded = []
        torch = SimpleNamespace(cuda=SimpleNamespace(
            get_device_properties=lambda i: SimpleNamespace(total_memory=4*GIB),
            mem_get_info=lambda i: (3*GIB, 4*GIB),
            set_per_process_memory_fraction=lambda fraction, **kw: recorded.append(fraction)),
            backends=SimpleNamespace(cudnn=SimpleNamespace(benchmark=True)))
        self.assertEqual(configure_gpu(torch), int(2.75*GIB))
        self.assertEqual(recorded, [2.75/4])
        self.assertFalse(torch.backends.cudnn.benchmark)
        for limit in (0, -1, 5):
            with self.assertRaises(ValueError):
                configure_gpu(torch, limit)

    def run_retries(self, requested, failures):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent/'data') as folder:
            output = Path(folder)/'turns.json'
            output.write_text('[]', encoding='utf-8')
            commands = []
            def run(command, **kwargs):
                commands.append(command)
                self.assertEqual(kwargs['env']['CUDA_VISIBLE_DEVICES'], '0')
                self.assertEqual(command[command.index('--ram-gib')+1], '4')
                return SimpleNamespace(returncode=20 if len(commands)<=failures else 0, stdout='', stderr='')
            with patch('diarization.require_ready'), patch('diarization.subprocess.run', side_effect=run):
                if requested == 'cuda' and failures >= 5:
                    with self.assertRaises(RuntimeError):
                        diarization.run_diarization('source.wav',output,Path(folder)/'progress.json',device=requested)
                else:
                    diarization.run_diarization('source.wav',output,Path(folder)/'progress.json',device=requested)
            return commands

    def test_oom_retries_smaller_batches_before_cpu_fallback(self):
        commands = self.run_retries('auto', 5)
        self.assertEqual([c[c.index('--batch-size')+1] for c in commands], ['16','8','4','2','1','1'])
        self.assertEqual([c[c.index('--device')+1] for c in commands], ['auto']*5+['cpu'])

    def test_gpu_request_retries_without_cpu(self):
        commands = self.run_retries('cuda', 5)
        self.assertTrue(all(c[c.index('--device')+1]=='cuda' for c in commands))
        self.assertEqual(len(commands), 5)

    @unittest.skipUnless(os.name=='nt', 'Windows memory budget')
    def test_ram_commit_limit_rejects_oversized_allocation_in_child(self):
        script = '''from speaker_resources import limit_ram
limit_ram(64/1024)
try:
    data=bytearray(128*1024**2)
except MemoryError:
    print('limit enforced')
else:
    raise SystemExit(2)
'''
        result=subprocess.run([sys.executable,'-X','utf8','-c',script],cwd=Path(__file__).resolve().parents[1],
                              capture_output=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('limit enforced', result.stdout)

    def test_ram_limit_cannot_be_silently_disabled_or_exceeded(self):
        for budget in (0, -1, 5):
            with self.assertRaises(ValueError):
                limit_ram(budget)

    def test_asr_child_finishes_before_speaker_worker_starts(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent/'data') as folder:
            folder=Path(folder)
            output=folder/'result.txt'
            state={'asr_done':False}
            def asr(command, **kwargs):
                self.assertIn('--asr-only', command)
                self.assertIn('--diarize', command)
                output.write_text('Hello', encoding='utf-8-sig')
                output.with_suffix('.words.json').write_text(json.dumps([{'start':0,'end':1,'text':' Hello'}]),encoding='utf-8')
                state['asr_done']=True
                return SimpleNamespace(returncode=0)
            def speakers(*args,**kwargs):
                self.assertTrue(state['asr_done'])
                self.assertEqual(kwargs['device'],'cuda')
                return [{'start':0,'end':1,'speaker':'A'}]
            argv=['worker',str(folder/'source.wav'),str(output),'--mode','transcribe','--progress',str(folder/'progress.json'),
                  '--diarize','--device','cuda']
            with patch('job_worker.sys.argv',argv), patch('diarization.require_ready'), \
                    patch('job_worker.subprocess.run',side_effect=asr), patch('diarization.run_diarization',side_effect=speakers):
                self.assertEqual(job_worker.main(),0)
            self.assertIn('Говорящий 1: Hello', output.read_text(encoding='utf-8-sig'))


if __name__=='__main__':
    unittest.main()
