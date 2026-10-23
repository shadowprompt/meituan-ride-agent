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
            self.assertIn('Unknown action',state['error'])

    def test_no_false_delivery(self):
        class Bad:
            def decide(self,*args):return {'action':'deliver','summary':'finished'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Bad())
            self.assertEqual(state['status'],'error')

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
            controls=json.loads(Path('examples/synthetic-controls.json').read_text())
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
            self.assertIn('User-supplied',state['error'])

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
            self.assertEqual(state['status'],'error')
            self.assertIn('matching attempt',state['error'])

    def test_bad_registration_blocks_roads(self):
        class Attempt:
            def decide(self,*args):return {'action':'match'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True,goal='roads')
            state.update(registered=True,registration_gate_passed=False)
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

    def test_complete_mock_download_to_candidates(self):
        import xml.etree.ElementTree as ET
        root = ET.parse('examples/synthetic-roads.osm').getroot()
        elements = []
        for n in root.findall('node'):
            elements.append(dict(type='node', id=int(n.get('id')),
                                 lon=float(n.get('lon')), lat=float(n.get('lat'))))
        for w in root.findall('way'):
            elements.append(dict(type='way', id=int(w.get('id')),
                                 nodes=[int(n.get('ref')) for n in w.findall('nd')],
                                 tags={t.get('k'):t.get('v') for t in w.findall('tag')}))
        with tempfile.TemporaryDirectory() as tmp:
            # Generate the fixture locally without requiring a gitignored PNG.
            source = Path(tmp)/'image.png'
            image = Image.new('RGB',(640,480),'white')
            draw = ImageDraw.Draw(image)
            draw.line([(120,360),(280,360),(280,200),(280,120),(200,120)],fill='#00BFA5',width=7)
            draw.line([(280,200),(520,200)],fill='#00BFA5',width=7)
            image.save(source)
            folder = Path(tmp)/'task'
            state = start(source,folder,True,'roads')
            state['messages'].append({'role':'user','content':json.dumps({'crop':[0,0,640,480],'color':'#00BFA5'})})
            state = run(state,folder,DemoPlanner())
            (folder/'controls.json').write_text(Path('examples/synthetic-controls.json').read_text())
            state['controls_crop'] = state['extraction_config']['crop']
            state = run(state,folder,DemoPlanner())
            self.assertTrue(state['registration_gate_passed'])
            state['map_download_requested'] = True
            with patch('map_fetch._download',return_value=json.dumps({'elements':elements}).encode()) as network:
                state = run(state,folder,DemoPlanner(),consent_map=True)
                network.assert_called_once()
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

    def test_download_uses_only_local_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            state,folder = self.prepare(tmp)
            def fetched(bounds, path, *, consent_download=False):
                self.assertTrue(consent_download)
                self.assertAlmostEqual(bounds[0],115.998)
                self.assertAlmostEqual(bounds[3],39.003)
                path.write_text(Path('examples/synthetic-roads.osm').read_text())
                return {'bounds':bounds,'node_count':10}
            with patch('ride_agent.fetch_roads',side_effect=fetched) as fetch:
                result=run(state,folder,DemoPlanner(),budget=1,consent_map=True)
                fetch.assert_called_once()
                self.assertFalse(result['map_download_requested'])
                self.assertEqual(result['status'],'paused')
                self.assertIn('map_download',load(folder))

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
            state['registration_gate_passed']=False
            with patch('ride_agent.fetch_roads') as fetch:
                result=run(state,folder,DemoPlanner(),consent_map=True)
                fetch.assert_not_called()
                self.assertEqual(result['status'],'error')

    def test_failed_download_keeps_retry_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state,folder = self.prepare(tmp)
            with patch('ride_agent.fetch_roads',side_effect=ValueError('HTTP 429; retry later')):
                result=run(state,folder,DemoPlanner(),consent_map=True)
                self.assertEqual(result['status'],'error')
                self.assertTrue(result['map_download_requested'])
                self.assertFalse(result.get('match_attempted',False))

if __name__=='__main__': unittest.main()
