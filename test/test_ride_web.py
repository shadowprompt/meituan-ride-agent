"""No external network: tests use only loopback HTTP and mocked CLI or offline demo."""
import base64
import http.client
import io
import json
import time
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from ride_web import ARTIFACTS, MAX_BODY, MAX_IMAGE, RideServer, WebError


class WebTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict('os.environ', {'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-vision','AMAP_API_KEY':'synthetic-map-key-only','OSM_SERVICE_URL':'http://127.0.0.1:9','OSM_SERVICE_TOKEN':'synthetic-relay-token-no-network-12345'})
        environment.start()
        self.addCleanup(environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)/'web'
        self.server = RideServer(0, output_root=self.root, cli_timeout=20)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path='/', data=None, *, method=None, headers=None, raw=None, wait=True):
        method = method or ('POST' if data is not None else 'GET')
        defaults = {}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            defaults = {'Content-Type': 'application/json', 'X-CSRF-Token': self.server.token}
        if raw is not None:
            body = raw
        defaults.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=40)
        connection.request(method, path, body, defaults)
        response = connection.getresponse()
        result = (response.status, dict(response.getheaders()), response.read())
        connection.close()
        # 旧端到端断言等待终态；异步协议专项必须 wait=False 直接检查真实 HTTP。
        if wait and result[0] == 202:
            task_id = json.loads(result[2])['task_id']
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                status = WebTests.request(self, '/api/tasks/'+task_id, wait=False)
                state = json.loads(status[2])
                if state.get('execution_state') not in ('queued', 'running'):
                    if state.get('web_execution', {}).get('http_error_code'):
                        # 模拟旧同步错误响应，只用于历史用例兼容。
                        code = state['web_execution']['http_error_code']
                        state.update(state['web_execution'])
                    else:
                        code = 201 if path == '/api/tasks' else 200
                    return code, status[1], json.dumps(state).encode()
                time.sleep(.01)
            raise AssertionError('后台测试任务未按时完成')
        return result

    def state(self, task_id=None, **fields):
        task_id = task_id or str(uuid.uuid4())
        folder = self.root/task_id
        (folder/'task').mkdir(parents=True)
        (folder/'inputs').mkdir()
        state = {'version': 1, 'mode': 'demo', 'goal': 'pixels', 'status': 'waiting_for_user',
                 'question': '请选择颜色', 'messages': [], 'verified': False, 'map_provider':'amap'}
        state.update(fields)
        (folder/'task'/'task.json').write_text(json.dumps(state), encoding='utf-8')
        return task_id

    def mock_cli(self, argv, **kwargs):
        self.assertEqual(argv[0], __import__('sys').executable)
        self.assertTrue(argv[1].endswith('ride_agent.py'))
        self.assertFalse(kwargs['shell'])
        self.assertLessEqual(kwargs['timeout'], 300)
        target = Path(argv[argv.index('--task-dir')+1])
        if argv[2] == 'start':
            target.mkdir()
            (target/'task.json').write_text(json.dumps({
                'version': 1, 'map_provider':'amap', 'mode': 'demo' if '--demo' in argv else 'api',
                'goal': argv[argv.index('--goal')+1], 'status': 'waiting_for_user',
                'question': '<img src=x onerror=alert(1)>', 'messages': ['secret'],
            }))
        return subprocess.CompletedProcess(argv, 0)

    def test_stale_artifact_marked_not_current(self):
        task_id=self.state(extracted=False,centerlined=True)
        folder=self.root/task_id/'task'/'centerline'
        folder.mkdir()
        (folder/'graph-overlay.png').write_bytes(b'old')
        status=json.loads(self.request('/api/tasks/'+task_id)[2])
        self.assertFalse(status['artifacts'][0]['current'])
        self.assertIn(b'a.current&&', self.request()[2])

    def test_page_config_and_csrf(self):
        code, headers, body = self.request()
        self.assertEqual(code, 200)
        self.assertIn('骑行截图分析'.encode(), body)
        self.assertIn(b'.textContent=', body)
        self.assertNotIn(b'.innerHTML', body)
        self.assertNotIn(b'cdn', body)
        self.assertIn('frame-ancestors', headers['Content-Security-Policy'])
        self.assertNotIn('Access-Control-Allow-Origin', headers)
        config = json.loads(self.request('/api/config')[2])
        self.assertEqual(config['csrf_token'], self.server.token)
        self.assertNotEqual(self.server.token, RideToken())
        with patch('ride_web.subprocess.run') as run:
            code, _, _ = self.request('/api/tasks', {}, headers={'X-CSRF-Token': ''})
            self.assertEqual(code, 403)
            run.assert_not_called()

    def test_origin_host_and_fetch_metadata(self):
        for headers in ({'Host': 'evil.test'}, {'Origin': 'https://evil.test'},
                        {'Origin': 'null'}, {'Sec-Fetch-Site': 'cross-site'},
                        {'Host': '127.0.0.1:9'}):
            self.assertEqual(self.request('/api/config', headers=headers)[0], 403)
            self.assertEqual(self.request('/api/tasks', {}, headers=headers)[0], 403)
        origin = 'http://127.0.0.1:'+str(self.server.server_port)
        self.assertEqual(self.request('/api/config', headers={'Origin': origin})[0], 200)
        code, headers, _ = self.request(method='OPTIONS')
        self.assertEqual(code, 405)
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_payload_validation(self):
        headers = {'X-CSRF-Token': self.server.token, 'Content-Type': 'application/json'}
        for raw in (b'[]', b'{', b'{"image_base64":NaN}', b'null', b'"hi"'):
            self.assertEqual(self.request('/api/tasks', method='POST', raw=raw, headers=headers)[0], 400)
        self.assertEqual(self.request('/api/tasks', {}, headers={'Content-Length': str(MAX_BODY+1)})[0], 413)
        self.assertEqual(self.request('/api/tasks', {}, headers={'Content-Type': 'text/plain'})[0], 415)
        invalid = [
            {}, {'image_base64': '@'}, {'image_base64': ''},
            {'image_base64': 'eA==', 'mode': 'shell'},
            {'image_base64': 'eA==', 'goal': 'any'},
            {'image_base64': 'eA==', 'task_dir': '/tmp/escape'},
            {'image_base64': 'eA==', 'mode': 'api'},
            {'image_base64': 'eA==', 'consent_upload': 'true'},
        ]
        with patch('ride_web.subprocess.run') as run:
            for data in invalid:
                self.assertIn(self.request('/api/tasks', data)[0], (400, 403))
            # Patch bound rather than allocate a 27MB base64 test body.
            with patch('ride_web.MAX_IMAGE', 2):
                self.assertEqual(self.request('/api/tasks', {'image_base64': 'eHh4'})[0], 400)
            run.assert_not_called()

    def test_create_and_safe_status(self):
        with patch('ride_web.subprocess.run', side_effect=self.mock_cli) as run:
            code, _, body = self.request('/api/tasks', {'image_base64': 'eA==', 'mode': 'api',
                                                       'goal': 'roads', 'consent_upload': True})
        self.assertEqual(code, 201, body)
        status = json.loads(body)
        task_id = str(uuid.UUID(status['task_id']))
        self.assertTrue((self.root/task_id/'inputs'/'source-image').is_file())
        self.assertIn('--consent-upload', run.call_args.args[0])
        self.assertNotIn('messages', status)
        self.assertNotIn('source_sha256', status)
        self.assertEqual(status['question'], '<img src=x onerror=alert(1)>')
        # JSON is application/json; browser task fields are inserted only as textContent.
        self.assertEqual(self.request('/api/tasks/'+task_id)[0], 200)
        for name in ('task.json', 'input.png', 'inputs/source-image', '../task.json',
                     'extraction/../../task.json', 'not-real.png'):
            self.assertEqual(self.request('/artifacts/'+task_id+'/'+name)[0], 404)

    def test_status_redacts_key_and_internal_error(self):
        task_id = self.state(question='my-secret-KEY', error='/private/path my-secret-KEY',
                             api_key='my-secret-KEY', messages=['my-secret-KEY'])
        with patch.dict('os.environ', {'RIDE_API_KEY': 'my-secret-KEY'}):
            code, _, body = self.request('/api/tasks/'+task_id)
        self.assertEqual(code, 200)
        self.assertNotIn(b'my-secret-KEY', body)
        self.assertNotIn(b'/private/path', body)

    def test_artifact_allowlist_and_symlinks(self):
        task_id = self.state()
        folder = self.root/task_id/'task'
        (folder/'extraction').mkdir()
        target = folder/'extraction'/'overlay.png'
        target.write_bytes(b'PNG')
        code, headers, body = self.request('/artifacts/'+task_id+'/extraction/overlay.png')
        self.assertEqual((code, body), (200, b'PNG'))
        self.assertEqual(headers['Content-Type'], 'image/png')
        outside = Path(self.temp.name)/'secret'
        outside.write_text('secret')
        target.unlink()
        target.symlink_to(outside)
        self.assertEqual(self.request('/artifacts/'+task_id+'/extraction/overlay.png')[0], 403)
        with patch('ride_web.subprocess.run') as run:
            self.assertEqual(self.request('/api/tasks/'+task_id+'/resume', {'answer': 'yes'})[0], 403)
            run.assert_not_called()
        target.unlink()
        (folder/'task.json').unlink()
        (folder/'task.json').symlink_to(outside)
        self.assertEqual(self.request('/api/tasks/'+task_id)[0], 403)

    def test_task_ids_encoded_paths_and_missing(self):
        for path in ('/api/tasks/../../etc', '/api/tasks/nope',
                     '/api/tasks/'+str(uuid.uuid4()), '/api/tasks/%2e%2e',
                     '/api/config?token=1', 'http://evil.test/api/config'):
            self.assertIn(self.request(path)[0], (403, 404))
        with self.assertRaises(WebError):
            self.server.folder('../elsewhere')

    def test_resume_writes_fixed_files_and_flags(self):
        task_id = self.state(mode='api')
        data = {'answer': '--evil; $(touch /tmp/pwn)', 'color': '#00BFA5', 'crop': [0, 1, 50, 60],
                'controls_json': '{"landmarks": []}',
                'consent_upload': True}
        with patch('ride_web.subprocess.run', side_effect=self.mock_cli) as run:
            code, _, body = self.request('/api/tasks/'+task_id+'/resume', data)
        self.assertEqual(code, 200, body)
        argv = run.call_args.args[0]
        self.assertIn('--consent-upload', argv)
        self.assertNotIn(data['answer'], argv)
        self.assertEqual(argv[argv.index('--crop')+1:argv.index('--crop')+5], ['0', '1', '50', '60'])
        inputs = self.root/task_id/'inputs'
        self.assertEqual((inputs/'answer.txt').read_text(), data['answer'])
        self.assertEqual((inputs/'controls-upload.json').read_text(), data['controls_json'])
        self.assertFalse((inputs/'osm-upload.xml').exists())
        with patch('ride_web.subprocess.run', side_effect=self.mock_cli) as run:
            self.assertEqual(self.request('/api/tasks/'+task_id+'/resume', {
                'consent_map': True, 'consent_upload': True})[0], 200)
            self.assertIn('--consent-map', run.call_args.args[0])
            self.assertNotIn('--fetch-map', run.call_args.args[0])
            self.assertEqual(self.request('/api/tasks/'+task_id+'/resume', {'fetch_map':True,'consent_map':True,'consent_upload':True})[0],409)
            # Consent is never persisted or inherited from last invocation.
            self.assertEqual(self.request('/api/tasks/'+task_id+'/resume', {'answer': 'yes'})[0], 403)

    def test_resume_rejects_bad_inputs_before_write(self):
        task_id = self.state()
        invalid = [
            {}, {'answer': ''}, {'answer': 'x'*8001}, {'answer': 123},
            {'color': 'red'}, {'color': '#00ffaa;'}, {'crop': [0, 0, 0, 5]},
            {'crop': [False, 0, 5, 5]}, {'crop': [0, 0, 5.1, 5]},
            {'crop': '../file'}, {'controls_json': 'NaN'}, {'controls_json': '[]'},
            {'controls_json': '{}', 'osm_xml': '<!DOCTYPE osm><osm/>'},
            {'osm_xml': ''}, {'fetch_map': True}, {'fetch_map': 'true'},
            {'fetch_map': True, 'consent_map': True, 'osm_xml': '<osm/>'},
            {'osm_file': '/etc/passwd'}, {'controls_file': '/etc/passwd'},
            {'consent_upload': 1, 'answer': 'hi'},
        ]
        with patch('ride_web.subprocess.run') as run:
            for data in invalid:
                code, _, body = self.request('/api/tasks/'+task_id+'/resume', data)
                self.assertEqual(code, 409 if data.get('fetch_map') is True else 400, (data,code,body))
            run.assert_not_called()
        self.assertEqual(list((self.root/task_id/'inputs').iterdir()), [])

    def test_timeout_and_cli_error_are_generic(self):
        task_id = self.state()
        with patch('ride_web.subprocess.run', side_effect=subprocess.TimeoutExpired('secret', 1)):
            self.assertEqual(self.request('/api/tasks/'+task_id+'/resume', {'answer': 'yes'})[0], 504)
        result = subprocess.CompletedProcess([], 1, b'API_KEY=secret', b'/private/secret')
        with patch('ride_web.subprocess.run', return_value=result):
            code, _, body = self.request('/api/tasks/'+task_id+'/resume', {'answer': 'yes'})
        self.assertEqual(code, 422)
        self.assertNotIn(b'secret', body)

    def test_concurrent_changes_are_serialized(self):
        import time
        task_id = self.state()
        active = 0
        peak = 0
        guard = threading.Lock()
        results = []

        def slow_cli(argv, **kwargs):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            time.sleep(0.1)
            with guard:
                active -= 1
            return subprocess.CompletedProcess(argv, 0)

        def call():
            results.append(self.request('/api/tasks/'+task_id+'/resume', {'answer': 'yes'})[0])

        with patch('ride_web.subprocess.run', side_effect=slow_cli):
            threads = [threading.Thread(target=call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(results), [200, 409])
        self.assertEqual(peak, 1)

    def test_symlinked_root_and_task_are_refused(self):
        task_id = self.state()
        link_id = str(uuid.uuid4())
        (self.root/link_id).symlink_to(self.root/task_id, target_is_directory=True)
        self.assertEqual(self.request('/api/tasks/'+link_id)[0], 403)
        outside = Path(self.temp.name)/'root-link'
        outside.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(WebError):
            RideServer(0, output_root=outside)

    def test_actual_http_amap_pixels_crop_and_osm_upload_rejection(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(image).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        stream=io.BytesIO();image.save(stream,format='PNG')
        code,_,body=self.request('/api/tasks',{'image_base64':base64.b64encode(stream.getvalue()).decode(),'mode':'demo','goal':'pixels','map_provider':'amap'})
        self.assertEqual(code,201,body)
        task_id=json.loads(body)['task_id']; endpoint='/api/tasks/'+task_id+'/resume'
        code,_,body=self.request(endpoint,{'crop':[0,0,80,60],'color':'#00BFA5'})
        self.assertEqual(code,200,body)
        result=json.loads(body)
        self.assertEqual(result['map_provider'],'amap')
        self.assertEqual(result['status'],'needs_confirmation')
        with patch('ride_web.subprocess.run') as cli:
            code,_,body=self.request(endpoint,{'osm_xml':'<osm/>'})
            self.assertEqual(code,400,body); cli.assert_not_called()
        code,_,body=self.request(endpoint,{'crop':[1,1,79,59]})
        self.assertEqual(code,200,body)
        result=json.loads(body)
        self.assertFalse(result.get('registered',False))
        self.assertFalse(result.get('match_attempted',False))
        self.assertFalse((self.root/task_id/'task'/'map.osm').exists())

    def test_actual_demo_cli_roundtrip(self):
        from PIL import Image, ImageDraw
        im = Image.new('RGB', (80, 60), 'white')
        ImageDraw.Draw(im).line([(10, 40), (50, 40), (50, 10)], fill='#00BFA5', width=5)
        out = io.BytesIO()
        im.save(out, format='PNG')
        # If the demo accidentally attempts network access the CLI gets no usable proxy/API config.
        with patch.dict('os.environ', {'RIDE_API_BASE': '', 'RIDE_API_KEY': '', 'RIDE_MODEL': ''}):
            code, _, body = self.request('/api/tasks', {
                'image_base64': base64.b64encode(out.getvalue()).decode(), 'mode': 'demo', 'goal': 'pixels'})
            self.assertEqual(code, 201, body)
            status = json.loads(body)
            self.assertEqual(status['status'], 'waiting_for_user')
            code, _, body = self.request('/api/tasks/'+status['task_id']+'/resume', {
                'answer': json.dumps({'crop': [0, 0, 80, 60], 'color': '#00BFA5'})})
        self.assertEqual(code, 200, body)
        status = json.loads(body)
        self.assertEqual(status['status'], 'needs_confirmation')
        self.assertFalse(status['verified'])
        self.assertGreaterEqual(len(status['artifacts']), 6)
        for artifact in status['artifacts']:
            self.assertIn(artifact['name'], ARTIFACTS)
            self.assertEqual(self.request(artifact['url'])[0], 200)
        self.assertEqual(self.request('/artifacts/'+status['task_id']+'/input.png')[0], 200)


def RideToken():
    # Independent process-equivalent server initialization must produce a fresh secret.
    with tempfile.TemporaryDirectory() as root:
        other = RideServer(0, output_root=Path(root)/'other')
        try:
            return other.token
        finally:
            other.server_close()


if __name__ == '__main__':
    unittest.main()
