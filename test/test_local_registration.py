"""本地经纬度交付回归：合成图/合成地标，不代表真实用户轨迹已定位。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw

from ride_agent import start, run, load, save, LocalPlanner, ConsentedPlanner
from test_register_pixels import spec
import test_api_start_e2e as web_fixtures

BASE = Path(__file__).resolve().parent.parent
GEO = 'registration/registered-segments.geojson'


class LocalRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        image = Image.new('RGB', (80, 60), 'white')
        ImageDraw.Draw(image).line([(10, 40), (50, 40), (50, 10)], fill='#00BFA5', width=5)
        source = self.root/'image.png'; image.save(source)
        self.folder = self.root/'task'
        state = start(source, self.folder, goal='registration')
        state['user_extraction_constraints'] = {'crop':[0,0,80,60], 'color':'#00BFA5'}
        self.state = run(state, self.folder, LocalPlanner())
        self.assertTrue(self.state['centerlined'])
        self.controls = self.root/'user-controls.json'
        self.controls.write_text(json.dumps(spec()))

    def cli(self, *args):
        # 禁用socket覆盖CLI子进程：即便配置了有效API/残留地图请求也不能联网。
        code = "import runpy,socket,ssl,urllib.request; socket.socket=lambda *a,**k: (_ for _ in ()).throw(AssertionError('network forbidden')); runpy.run_path('src/ride_agent.py',run_name='__main__')"
        return subprocess.run([sys.executable, '-c', code, 'resume', '--task-dir', str(self.folder),
                               '--local-only', *args], cwd=BASE, capture_output=True, text=True)

    def attach(self, controls=None):
        (self.folder/'controls.json').write_text(json.dumps(controls or spec()))
        self.state['controls_crop'] = [0,0,80,60]

    def test_cli_recovers_model_action_error_and_exports_unordered_segments(self):
        # 已提取API任务，模型动作错误不使已有像素失效。
        class Bad:
            semantic_review = True
            def decide(self, *_): return {'action':'invalid'}
        self.state = run(self.state, self.folder, Bad())
        self.assertEqual(self.state['status'], 'error')
        before = (self.folder/'centerline/pixel-graph.json').read_bytes()
        result = self.cli('--controls-file', str(self.controls))
        self.assertEqual(result.returncode, 0, result.stderr)
        state = load(self.folder)
        self.assertEqual(state['status'], 'needs_confirmation')
        self.assertTrue(state['registration_gate_passed'])
        self.assertEqual(state['execution'], {'source':'local','api_responses':0})
        self.assertFalse(state['verified']); self.assertFalse(state['original_gps'])
        self.assertNotIn('error_category', state)
        self.assertEqual(before, (self.folder/'centerline/pixel-graph.json').read_bytes())
        geo = json.loads((self.folder/GEO).read_text())
        self.assertTrue(geo['features'])
        for feature in geo['features']:
            self.assertFalse(feature['properties']['traversal_order_known'])
            self.assertFalse(feature['properties']['verified'])
            self.assertEqual(feature['geometry']['type'], 'LineString')
        self.assertIn(GEO, state['artifacts'])
        self.assertFalse((self.folder/'map.osm').exists())

    def test_missing_invalidated_or_different_crop_never_registers(self):
        for kind in ('missing', 'invalidated', 'crop'):
            with self.subTest(kind=kind):
                state = copy.deepcopy(self.state)
                if kind != 'missing':
                    self.attach(); state['controls_crop'] = [0,0,80,60]
                if kind == 'invalidated': state['controls_invalidated'] = True
                if kind == 'crop': state['controls_crop'] = [1,0,80,60]
                result = run(state, self.folder, LocalPlanner())
                self.assertFalse(result['registered'])
                self.assertEqual(result['status'], 'waiting_for_user')
                self.assertFalse((self.folder/GEO).exists())

    def test_cli_resumes_with_existing_current_controls_without_new_file(self):
        self.attach(); save(self.state, self.folder)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        state = load(self.folder)
        self.assertEqual(state['status'], 'needs_confirmation')
        self.assertTrue(state['registered'])
        self.assertIn(GEO, state['artifacts'])

    def test_cli_rejects_wrong_coordinate_system(self):
        controls = spec(); controls['coordinate_system'] = 'GCJ02'
        self.controls.write_text(json.dumps(controls))
        result = self.cli('--controls-file', str(self.controls))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(load(self.folder)['registered'])
        self.assertFalse((self.folder/GEO).exists())

    def test_gate_failure_delivers_registration_but_blocks_roads_and_download(self):
        for kind in ('no_checks', 'bad_check'):
            controls = spec()
            if kind == 'no_checks':
                controls['control_points'] = controls['control_points'][:3]
                controls['check_points'] = []
            else: controls['check_points'][0]['wgs84'][0] += .002
            for goal in ('registration', 'roads'):
                with self.subTest(kind=kind, goal=goal):
                    self.attach(controls)
                    state = copy.deepcopy(self.state)
                    state.update(goal=goal, controls_crop=[0,0,80,60], map_download_requested=True)
                    with patch('ride_agent.fetch_roads', side_effect=AssertionError('no map')), patch('ride_agent.match', side_effect=AssertionError('no match')):
                        result = run(state, self.folder, LocalPlanner(), consent_map=True)
                    self.assertTrue(result['registered'])
                    self.assertFalse(result['registration_gate_passed'])
                    self.assertFalse(result['match_attempted'])
                    self.assertTrue((self.folder/GEO).exists())
                    self.assertEqual(result['status'], 'needs_confirmation' if goal == 'registration' else 'waiting_for_user')

    def test_checked_roads_wait_for_map_and_fresh_consent(self):
        self.attach()
        for requested in (False, True):
            state = copy.deepcopy(self.state)
            state.update(map_provider='osm', goal='roads', controls_crop=[0,0,80,60], map_download_requested=requested)
            with patch('ride_agent.fetch_roads', side_effect=AssertionError('no consent')), patch('ride_agent.match', side_effect=AssertionError('no map')):
                result = run(state, self.folder, LocalPlanner(), consent_map=False)
            self.assertTrue(result['registration_gate_passed'])
            self.assertEqual(result['status'], 'waiting_for_user')
            self.assertFalse(result['match_attempted'])

    def test_local_roads_attempts_supplied_map_without_download(self):
        self.attach()
        (self.folder/'map.osm').write_text('<osm version="0.6"/>')
        self.state.update(map_provider='osm', goal='roads')
        with patch('ride_agent.fetch_roads', side_effect=AssertionError('no automatic download')):
            state = run(self.state, self.folder, LocalPlanner())
        self.assertTrue(state['registration_gate_passed'])
        self.assertTrue(state['match_attempted'])
        self.assertEqual(state['status'], 'needs_confirmation')
        self.assertEqual(state['road_match_status'], 'no_match')
        self.assertIn('roads/road-candidates.geojson', state['artifacts'])
        self.assertFalse(state['verified'])

    def test_consented_pipeline_registers_before_model_decision(self):
        self.attach(); save(self.state, self.folder)
        # API流程也确定性配准，但仍保留其语义复核要求，不静默声称复核通过。
        with patch('ride_agent.APIPlanner') as model:
            model.return_value.decide.return_value = {'action':'ask', 'question':'需要复核'}
            state = run(self.state, self.folder, ConsentedPlanner())
        self.assertTrue(state['registered'])
        self.assertTrue(state['semantic_review_required'])
        self.assertEqual(state['status'], 'waiting_for_user')


class WebLocalRegistrationTests(unittest.TestCase):
    setUp = web_fixtures.APIStartE2ETests.setUp
    tearDown = web_fixtures.APIStartE2ETests.tearDown
    request = web_fixtures.APIStartE2ETests.request
    model = web_fixtures.APIStartE2ETests.model
    create_image = web_fixtures.APIStartE2ETests.create_image

    def test_upload_controls_after_model_action_error_same_task(self):
        image = Image.new('RGB', (80,60), 'white')
        ImageDraw.Draw(image).line([(10,40),(50,40),(50,10)], fill='#00BFA5', width=5)
        code, data, calls = self.create_image(image, [
            {'action':'extract','crop':[0,0,80,60],'color':'#00BFA5'}, {'action':'invalid'}])
        self.assertEqual(code, 201)
        self.assertEqual(data['status'], 'error')
        self.assertTrue(data['centerlined'])
        folder = self.root/data['task_id']/'task'
        before = hashlib.sha256((folder/'centerline/pixel-graph.json').read_bytes()).hexdigest()
        # 模型服务仍活着；若错误调用模型，调用数会增加。故意无效的凭据也不阻塞本地继续。
        with patch.dict(os.environ, {'RIDE_API_BASE':'bad','RIDE_API_KEY':'','RIDE_MODEL':''}):
            code, _, body = self.request('/api/tasks/'+data['task_id']+'/resume',
                {'local_only':True, 'controls_json':json.dumps(spec())})
        self.assertEqual(code, 200, body)
        result = json.loads(body)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['task_id'], data['task_id'])
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertEqual(result['execution'], {'source':'local','api_responses':0})
        self.assertFalse(result['verified']); self.assertFalse(result['original_gps'])
        self.assertEqual(result['review_status'], 'not_reviewed')
        self.assertNotIn('error_category', result)
        self.assertEqual(hashlib.sha256((folder/'centerline/pixel-graph.json').read_bytes()).hexdigest(), before)
        artifact = next(a for a in result['artifacts'] if a['name'] == GEO)
        self.assertTrue(artifact['current'])
        code, _, exported = self.request(artifact['url'])
        self.assertEqual(code, 200)
        geo = json.loads(exported)
        self.assertEqual(geo, json.loads((folder/GEO).read_text()))
        self.assertTrue(geo['features'])
        self.assertFalse(geo['features'][0]['properties']['traversal_order_known'])
        self.assertFalse((folder/'map.osm').exists())


if __name__ == '__main__': unittest.main()
