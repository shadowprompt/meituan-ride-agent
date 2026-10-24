"""新建 API pixels 的离线复现；网络均 mock，图片只用临时合成图。"""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from PIL import Image, ImageDraw
import ride_agent as agent
from ride_errors import TaskFailure
from ride_web import GUIDANCE, processing_category
import test_ride_web as fixtures
from test_route_review import mock_observe, mock_review


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'source.png'
        image = Image.new('RGB', (80, 60), 'white')
        ImageDraw.Draw(image).line([(10,45),(60,45),(60,10)], fill='#00BFA5', width=5)
        image.save(self.source)
        self.folder = self.root/'task'
        self.state = agent.start(self.source, self.folder)

    def failed(self, error):
        planner = unittest.mock.Mock()
        planner.decide.side_effect = error
        return agent.run(self.state, self.folder, planner)

    def test_missing_config_not_extraction_required_or_secret(self):
        with patch.dict(os.environ, {}, clear=True):
            result = agent.run(self.state, self.folder, agent.ConsentedPlanner())
        self.assertEqual(processing_category(result), 'api_configuration_missing')
        self.assertFalse(result['extracted'])
        self.assertNotIn('颜色', GUIDANCE['api_configuration_missing'])

    def test_each_missing_field_cli_path(self):
        import sys
        for i, env in enumerate(({}, {'RIDE_API_KEY':'mock-only'}, {'RIDE_MODEL':'mock-vision'})):
            child_env = {k:v for k,v in os.environ.items() if k not in ('RIDE_API_KEY','RIDE_MODEL','RIDE_API_BASE')}
            child_env.update(env)
            folder = self.root/('cli-'+str(i))
            result = subprocess.run([sys.executable,'ride_agent.py','start','--image',str(self.source),
                '--task-dir',str(folder),'--consent-upload'],capture_output=True,env=child_env,timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            state = agent.load(folder)
            self.assertEqual(state['status'],'error')
            self.assertEqual(state['error_category'],'api_configuration_missing')
            self.assertEqual(state['error_stage'],'planner')
            self.assertFalse(state['extracted'])
            self.assertNotIn('mock-only',result.stdout.decode())

    def test_unknown_and_network_causes_do_not_guess_color(self):
        for error, category in [(ValueError('SECRET /private'), 'task_processing_failed'),
                                (TimeoutError('SECRET'), 'api_timeout'),
                                (urllib.error.URLError(TimeoutError()), 'api_timeout'),
                                (urllib.error.URLError('SECRET'), 'api_network')]:
            with self.subTest(category=category):
                result = self.failed(error)
                self.assertEqual(processing_category(result), category)
                self.assertNotIn('SECRET', (self.folder/'task.json').read_text())
                self.assertNotIn('/private', result['error'])
                self.assertNotIn('summary', result)

    def test_tool_failure_is_stage_specific_and_safe(self):
        planner = unittest.mock.Mock()
        planner.decide.return_value = {'action':'extract','color':'#00BFA5','crop':[0,0,80,60]}
        with patch('ride_agent.extract', side_effect=OSError('SECRET /private')):
            result = agent.run(self.state, self.folder, planner)
        self.assertEqual(result['error_category'], 'extraction_failed')
        self.assertEqual(result['error_stage'], 'extract')
        self.assertFalse(result['extracted'])
        self.assertNotIn('SECRET', (self.folder/'task.json').read_text())

    def response(self, data):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = data
        return response

    def test_api_protocol_and_http_classified_without_body(self):
        with patch.dict(os.environ, {'RIDE_API_KEY':'SECRET','RIDE_MODEL':'mock'}):
            for payload in (b'bad SECRET', b'{"choices":[]}',
                            b'{"choices":[{"message":{"content":"[]"}}]}'):
                with patch('ride_agent.urllib.request.build_opener') as opener:
                    opener.return_value.open.return_value = self.response(payload)
                    result = agent.run(self.state, self.folder, agent.ConsentedPlanner())
                self.assertEqual(result['error_category'], 'api_response_invalid')
                self.assertNotIn('SECRET', (self.folder/'task.json').read_text())
            with patch('ride_agent.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = urllib.error.HTTPError('https://secret',401,'SECRET',{},None)
                result = agent.run(self.state, self.folder, agent.ConsentedPlanner())
            self.assertEqual(result['error_category'], 'api_http')
            self.assertNotIn('SECRET', result['error'])

    def test_new_api_pixels_success_mock_transport_real_extraction(self):
        calls = []
        actions = [{'action':'extract','crop':[0,0,80,60],'color':'#00BFA5'},
                   mock_observe, mock_review, {'action':'deliver','summary':'mock当前版本复核候选'}]
        def respond(request, **kwargs):
            body = json.loads(request.data)
            action = actions[len(calls)]
            calls.append(body)
            if callable(action):
                action = action(body)
            payload = json.dumps({'choices':[{'message':{'content':json.dumps(action)}}]}).encode()
            return self.response(payload)
        with patch.dict(os.environ, {'RIDE_API_KEY':'SECRET','RIDE_MODEL':'mock'}), \
             patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = respond
            result = agent.run(self.state, self.folder, agent.ConsentedPlanner())
        self.assertEqual(opener.return_value.open.call_count, 4)
        self.assertTrue(agent.route_review.approved(result, self.folder))
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertTrue(result['extracted'])
        self.assertTrue(result['centerlined'])
        self.assertFalse(result['registered'])
        self.assertFalse(result['verified'])
        for name in result['artifacts']:
            self.assertTrue((self.folder/name).is_file())

    def test_unknown_legacy_error_not_inferred_from_stage(self):
        for flags in ({}, {'extracted':True}, {'extracted':True,'centerlined':True}):
            self.assertEqual(processing_category(dict(status='error', error='SECRET', **flags)),
                             'task_processing_failed')


class WebDiagnosticTests(unittest.TestCase):
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    request = fixtures.WebTests.request
    state = fixtures.WebTests.state

    def test_create_disguised_tiff_cli_fixed_error_not_raw(self):
        image = io.BytesIO()
        Image.new('RGB',(20,20),'white').save(image,format='TIFF')
        # 真 CLI 仅执行本地图片校验，在模型调用之前拒绝 TIFF。
        code, _, body = self.request('/api/tasks', {
            'mode':'api','goal':'pixels','consent_upload':True,
            'image_base64':base64.b64encode(image.getvalue()).decode()})
        self.assertEqual(code, 422, body)
        self.assertEqual(json.loads(body)['error_category'],'image_input_invalid')
        self.assertNotIn(str(self.root).encode(), body)

    def test_create_api_mock_cli_runs_real_planner_and_tools(self):
        image = io.BytesIO()
        source = Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(source).line([(10,45),(60,45),(60,10)],fill='#00BFA5',width=5)
        source.save(image,format='PNG')
        def local_cli(argv, **kwargs):
            folder = Path(argv[argv.index('--task-dir')+1])
            state = agent.start(argv[argv.index('--image')+1], folder)
            actions = [{'action':'extract','color':'#00BFA5','crop':[0,0,80,60]},
                       mock_observe, mock_review, {'action':'deliver','summary':'网页mock复核候选'}]
            calls = []
            def respond(request, **kwargs):
                body = json.loads(request.data)
                action = actions[len(calls)]
                calls.append(body)
                if callable(action):
                    action = action(body)
                response = unittest.mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps({
                    'choices':[{'message':{'content':json.dumps(action)}}]}).encode()
                return response
            with patch.dict(os.environ, {'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock'}), \
                 patch('ride_agent.urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = respond
                result = agent.run(state, folder, agent.ConsentedPlanner())
            self.assertEqual(len(calls), 4)
            self.assertTrue(agent.route_review.approved(result, folder))
            self.assertEqual(result['status'],'needs_confirmation')
            return subprocess.CompletedProcess(argv,0,b'',b'')
        with patch('ride_web.subprocess.run',side_effect=local_cli):
            code, _, body = self.request('/api/tasks', {
                'mode':'api','goal':'pixels','consent_upload':True,
                'image_base64':base64.b64encode(image.getvalue()).decode()})
        data = json.loads(body)
        self.assertEqual(code,201,body)
        self.assertEqual(data['status'],'needs_confirmation')
        self.assertNotIn('error_category', data)
        self.assertTrue(data['artifacts'])
        self.assertTrue(all(item['current'] for item in data['artifacts']))

    def test_status_stage_allowlist_no_raw_text(self):
        for stage, expected in [('extract', 'extract'), ('SECRET /private', None)]:
            task = self.state(status='error',error='SECRET',error_category='extraction_failed',error_stage=stage)
            code, _, body = self.request('/api/tasks/'+task)
            self.assertEqual(code,200)
            self.assertEqual(json.loads(body).get('error_stage'),expected)
            self.assertNotIn(b'SECRET',body)

    def test_missing_config_web_preflight_each_missing_field(self):
        for env in ({}, {'RIDE_API_KEY':'mock-only'}, {'RIDE_MODEL':'mock-vision'},
                    {'RIDE_API_KEY':'','RIDE_MODEL':'mock-vision'},
                    {'RIDE_API_KEY':'mock-only','RIDE_MODEL':''}):
            with self.subTest(fields=list(env)), patch.dict(os.environ,env,clear=True), \
                 patch('ride_web.subprocess.run') as cli:
                before = list(self.root.iterdir())
                code, _, body = self.request('/api/tasks', {'mode':'api','consent_upload':True,
                                                           'image_base64':'eA=='})
                self.assertEqual(code,422,body)
                self.assertEqual(json.loads(body)['error_category'],'api_configuration_missing')
                self.assertNotIn(b'mock-only',body)
                self.assertEqual(list(self.root.iterdir()),before)
                cli.assert_not_called()

    def test_cli_fixed_code_and_unknown_stderr(self):
        task = self.state()
        for stderr, category in [(b'{"error_category":"api_configuration"}', 'api_configuration'),
                                 (b'SECRET /private', 'cli_failed'),
                                 (b'{"error_category":"SECRET"}', 'cli_failed')]:
            with patch('ride_web.subprocess.run', return_value=subprocess.CompletedProcess([],2,b'SECRET',stderr)):
                code, _, body = self.request('/api/tasks/'+task+'/resume', {'answer':'继续'})
            self.assertEqual(code,422)
            self.assertEqual(json.loads(body)['error_category'],category)
            self.assertNotIn(b'SECRET',body)


if __name__ == '__main__':
    unittest.main()
