"""真实APIPlanner请求构造/动作执行，网络仅mock；不验证模型识别能力。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from ride_agent import APIPlanner, ConsentedPlanner, LocalPlanner, start, run, delivery_requirements, invalidate_extraction
import route_review


def current_evidence(request):
    """mock必须从实际请求取版本，并解码原图及当前叠图；不模拟视觉正确性。"""
    import base64
    import io
    messages = request['messages']
    current_content = messages[-1]['content']
    current = json.loads(current_content[0]['text'].split('CURRENT_STAGE_STATE ')[1].split('. Earlier')[0])
    originals = messages[1]['content']
    for blocks in (originals, current_content):
        for item in blocks:
            if item['type'] == 'image_url':
                with Image.open(io.BytesIO(base64.b64decode(item['image_url']['url'].split(',', 1)[1]))) as image:
                    image.load()
    return current, current_content


def mock_observe(request):
    current, content = current_evidence(request)
    assert current['extracted'] and current['centerlined'] and current['review_version']
    assert sum(item['type'] == 'image_url' for item in content) >= 2
    crop = current['extraction_diagnostics']['crop_xyxy']
    region = [crop[0], crop[1], min(crop[2], crop[0]+512), min(crop[3], crop[1]+512)]
    return {'action':'observe_region', 'region':region, 'hypothesis':'uncertain',
            'reason':'mock协议验收：请求当前原图及骨架局部证据，不测试真实模型识别'}


def mock_review(request):
    current, content = current_evidence(request)
    evidence = next(item['text'] for item in content if item.get('text', '').startswith('LOCAL_REVIEW_EVIDENCE '))
    observation = json.loads(evidence.removeprefix('LOCAL_REVIEW_EVIDENCE '))
    assert observation['version'] == current['review_version']
    assert sum(item['type'] == 'image_url' for item in content) >= 5
    return {'action':'review_route', 'version':current['review_version'], 'result':'consistent',
            'observations':[{'region':observation['region_original_xyxy'], 'hypothesis':'uncertain',
                             'evidence':'mock确认当前版本三类局部图已进入请求；非真实模型视觉结论'}],
            'limitations':'仅离线mock编排验收，不证明真实识别、完整性或行进顺序'}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root/'source.png'
        image = Image.new('RGB', (100, 80), 'white')
        ImageDraw.Draw(image).line([(20, 50), (70, 50), (70, 20)], fill='#00BFA5', width=7)
        image.save(source)
        self.folder = self.root/'task'
        self.state = start(source, self.folder)
        self.requests = []

    def execute(self, actions, budget=30, planner=None):
        # 每条复核先实际呈现其局部图；旧版本/假版本测试仍由服务端拒绝。
        expanded = []
        for action in actions:
            if action.get('action') == 'review_route':
                expanded.append({'action':'observe_region', 'region':action['observations'][0]['region'],
                                 'hypothesis':action['observations'][0]['hypothesis'], 'reason':'先读取1:1证据'})
            expanded.append(action)
        actions = expanded
        test = self
        class Response:
            def __init__(self, action): self.action = action
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args):
                return json.dumps({'choices':[{'message':{'content':json.dumps(self.action)}}]}).encode()
        def respond(request, **kwargs):
            body = json.loads(request.data)
            index = len(test.requests)
            test.requests.append(body)
            current = json.loads(body['messages'][-1]['content'][0]['text'].split('CURRENT_STAGE_STATE ')[1].split('. Earlier')[0])
            action = dict(actions[min(index, len(actions)-1)])
            if action.get('version') == 'CURRENT': action['version'] = current['review_version']
            return Response(action)
        with patch.dict(os.environ, {'RIDE_API_KEY':'mock', 'RIDE_MODEL':'mock', 'RIDE_API_BASE':'https://mock.invalid/v1'}), patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = respond
            return run(self.state, self.folder, planner or APIPlanner(), budget=budget)

    def extract(self, crop=None):
        return {'action':'extract', 'crop':crop or [0,0,100,80], 'color':'#00BFA5'}

    def review(self, result='consistent', version='CURRENT'):
        return {'action':'review_route', 'version':version, 'result':result,
                'observations':[{'region':[20,20,80,60], 'hypothesis':'turnback', 'evidence':'原图与骨架局部对比，保留可见拐角，无法推断行进方向'}],
                'limitations':'仅可见像素一致性，完整性和重复顺序未验证，不可导航'}

    def test_actual_planner_local_evidence_reextract_review_deliver(self):
        actions = [self.extract([10,10,90,70]), {'action':'centerline'}, self.review('needs_revision'),
                   {'action':'observe_region','region':[20,20,80,60], 'hypothesis':'two_arms','reason':'核对拐角内侧，避免填平回转孔'},
                   self.extract(), {'action':'centerline'}, self.review(), {'action':'deliver','summary':'候选与可见证据一致，仍未验证真实顺序'}]
        result = self.execute(actions)
        self.assertEqual(result['status'], 'needs_confirmation', result)
        self.assertFalse(result['verified'])
        self.assertFalse(result['original_gps'])
        self.assertEqual(len(self.requests), 10)
        local = self.requests[5]['messages'][-1]['content']
        evidence = next(x['text'] for x in local if 'LOCAL_REVIEW_EVIDENCE' in x.get('text',''))
        metadata = json.loads(evidence.removeprefix('LOCAL_REVIEW_EVIDENCE '))
        self.assertEqual(metadata['extraction_origin_xy'], [10,10])
        self.assertEqual(metadata['region_original_xyxy'], [20,20,80,60])
        self.assertEqual(sum(x['type']=='image_url' for x in local), 5)
        # 检查真正进入请求的PNG像素，而非只检查image_url字段存在。
        import base64
        import io
        payloads = [x['image_url']['url'] for x in local if x['type']=='image_url'][-3:]
        for relative, url in zip(('input.png','extraction/overlay.png','centerline/graph-overlay.png'), payloads):
            with Image.open(io.BytesIO(base64.b64decode(url.split(',', 1)[1]))) as actual:
                with Image.open(self.folder/relative) as source:
                    # 此时磁盘已是第二次全图提取；第一轮局部图需使用原图偏移验证原图。
                    if relative == 'input.png':
                        expected = source.convert('RGB').crop((20,20,80,60))
                        self.assertEqual(actual.tobytes(), expected.tobytes())
                self.assertEqual(actual.size, (60,40))
        for request in self.requests:
            self.assertEqual(request['messages'][1]['content'][1]['type'], 'image_url')
        self.assertTrue(route_review.approved(result,self.folder))
        # 原图与当前掩码/中心线文件共同绑定；旁路改文件也令交付失效。
        with (self.folder/'extraction/mask.png').open('ab') as stream: stream.write(b'changed')
        self.assertIsNotNone(delivery_requirements(result,self.folder))

    def test_deliver_without_review_is_bounded_rejected(self):
        result = self.execute([self.extract(), {'action':'centerline'}, {'action':'deliver','summary':'错误提前交付'}])
        self.assertEqual(result['status'],'paused')
        self.assertEqual(len(self.requests),5)
        self.assertNotIn('summary', result)

    def test_ambiguous_review_cannot_deliver_then_ask(self):
        result = self.execute([self.extract(), {'action':'centerline'}, self.review('ambiguous'),
                               {'action':'deliver','summary':'不应交付'}, {'action':'ask','question':'该区域是同路往返还是两条线臂？'}])
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertNotIn('summary',result)

    def test_cannot_reverse_ambiguity_without_new_evidence(self):
        result = self.execute([self.extract(), {'action':'centerline'}, self.review('ambiguous'),
                               self.review(), {'action':'deliver','summary':'错误改口'},
                               {'action':'ask','question':'请解释重复路段顺序'}])
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertEqual(result['route_review']['result'],'ambiguous')
        self.assertNotIn('summary',result)

    def test_reextract_invalidates_old_version_even_identical_pixels(self):
        self.execute([self.extract(), {'action':'centerline'}, self.review(), {'action':'ask','question':'继续？'}])
        self.assertIn('route_review',self.state)
        invalidate_extraction(self.state)
        self.assertNotIn('route_review',self.state)
        self.assertNotIn('review_presented_version',self.state)

    def test_no_progress_extraction_budget_stops(self):
        result = self.execute([self.extract()])
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertEqual(len(self.requests),4)
        self.assertNotIn('summary',result)

    def test_review_budget_and_total_budget_stop_ask(self):
        observe = {'action':'observe_region','region':[20,20,80,60], 'hypothesis':'uncertain','reason':'核对'}
        result = self.execute([self.extract(), {'action':'centerline'}, observe])
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertEqual(len(self.requests),9)
        self.assertNotIn('summary',result)

    def test_forged_review_before_presentation_rejected(self):
        result = route_review.record(self.state,self.folder,self.review(version='fake'))
        self.assertEqual(result['status'],'rejected')
        self.assertNotIn('route_review',self.state)

    def test_consented_pipeline_no_automatic_delivery_bypass(self):
        result = self.execute([self.extract(), self.review(), {'action':'deliver','summary':'候选'}], planner=ConsentedPlanner())
        self.assertEqual(result['status'],'needs_confirmation',result)
        self.assertEqual(len(self.requests),4)
        self.assertTrue(route_review.approved(result,self.folder))

    def test_local_only_offline_no_fake_review(self):
        self.state['user_extraction_constraints'] = {'color':'#00BFA5','crop':[0,0,100,80]}
        with patch('ride_agent.urllib.request.build_opener', side_effect=AssertionError('离线不允许网络')):
            result = run(self.state,self.folder,LocalPlanner())
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertIn('未经过语义模型复核',result['summary'])
        self.assertNotIn('route_review',result)

    def test_current_version_and_invented_region_without_local_evidence_rejected(self):
        self.execute([self.extract(), {'action':'centerline'}, {'action':'ask','question':'检查'}])
        action = self.review(version=route_review.version(self.state, self.folder))
        result = route_review.record(self.state, self.folder, action)
        self.assertEqual(result['status'], 'rejected')
        self.assertNotIn('route_review', self.state)
        self.assertIsNotNone(delivery_requirements(self.state, self.folder))

    def test_identical_reextract_cannot_erase_ambiguity(self):
        result = self.execute([self.extract(), {'action':'centerline'}, self.review('ambiguous'),
                               self.extract(), {'action':'centerline'}, self.review(),
                               {'action':'deliver','summary':'不应成功'}, {'action':'ask','question':'请补充歧义说明'}])
        self.assertEqual(result['status'], 'waiting_for_user')
        self.assertNotIn('summary', result)
        self.assertFalse(route_review.approved(result, self.folder))

    def test_user_parameter_change_revokes_review_and_regions(self):
        from ride_agent import apply_extraction_correction
        self.execute([self.extract(), {'action':'centerline'}, self.review(), {'action':'ask','question':'检查'}])
        self.assertTrue(route_review.approved(self.state, self.folder))
        apply_extraction_correction(self.state, color='#00BEA4')
        self.assertNotIn('review_presented_regions', self.state)
        self.assertNotIn('route_review', self.state)
        self.assertIsNotNone(delivery_requirements(self.state, self.folder))

    def test_failed_transport_does_not_mark_evidence_presented(self):
        self.execute([self.extract(), {'action':'centerline'}, {'action':'ask','question':'检查'}])
        self.state.pop('review_presented_version', None)
        with patch.dict(os.environ, {'RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}), \
             patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = TimeoutError()
            result = run(self.state, self.folder, APIPlanner())
        self.assertEqual(result['error_category'], 'api_timeout')
        self.assertNotIn('review_presented_version', self.state)

    def test_coordinates_bounds_and_no_arbitrary_paths(self):
        for region in ([False,0,10,10],[-1,0,10,10],[0,0,101,80]):
            with self.assertRaises(ValueError): route_review.region_box(region,[100,80])
        with self.assertRaises(ValueError):
            route_review.observe(self.state,self.folder,{'action':'observe_region','region':[0,0,10,10], 'hypothesis':'turnback','reason':'看图','path':'/etc/passwd'})


if __name__ == '__main__': unittest.main()
