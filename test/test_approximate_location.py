"""所有经纬度为合成测试锚点，绝非真实截图定位或真实轨迹精度验收。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from approximate_location import approximate, fit_approximation
from register_pixels import fit_registration
from ride_agent import load, run, LocalPlanner, save, download_task_map
from road_match import _load_segments
from ride_web import artifact_current
import test_local_registration as local_fixtures
import test_ride_web as web_fixtures


def anchors():
    return {'mode':'approximate', 'coordinate_system':'WGS84', 'pixel_frame':'local_mask',
            'synthetic':True, 'uncertainty_note':'合成锚点，仅测试；不是实际位置或统计误差上界。',
            'anchors':[{'pixel_xy':[x,y], 'wgs84':[120+x*.00001,30-y*.00001],
                        'source':'synthetic fixture, not real evidence', 'uncertainty_m':100}
                       for x,y in [(0,0),(70,0),(0,50)]]}


class ApproximateMathTests(unittest.TestCase):
    def test_metadata_residuals_extrapolation_and_road_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'a.json').write_text(json.dumps(anchors()))
            (p/'g.json').write_text(json.dumps({'coordinate_system':'local_mask_pixels_xy_top_left',
                                             'segments_xy':[[[1,1],[65,45],[100,100]]]}))
            result = approximate(p/'g.json',p/'a.json',p/'out')
            self.assertTrue(result['approximate'])
            for key in ('verified','original_gps','navigable','independent_error_gate_passed','strict_registration_eligible'):
                self.assertFalse(result[key])
            self.assertEqual(result['vertices_outside_anchor_hull'],2)
            self.assertEqual(result['vertices_outside_control_bbox'],1)
            self.assertEqual(result['anchor_uncertainty_max_m'],100)
            self.assertEqual(result['anchors'],anchors()['anchors'])
            self.assertLess(result['fit_rmse_m'],1e-7)
            geo = json.loads((p/'out/approximate-segments.geojson').read_text())
            self.assertEqual(geo['metadata'],result)
            self.assertTrue(geo['features'][0]['properties']['approximate'])
            with self.assertRaises(ValueError): _load_segments(p/'out/approximate-segments.geojson')
            # 即使删掉顶层标记，逐分段标记仍拒绝。
            geo.pop('approximate'); (p/'geo.json').write_text(json.dumps(geo))
            with self.assertRaises(ValueError): _load_segments(p/'geo.json')

    def test_invalid_evidence_and_coordinate_frames(self):
        for invalid in ([], None, 'not object'):
            with self.assertRaises(ValueError): fit_approximation(invalid)
        for kind in ('source','uncertainty','nan','boolean','note','crs','frame','few','collinear','duplicate'):
            s = anchors()
            if kind == 'source': s['anchors'][0]['source']=''
            if kind == 'uncertainty': s['anchors'][0]['uncertainty_m']=0
            if kind == 'nan': s['anchors'][0]['uncertainty_m']=float('nan')
            if kind == 'boolean': s['anchors'][0]['uncertainty_m']=True
            if kind == 'note': s.pop('uncertainty_note')
            if kind == 'crs': s['coordinate_system']='GCJ02'
            if kind == 'frame': s['pixel_frame']='original_image'
            if kind == 'few': s['anchors']=s['anchors'][:2]
            if kind == 'collinear': s['anchors'][2]['pixel_xy']=[10,0]
            if kind == 'duplicate': s['anchors'][2]['pixel_xy']=[0,0]
            with self.subTest(kind=kind), self.assertRaises(ValueError): fit_approximation(s)

    def test_strict_rejects_approximate_even_with_strict_points(self):
        from test_register_pixels import spec
        s=spec(); s['mode']='approximate'
        with self.assertRaises(ValueError): fit_registration(s)


class ApproximateCLITests(unittest.TestCase):
    setUp = local_fixtures.LocalRegistrationTests.setUp
    cli = local_fixtures.LocalRegistrationTests.cli

    def test_cli_export_without_model_then_strict_transition(self):
        f = self.root/'anchors.json'; f.write_text(json.dumps(anchors()))
        with patch('ride_agent.APIPlanner',side_effect=AssertionError('no model')):
            result = self.cli('--approximate-anchors-file',str(f))
        self.assertEqual(result.returncode,0,result.stderr)
        state = load(self.folder)
        self.assertEqual(state['goal'],'approximate')
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertFalse(state['registered']); self.assertFalse(state['registration_gate_passed'])
        self.assertEqual(state['execution'],{'source':'local','api_responses':0})
        self.assertIn('approximation/approximate-segments.geojson',state['artifacts'])
        with self.assertRaises(ValueError): download_task_map(state,self.folder)
        (self.root/'map.osm').write_text('<osm version="0.6"/>')
        for extra in [('--osm-file',str(self.root/'map.osm')),('--fetch-map','--consent-map')]:
            blocked=self.cli(*extra)
            self.assertEqual(blocked.returncode,2,blocked.stdout)
            self.assertEqual(load(self.folder)['goal'],'approximate')
        result=self.cli('--controls-file',str(self.controls))
        self.assertEqual(result.returncode,0,result.stderr)
        state=load(self.folder)
        self.assertEqual(state['goal'],'registration'); self.assertTrue(state['registration_gate_passed'])
        self.assertFalse(artifact_current(state,'approximation/approximate-segments.geojson'))

    def test_missing_invalidated_or_other_crop_never_exports(self):
        for kind in ('missing','invalidated','other_crop'):
            state = copy.deepcopy(self.state)
            state['goal'] = 'approximate'
            if kind != 'missing':
                (self.folder/'anchors.json').write_text(json.dumps(anchors()))
                state['anchors_crop'] = [0,0,80,60]
            if kind == 'invalidated': state['anchors_invalidated'] = True
            if kind == 'other_crop': state['anchors_crop'] = [1,0,80,60]
            result = run(state,self.folder,LocalPlanner())
            self.assertFalse(result.get('approximate_complete',False))
            self.assertEqual(result['status'],'waiting_for_user')
            self.assertFalse((self.folder/'approximation/approximate-segments.geojson').exists())

    def test_model_cannot_inject_anchors_or_bypass_strict_mode(self):
        class BadModel:
            def decide(self, *_):
                return {'action':'approximate', 'anchors':anchors()['anchors']}
        # 未切换approximate goal，即使有当前锚点也不能通过模型动作进入近似模式。
        (self.folder/'anchors.json').write_text(json.dumps(anchors()))
        self.state['anchors_crop'] = [0,0,80,60]
        result = run(self.state,self.folder,BadModel())
        self.assertEqual(result['status'],'error')
        self.assertFalse(result.get('approximate_complete',False))
        self.assertFalse((self.folder/'approximation/approximate-segments.geojson').exists())

    def test_crop_invalidates_anchors(self):
        f = self.root/'anchors.json'; f.write_text(json.dumps(anchors()))
        self.assertEqual(self.cli('--approximate-anchors-file',str(f)).returncode,0)
        result=self.cli('--crop','1','0','80','60')
        self.assertEqual(result.returncode,0,result.stderr)
        state=load(self.folder)
        self.assertFalse(state['approximate_complete']); self.assertTrue(state['anchors_invalidated'])
        self.assertEqual(state['status'],'waiting_for_user')
        self.assertFalse(artifact_current(state,'approximation/approximate-segments.geojson'))


class ApproximateHTTPTests(unittest.TestCase):
    setUp=web_fixtures.WebTests.setUp
    tearDown=web_fixtures.WebTests.tearDown
    request=web_fixtures.WebTests.request

    def test_real_loopback_cli_upload_and_download(self):
        import base64, io
        image=Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(image).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        image_bytes=io.BytesIO(); image.save(image_bytes,format='PNG')
        code,_,body=self.request('/api/tasks',{'mode':'demo','goal':'approximate',
            'image_base64':base64.b64encode(image_bytes.getvalue()).decode()})
        self.assertEqual(code,201,body)
        task=json.loads(body)['task_id']
        code,_,body=self.request('/api/tasks/'+task+'/resume',{'local_only':True,
            'color':'#00BFA5','crop':[0,0,80,60]})
        self.assertEqual(code,200,body)
        code,_,body=self.request('/api/tasks/'+task+'/resume',{'local_only':True,
            'approximate_anchors_json':json.dumps(anchors())})
        self.assertEqual(code,200,body)
        state=json.loads(body)
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertTrue(state['approximate_complete'])
        self.assertFalse(load(self.root/task/'task')['registered'])
        a=next(a for a in state['artifacts'] if a['name']=='approximation/approximate-segments.geojson')
        self.assertTrue(a['current'])
        code,_,body=self.request(a['url'])
        self.assertEqual(code,200)
        geo=json.loads(body); self.assertTrue(geo['approximate']); self.assertFalse(geo['navigable'])
        self.assertEqual(geo['metadata']['anchors'],anchors()['anchors'])
        code,_,body=self.request('/api/tasks/'+task+'/resume',{'local_only':True,
            'approximate_anchors_json':json.dumps(anchors()),'controls_json':'{}'})
        self.assertEqual(code,400,body)
        code,_,body=self.request('/')
        self.assertIn(b'locationMode',body); self.assertIn(b'approximate_anchors_json',body)

if __name__=='__main__': unittest.main()
