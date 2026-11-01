import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi.testclient import TestClient

from history import HistoryStore
from jobqueue import TaskQueue, TERMINAL
from speaker_text import align_words, render_transcript, validate_names
from test_queue import command, until
import transcribe
import web
import diarization

class SpeakerTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parent / 'data'
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def data(self):
        return align_words([
            {'start':0,'end':.4,'text':' Hello'}, {'start':.5,'end':.9,'text':' there.'},
            {'start':1.1,'end':1.4,'text':' Yes.'}, {'start':2,'end':2.4,'text':' Good.'}],
            [{'start':0,'end':1,'speaker':'B'},{'start':1,'end':1.5,'speaker':'A'},
             {'start':2,'end':2.6,'speaker':'B'}])
    def test_word_alignment_labels_and_unknown_are_honest(self):
        data = self.data()
        self.assertEqual([s['speaker'] for s in data['segments']], ['speaker_1','speaker_2','speaker_1'])
        self.assertEqual(data['segments'][0]['text'], 'Hello there.')
        self.assertIn('[00:00] Говорящий 1: Hello there.', render_transcript(data))
        unknown = align_words([{'start':20,'end':21,'text':' Hello'}], [{'start':0,'end':1,'speaker':'A'}])
        self.assertEqual(unknown['segments'][0]['speaker'], 'unknown')
        self.assertEqual(align_words([],[])['speakers'], [])
        point = align_words([{'start':1.2,'end':1.2,'text':' Yes'}], [{'start':1,'end':2,'speaker':'A'}])
        self.assertEqual(point['segments'][0]['speaker'], 'speaker_1')
    def test_names_reject_unknown_ids_controls_and_allow_reset(self):
        data = self.data()
        self.assertEqual(validate_names(data, {'speaker_1':' Джон Доу ', 'speaker_2':''}), {'speaker_1':'Джон Доу'})
        for names in ({'other':'Joe'}, {'speaker_1':'A\nB'}, {'speaker_1':'a'*81}):
            with self.assertRaises(ValueError): validate_names(data, names)
    def save(self, store):
        output = self.root / 'result.txt'
        data = self.data()
        output.write_text(render_transcript(data),encoding='utf-8-sig')
        output.with_suffix('.speakers.json').write_text(json.dumps(data), encoding='utf-8')
        job = SimpleNamespace(id=uuid.uuid4().hex, filename='беседа.mp3',mode='transcribe',output=output,options={'diarize':True},elapsed=2)
        store.save(job)
        return job.id
    def test_names_persist_in_txt_history_and_delete_cascades(self):
        store = HistoryStore(self.root / 'history')
        identity = self.save(store)
        store.rename_speakers(identity, {'speaker_1':'Джон Доу', 'speaker_2':'Анна'})
        restored = HistoryStore(self.root / 'history')
        self.assertEqual(restored.transcript(identity)['names']['speaker_1'], 'Джон Доу')
        saved = restored.path(restored.get(identity)).read_text(encoding='utf-8-sig')
        self.assertIn('Джон Доу: Hello',saved)
        self.assertIn('Анна: Yes',saved)
        restored.rename_speakers(identity, {})
        self.assertIn('Говорящий 1', restored.transcript(identity)['text'])
        restored.delete(identity)
        with restored._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM speaker_data').fetchone()[0], 0)
    def test_whisper_words_are_requested_only_when_enabled(self):
        calls = []
        def recognize(*args,**kwargs):
            calls.append(kwargs)
            word = SimpleNamespace(start=0,end=.4,word=' Hi!')
            segment = SimpleNamespace(start=0,end=.5,text='Hi!',words=[word])
            return iter([segment]),SimpleNamespace(language='en',duration=1)
        model = SimpleNamespace(transcribe=recognize)
        for enabled in (False,True):
            output = self.root / f'{enabled}.txt'
            args = SimpleNamespace(audio='x',language='auto',overwrite=False,diarize=enabled)
            transcribe.transcribe(model,args,output)
            self.assertEqual(calls[-1]['word_timestamps'],enabled)
            self.assertEqual(output.with_suffix('.words.json').exists(),enabled)
    def test_api_rejects_unprepared_model_without_breaking_plain_jobs(self):
        with patch.object(web,'HISTORY_ROOT', self.root/'api-history'), patch.object(TaskQueue,'_command',command), \
                patch('diarization.require_ready',side_effect=ValueError('Сначала скачайте модель')), TestClient(web.app) as client:
            response=client.post('/api/jobs',files={'file':('a.wav',b'fast')},data={'diarize':'true'})
            self.assertEqual(response.status_code,400)
            self.assertIn('скачайте',response.json()['detail'])
            response=client.post('/api/jobs',files={'file':('a.wav',b'fast')})
            self.assertEqual(response.status_code,202)
    def test_api_mapping_updates_download_and_zip(self):
        with patch.object(web,'HISTORY_ROOT',self.root/'api-history'), TestClient(web.app) as client:
            store=client.app.state.history
            identity=self.save(store)
            self.assertEqual(len(client.get(f'/api/history/{identity}/result').json()['speakers']),2)
            response=client.put(f'/api/history/{identity}/speakers',json={'names':{'speaker_1':'Джон Доу'}})
            self.assertEqual(response.status_code,200,response.text)
            self.assertIn('Джон Доу',client.get(f'/api/history/{identity}/download').content.decode('utf-8-sig'))
            self.assertEqual(client.put(f'/api/history/{identity}/speakers',json={'names':{'unknown-id':'bad'}}).status_code,400)
            client.app.state.jobs.jobs[identity]=SimpleNamespace(id=identity,output=store.path(store.get(identity)),output_name='беседа.txt',state='done')
            archive=client.get('/api/jobs/archive')
            self.assertIn('Джон Доу',zipfile.ZipFile(io.BytesIO(archive.content)).read('беседа.txt').decode('utf-8-sig'))
            client.app.state.jobs.jobs.clear()

    def test_preparation_keeps_token_out_of_command_and_status(self):
        captured = {}
        process = SimpleNamespace(wait=lambda: 1, poll=lambda: 1)
        def launch(command, **kwargs):
            captured['command'] = command
            captured['env'] = kwargs['env'].copy()
            return process
        preparation = diarization.ModelPreparation(self.root)
        with patch('diarization.readiness', return_value={'libraries_ready':True,'model_ready':False,'ready':False}), \
                patch('diarization.subprocess.Popen', side_effect=launch):
            preparation.start('hf_test_secret_not_real')
            preparation.thread.join(timeout=3)
            status = preparation.status()
        self.assertNotIn('hf_test_secret_not_real', json.dumps(status))
        self.assertNotIn('hf_test_secret_not_real', ' '.join(captured['command']))
        self.assertEqual(captured['env']['HF_TOKEN'], 'hf_test_secret_not_real')
        preparation.close()

    def test_inference_is_offline_and_does_not_forward_token(self):
        output = self.root / 'turns.json'
        output.write_text('[]',encoding='utf-8')
        def run(command, **kwargs):
            self.assertEqual(kwargs['env']['HF_HUB_OFFLINE'],'1')
            self.assertEqual(kwargs['env']['CUDA_VISIBLE_DEVICES'],'-1')
            self.assertNotIn('HF_TOKEN',kwargs['env'])
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        with patch('diarization.require_ready'), patch.dict('os.environ', {'HF_TOKEN':'not-real'}), \
                patch('diarization.subprocess.run',side_effect=run):
            self.assertEqual(diarization.run_diarization(self.root/'audio.wav',output,self.root/'progress.json'),[])

if __name__=='__main__': unittest.main()
