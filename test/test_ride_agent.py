import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from ride_agent import start, run, load, save, DemoPlanner, APIPlanner


class AgentTests(unittest.TestCase):
    def fixture(self, root):
        path=Path(root)/'image.png'
        im=Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(im).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        im.save(path)
        return path

    def test_persistent_demo_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True)
            state=run(state,folder,DemoPlanner())
            self.assertEqual(state['status'],'waiting_for_user')
            state=load(folder)
            state['messages'].append({'role':'user','content':json.dumps({'crop':[0,0,80,60],'color':'#00BFA5'})})
            state=run(state,folder,DemoPlanner())
            self.assertEqual(state['status'],'needs_confirmation')
            self.assertFalse(state['verified'])
            for name in state['artifacts']:
                self.assertTrue((folder/name).exists())
            self.assertEqual(load(folder)['status'],'needs_confirmation')
            with self.assertRaises(ValueError): start(self.fixture(tmp),folder,True)

    def test_unknown_tool_refused(self):
        class Bad:
            def decide(self,*args):return {'action':'shell','command':'echo test'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Bad())
            self.assertEqual(state['status'],'error')
            self.assertEqual(state['error_category'],'model_action_invalid')

    def test_no_false_delivery(self):
        class Bad:
            def decide(self,*args):return {'action':'deliver','summary':'finished'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Bad())
            self.assertEqual(state['status'],'paused')
            self.assertNotIn('summary',state)

    def test_loop_budget(self):
        class Repeater:
            def decide(self,*args):return {'action':'extract','crop':[0,0,80,60],'color':'#00BFA5'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Repeater(),budget=2)
            self.assertEqual(state['status'],'paused')

    def test_registration_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True,goal='registration')
            state['messages'].append({'role':'user','content':json.dumps({'crop':[0,0,80,60],'color':'#00BFA5'})})
            state=run(state,folder,DemoPlanner())
            self.assertEqual(state['status'],'waiting_for_user')
            self.assertTrue(state['centerlined'])
            controls=json.loads((Path(__file__).resolve().parent.parent/'examples/synthetic-controls.json').read_text())
            (folder/'controls.json').write_text(json.dumps(controls))
            state['controls_crop'] = state['extraction_config']['crop']
            state=run(state,folder,DemoPlanner())
            self.assertEqual(state['status'],'needs_confirmation')
            self.assertTrue(state['registered'])
            self.assertTrue(state['registration_gate_passed'])
            self.assertIn('registration/registered-segments.geojson',state['artifacts'])

    def test_register_cannot_invent_controls(self):
        class Bad:
            def decide(self,*args): return {'action':'register','controls':{'fake':True}}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True)
            state['centerlined']=True
            state=run(state,folder,Bad())
            self.assertEqual(state['status'],'error')
            self.assertEqual(state['error_category'],'model_action_invalid')

    def test_mock_api_request(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self,n):
                return json.dumps({'choices':[{'message':{'content':json.dumps({'action':'ask','question':'crop?'})}}]}).encode()
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True)
            with patch.dict(os.environ,{'RIDE_API_BASE':'https://example.com/v1','RIDE_API_KEY':'fake','RIDE_MODEL':'fake'}):
                planner=APIPlanner()
                with patch('urllib.request.build_opener') as opener:
                    opener.return_value.open.return_value=Response()
                    result=planner.decide(state,folder)
                    self.assertEqual(result['action'],'ask')
                    self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 300)
                    request=opener.return_value.open.call_args.args[0]
                    self.assertEqual(request.get_header('Authorization'),'Bearer fake')
                    body=json.loads(request.data)
                    self.assertTrue(body['messages'][1]['content'][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_roads_goal_rejects_premature_delivery(self):
        class Bad:
            def decide(self,*args):return {'action':'deliver','summary':'finished'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True,goal='roads')
            state.update(extracted=True,centerlined=True,registered=True)
            state=run(state,folder,Bad())
            self.assertEqual(state['status'],'paused')
            self.assertNotIn('summary',state)
            self.assertFalse(state.get('match_attempted',False))

    def test_bad_registration_blocks_roads(self):
        class Attempt:
            def decide(self,*args):return {'action':'match'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True,goal='roads')
            state.update(map_provider='osm',registered=True,registration_gate_passed=False)
            state=run(state,folder,Attempt())
            self.assertEqual(state['status'],'waiting_for_user')
            self.assertFalse(state.get('match_attempted',False))

    def test_api_security(self):
        with patch.dict(os.environ,{'RIDE_API_BASE':'http://example.com/v1','RIDE_API_KEY':'fake','RIDE_MODEL':'fake'}):
            with self.assertRaises(ValueError):APIPlanner()
        with patch.dict(os.environ,{'RIDE_API_BASE':'https://user:pass@example.com/v1','RIDE_API_KEY':'fake','RIDE_MODEL':'fake'}):
            with self.assertRaises(ValueError):APIPlanner()




class MapAgentTests(unittest.TestCase):
    def prepare(self, tmp):
        folder = Path(tmp)/'task'
        source = Path(tmp)/'image.png'
        Image.new('RGB',(640,480),'white').save(source)
        state = start(source,folder,True,goal='roads')
        state.update(extracted=True,centerlined=True,registered=True,
                     registration_gate_passed=True,map_download_requested=True)
        (folder/'registration').mkdir()
        geo = {'type':'FeatureCollection','features':[{'type':'Feature',
               'properties':{'coordinate_system':'WGS84'},
               'geometry':{'type':'LineString','coordinates':[[116.0,39.0],[116.001,39.001]]}}]}
        (folder/'registration/registered-segments.geojson').write_text(json.dumps(geo))
        return state, folder

    def test_explicit_local_fixture_to_candidates_without_network(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp:
            # 在测试临时目录生成合成OSM，不依赖gitignored产物，也不改examples。
            generator = Path(__file__).resolve().parent.parent/'examples/make_synthetic_roads.py'
            with contextlib.redirect_stdout(io.StringIO()):
                exec(compile(generator.read_text(), str(generator), 'exec'),
                     {'__file__': str(Path(tmp)/'fixture.py')})
            fixture = (Path(tmp)/'synthetic-roads.osm').read_text()
            # Generate the fixture locally without requiring a gitignored PNG.
            source = Path(tmp)/'image.png'
            image = Image.new('RGB',(640,480),'white')
            draw = ImageDraw.Draw(image)
            draw.line([(120,360),(280,360),(280,200),(280,120),(200,120)],fill='#00BFA5',width=7)
            draw.line([(280,200),(520,200)],fill='#00BFA5',width=7)
            image.save(source)
            folder = Path(tmp)/'task'
            state = start(source,folder,True,'roads')
            state['map_provider'] = 'osm'
            state['messages'].append({'role':'user','content':json.dumps({'crop':[0,0,640,480],'color':'#00BFA5'})})
            state = run(state,folder,DemoPlanner())
            (folder/'controls.json').write_text((Path(__file__).resolve().parent.parent/'examples/synthetic-controls.json').read_text())
            state['controls_crop'] = state['extraction_config']['crop']
            state = run(state,folder,DemoPlanner())
            self.assertTrue(state['registration_gate_passed'])
            (folder/'map.osm').write_text(fixture)
            with patch('map_fetch._download',side_effect=AssertionError('本地匹配不得联网')) as network:
                state = run(state,folder,DemoPlanner(),consent_map=True)
                network.assert_not_called()
            self.assertEqual(state['status'],'needs_confirmation')
            report=json.loads((folder/'roads/road-match.json').read_text())
            self.assertEqual(report['matched_segment_count'],report['input_segment_count'])
            self.assertEqual(report['missing_segment_indices'],[])
            self.assertEqual(report['coverage_fraction'],1)
            self.assertEqual(state['road_diagnostics']['coverage_fraction'],1)
            self.assertFalse(state['verified'])
            for artifact in state['artifacts']:
                self.assertTrue((folder/artifact).exists())

    def test_download_requires_fresh_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state,folder = self.prepare(tmp)
            with patch('ride_agent.fetch_roads') as fetch:
                result=run(state,folder,DemoPlanner())
                self.assertEqual(result['status'],'waiting_for_user')
                fetch.assert_not_called()
                self.assertTrue(result['map_download_requested'])

    def test_old_download_request_never_queries_even_with_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, folder = self.prepare(tmp)
            with patch('ride_agent.fetch_roads') as fetch:
                result = run(state,folder,DemoPlanner(),budget=1,consent_map=True)
                fetch.assert_not_called()
            self.assertEqual(result['status'], 'waiting_for_user')
            self.assertIn('osm_relay/amap', result['question'])
            self.assertTrue(result['map_download_requested'])
            self.assertFalse((folder/'map.osm').exists())

    def test_download_rejects_model_coordinates(self):
        class Bad:
            def decide(self,*args):return {'action':'fetch_map','bounds':[0,0,1,1]}
        with tempfile.TemporaryDirectory() as tmp:
            state,folder = self.prepare(tmp)
            with patch('ride_agent.fetch_roads') as fetch:
                result=run(state,folder,Bad(),consent_map=True)
                fetch.assert_not_called()
                self.assertEqual(result['status'],'error')

    def test_download_rejects_bad_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            state,folder = self.prepare(tmp)
            state['registration_gate_passed'] = False
            with patch('ride_agent.fetch_roads') as fetch:
                result=run(state,folder,DemoPlanner(),consent_map=True)
            fetch.assert_not_called()
            self.assertEqual(result['status'],'waiting_for_user')
            self.assertFalse(result.get('match_attempted',False))

    def test_old_download_request_pauses_without_replaying(self):
        with tempfile.TemporaryDirectory() as tmp:
            state,folder = self.prepare(tmp)
            with patch('ride_agent.fetch_roads',side_effect=AssertionError('退休入口不得调用')) as fetch:
                result=run(state,folder,DemoPlanner(),consent_map=True)
            fetch.assert_not_called()
            self.assertEqual(result['status'],'waiting_for_user')
            self.assertTrue(result['map_download_requested'])
            self.assertFalse(result.get('match_attempted',False))

if __name__=='__main__': unittest.main()
