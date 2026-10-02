import json
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from bosshunter.config import load_config
from bosshunter.agent_api import validate_agent_evaluations
from bosshunter.db import get_db, insert_job, persist_agent_evaluations, get_jobs_ready_to_send

spec = importlib.util.spec_from_file_location('worker', Path(__file__).parents[1] / 'scripts/codex_score.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)
JOB = {'id': 'synthetic-test', 'title': 'Web开发', 'company': '测试公司', 'city': '上海', 'salary': '15-25K', 'experience': '不限', 'jd': '使用Python开发Web应用。'}


def response(points=None):
    score = {k: {'evidence': '候选人有对应的Python开发项目证据', 'score': n} for k, n in w.COMPONENT_LIMITS.items()}
    if points is not None:
        for value in score.values(): value['score'] = points
    score.update(role_summary='Python开发', reason='有具体项目证据', missing='', caps=[], hard_gaps=[])
    return {'score': score, 'greeting': '您好，我有使用Python开发Web应用的项目经验，希望了解贵司岗位的具体需求。'}


class WorkerTests(unittest.TestCase):
    def test_recruiter_voice_rejected(self):
        examples = [
            '李同学您好，看到您有Python开发经验，和我们上海开发岗位契合，想进一步沟通。',
            '您好，我看到您有Python项目经验，希望与您交流我们公司的开发岗位。',
            '您好，我是招聘负责人，您的背景与岗位契合，邀请您面试。',
            '您好，我司正在招聘Python开发工程师，希望您加入我们的团队。',
            '您好，我看到你在前公司开发过Python应用，希望沟通开发岗位。',
        ]
        for greeting in examples:
            with self.subTest(greeting=greeting), self.assertRaises(ValueError):
                result = response(); result['greeting'] = greeting
                w.prepare(JOB, 'Python开发简历', load_config(), lambda _: result)

    def test_candidate_voice_and_prompt(self):
        def generate(prompt):
            self.assertIn('greeting 必须切换为简历本人', prompt)
            self.assertIn('你是求职者', prompt)
            return response()
        self.assertEqual(w.prepare(JOB, '简历', load_config(), generate)['greeting'], response()['greeting'])
        w.validate_candidate_greeting('您好，我有Python开发经验，想了解贵司岗位，方便与您沟通吗？')

    def test_prompt_version_invalidates_cache(self):
        before = w.fingerprint(JOB, '简历', load_config())
        with patch.object(w, 'PROMPT_VERSION', 'different'):
            self.assertNotEqual(before, w.fingerprint(JOB, '简历', load_config()))

    def test_invalid_cached_greeting_is_not_submitted(self):
        writes = []
        def handle(req):
            if req.method == 'POST':
                writes.append(1)
                return httpx.Response(200, json={'success': True})
            return httpx.Response(200, json={'task': None} if req.url.path.endswith('/state') else
                {'items': [JOB], 'resume': {'included': True, 'content': '简历'}})
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handle)) as client:
            item = w.prepare(JOB, '简历', load_config(), lambda _: response())
            item['greeting'] = '您好，我看到您有Python项目经验，希望与您交流我们公司的开发岗位。'
            path = Path(tmp) / (w.fingerprint(JOB, '简历', load_config()) + '.json')
            w.atomic_json(path, {'attempts': 1, 'evaluation': item})
            self.assertEqual(w.cycle(client, Path(tmp), lambda _: self.fail('model called')), 0)
            self.assertNotIn('evaluation', json.loads(path.read_text()))
            self.assertEqual(w.cycle(client, Path(tmp), lambda _: response()), 1)
        self.assertEqual(len(writes), 1)

    def test_low_score_drops_greeting(self):
        item = w.prepare(JOB, '简历', load_config(), lambda _: response(0))
        self.assertNotIn('greeting', item)

    def test_model_cannot_change_job_id_or_approve(self):
        result = response(); result.update(job_id='other', approved=True)
        item = w.prepare(JOB, '简历', load_config(), lambda _: result)
        self.assertEqual(set(item), {'job_id', 'score', 'greeting'})
        self.assertEqual(item['job_id'], JOB['id'])
        with tempfile.TemporaryDirectory() as tmp:
            db = get_db(Path(tmp)/'test.db'); insert_job(db, JOB)
            validated = validate_agent_evaluations([item], 71)
            persist_agent_evaluations(db, validated)
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0], 'ready')
            self.assertEqual(get_jobs_ready_to_send(db), [])
            with self.assertRaises(ValueError): persist_agent_evaluations(db, validated)
            db.close()

    def test_cached_result_survives_write_failure(self):
        calls = []; writes = []
        def handle(req):
            if req.url.path.endswith('/state'): return httpx.Response(200, json={'task': None})
            if req.method == 'GET': return httpx.Response(200, json={'items':[JOB], 'resume':{'included':True,'content':'Python开发简历'}})
            writes.append(1)
            if len(writes) == 1: return httpx.Response(503)
            return httpx.Response(200, json={'success':True})
        def model(_): calls.append(1); return response()
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(handle)) as c:
            with self.assertRaises(httpx.HTTPStatusError): w.cycle(c, Path(tmp), model)
            self.assertEqual(w.cycle(c, Path(tmp), model), 1)
            self.assertEqual(w.cycle(c, Path(tmp), model), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(writes), 2)

    def test_active_task_does_not_invoke_model(self):
        with httpx.Client(base_url='http://127.0.0.1', transport=httpx.MockTransport(lambda _: httpx.Response(200,json={'task':{'id':'busy'}}))) as c:
            self.assertEqual(w.cycle(c, generate=lambda _: self.fail('model called')), 0)

    def test_model_failures_stop_after_three_attempts(self):
        calls=[]
        def handle(req):
            return httpx.Response(200,json={'task':None} if req.url.path.endswith('/state') else {'items':[JOB],'resume':{'included':True,'content':'简历'}})
        def model(_): calls.append(1); raise RuntimeError('failure')
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(base_url='http://127.0.0.1',transport=httpx.MockTransport(handle)) as c:
            for _ in range(5): w.cycle(c,Path(tmp),model)
        self.assertEqual(len(calls),3)

if __name__ == '__main__': unittest.main()
