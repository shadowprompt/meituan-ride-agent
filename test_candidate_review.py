"""候选语义闭环HTTP mock；只验证协议/证据/网页，不声称真实视觉识别。"""
import base64
import io
import json
import os
from pathlib import Path
import unittest
from PIL import Image, ImageDraw
import test_api_start_e2e as http_fixtures
from test_route_review import current_evidence, mock_observe
import candidate_review
from ride_agent import apply_component_selection, start, run, LocalPlanner


def observe_candidate(request):
    state, _ = current_evidence(request)
    assert state['component_selection']['requires_selection']
    crop = state['extraction_diagnostics']['crop_xyxy']
    return {'action':'observe_candidates','region':[crop[0],crop[1],min(crop[2],crop[0]+512),min(crop[3],crop[1]+512)],'reason':'核对当前路线片段与相邻图标，不能自动全选'}


def choose_candidate(request):
    state, content = current_evidence(request)
    evidence = json.loads(next(x['text'].removeprefix('CANDIDATE_LOCAL_EVIDENCE ') for x in content if x.get('text','').startswith('CANDIDATE_LOCAL_EVIDENCE ')))
    assert evidence['version'] == state['candidate_version']
    images = [x for x in content if x['type']=='image_url'][-3:]
    for item in images:
        with Image.open(io.BytesIO(base64.b64decode(item['image_url']['url'].split(',',1)[1]))) as image:
            assert image.size == (evidence['region'][2]-evidence['region'][0],evidence['region'][3]-evidence['region'][1])
    return {'action':'select_candidates','version':state['candidate_version'], 'decisions':[
        {'id':c['id'],'keep':c['id']==1,'reason':'mock目标长线候选' if c['id']==1 else 'mock旁侧实心图标；不是目标路线'} for c in state['component_selection']['candidates']]}


def ambiguous_review(request):
    state, content = current_evidence(request)
    observation = json.loads(next(x['text'].removeprefix('LOCAL_REVIEW_EVIDENCE ') for x in content if x.get('text','').startswith('LOCAL_REVIEW_EVIDENCE ')))
    return {'action':'review_route','version':state['review_version'],'result':'ambiguous','observations':[
        {'region':observation['region_original_xyxy'],'hypothesis':'occlusion','evidence':'mock局部起点遮挡无法判断是否连通；不连断点'}], 'limitations':'mock不能证明真实视觉识别，需具体追问'}


def specific_question(request):
    state, content = current_evidence(request)
    region = state['route_review']['observations'][0]['region']
    return {'action':'ask','region':region,'question':'左下起点图标附近的灰线是否被图标遮挡？请说明是否仍属目标片段；我不会自动连接断点。'}


class CandidateHTTPTests(unittest.TestCase):
    setUp = http_fixtures.APIStartE2ETests.setUp
    tearDown = http_fixtures.APIStartE2ETests.tearDown
    request = http_fixtures.APIStartE2ETests.request
    model = http_fixtures.APIStartE2ETests.model
    create_image = http_fixtures.APIStartE2ETests.create_image

    def test_candidate_http_to_centerline_review_specific_question(self):
        image = Image.new('RGB',(160,120),'white')
        draw = ImageDraw.Draw(image)
        draw.line([(15,95),(100,95),(100,15)],fill='#00BFA5',width=7)
        draw.rectangle((125,30,145,50),fill='#00BFA5')
        code,data,calls = self.create_image(image,[{'action':'extract','color':'#00BFA5','crop':[0,0,160,120]},
            observe_candidate,choose_candidate,mock_observe,ambiguous_review,specific_question])
        self.assertEqual(code,201,data)
        self.assertEqual(data['status'],'waiting_for_user',data)
        self.assertEqual(len(calls),6)
        self.assertEqual(data['component_selection']['source'],'model_unverified')
        self.assertEqual(data['component_selection']['selected_ids'],[1])
        self.assertEqual(data['candidate_decision']['decisions'][1]['keep'],False)
        self.assertFalse(data['verified'])
        self.assertEqual(data['route_review']['result'],'ambiguous')
        self.assertIn('起点图标',data['question'])
        self.assertEqual(data['question_evidence']['region'],[0,0,160,120])
        for name in ('extraction/question-source.png','extraction/question-overlay.png','centerline/pixel-graph.json'):
            self.assertTrue(any(a['name']==name and a['current'] for a in data['artifacts']))
        # 原图和候选1:1证据真实进入本地HTTP，不仅有文字宣称。
        content = calls[2]['messages'][-1]['content']
        images=[x for x in content if x['type']=='image_url'][-3:]
        with Image.open(io.BytesIO(base64.b64decode(images[0]['image_url']['url'].split(',',1)[1]))) as actual:
            self.assertEqual(actual.tobytes(),image.tobytes())

    def test_offline_still_pauses_before_candidate_decision(self):
        import tempfile
        with tempfile.TemporaryDirectory() as temp:
            image=Image.new('RGB',(160,120),'white');d=ImageDraw.Draw(image)
            d.line([(10,80),(90,80)], fill='#00BFA5',width=7);d.rectangle((120,20,140,40),fill='#00BFA5')
            source=Path(temp)/'source.png';image.save(source);folder=Path(temp)/'task'
            state=start(source,folder)
            state['user_extraction_constraints']={'crop':[0,0,160,120],'color':'#00BFA5'}
            run(state,folder,LocalPlanner())
            self.assertEqual(state['status'],'waiting_for_user');self.assertFalse(state['centerlined'])
            action={'action':'select_candidates','version':candidate_review.version(state,folder),'decisions':[
                {'id':c['id'],'keep':c['id']==1,'reason':'测试'} for c in state['component_selection']['candidates']]}
            self.assertEqual(candidate_review.select(state,folder,action)['status'],'rejected')
            observation=candidate_review.observe(state,folder,{'action':'observe_candidates','region':[0,0,160,120],'reason':'测试当前局部'})
            candidate_review.mark_presented(state,folder)
            # 候选文件旁路变化：旧hash不可重用。
            mask=folder/'extraction/candidates-mask.png';before=mask.read_bytes();mask.write_bytes(before+b'changed')
            self.assertEqual(candidate_review.select(state,folder,action)['status'],'rejected');mask.write_bytes(before)
            # 用户显式选择永远优先，模型不能覆盖。
            state['user_extraction_constraints']['component_ids']=[2]
            self.assertEqual(candidate_review.select(state,folder,action)['status'],'rejected')
            state['user_extraction_constraints'].pop('component_ids')
            # 只看到候选一小块也不得用来决定整条长分量。
            full = state['candidate_presented']
            state['candidate_presented'] = [dict(full[0], region=[0,0,130,85])]
            self.assertEqual(candidate_review.select(state,folder,action)['status'],'rejected')
            state['candidate_presented'] = full
            action['decisions'][0]['keep']=True
            result=candidate_review.select(state,folder,action)
            self.assertEqual(result['status'],'selected_unverified')
            self.assertNotIn('component_ids',state['user_extraction_constraints'])
            self.assertFalse(state['verified'])

    @unittest.skipUnless(os.environ.get('RIDE_TURNBACK_SCREENSHOT'),'需本地折返原图')
    def test_turnback_candidate_model_path_local_http(self):
        def observe_left(request):
            current,_=current_evidence(request)
            return {'action':'observe_candidates','region':[240,1140,752,1580],'reason':'核对顶部真实回转间隙与左部片段'}
        def observe_right(request):
            return {'action':'observe_candidates','region':[752,1140,1000,1580],'reason':'核对右斜箭头孔和图标，不做补孔'}
        def choose_all_reasoned(request):
            current,_=current_evidence(request)
            return {'action':'select_candidates','version':current['candidate_version'],'decisions':[
                {'id':c['id'],'keep':True,'reason':'仅mock保留该断裂片段以验证多分量协议；没有视觉正确性结论，不连接断点'} for c in current['component_selection']['candidates']]}
        with Image.open(os.environ['RIDE_TURNBACK_SCREENSHOT']) as opened:image=opened.convert('RGB')
        code,data,calls=self.create_image(image,[{'action':'extract','color':'#6A778B','crop':[240,1140,1000,1580]},
            observe_left,observe_right,choose_all_reasoned,mock_observe,ambiguous_review,specific_question])
        self.assertEqual(data['status'],'waiting_for_user',data)
        self.assertEqual(len(calls),7)
        self.assertEqual(data['component_selection']['status'],'model_candidate')
        self.assertEqual(data['route_review']['result'],'ambiguous')
        self.assertTrue(data['question_evidence'])

if __name__=='__main__':unittest.main()
