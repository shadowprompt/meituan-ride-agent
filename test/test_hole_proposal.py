"""仅loopback HTTP mock编排；不验收真实视觉语义。"""
import copy
import json
import os
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
import test_candidate_review as candidate_fixtures
from test_candidate_review import observe_candidate, choose_candidate
from test_route_review import current_evidence, mock_observe, mock_review
from ride_agent import load, save
import hole_proposal


def observe_after(request):
    current, _ = current_evidence(request)
    return {'action':'observe_region','region':current['extraction_config']['crop'],
            'hypothesis':'occlusion','reason':'重新观察重建后的原图与掩码/中心线'}


def propose(request):
    current, content = current_evidence(request)
    evidence = json.loads(next(x['text'][len('HOLE_LOCAL_EVIDENCE '):] for x in content if x.get('text','').startswith('HOLE_LOCAL_EVIDENCE ')))
    assert current['centerlined']
    assert any(x['type']=='image_url' for x in content)
    return {'action':'propose_holes', **{k:evidence[k] for k in ('review_id','fingerprint','hashes')},
            'holes':[{'id':1,'kind':'arrow','reason':'mock白色箭头位于灰带内；这不是视觉真值'}]}


class HoleProposalTests(unittest.TestCase):
    setUp = candidate_fixtures.CandidateHTTPTests.setUp
    tearDown = candidate_fixtures.CandidateHTTPTests.tearDown
    request = candidate_fixtures.CandidateHTTPTests.request
    model = candidate_fixtures.CandidateHTTPTests.model
    create_image = candidate_fixtures.CandidateHTTPTests.create_image
    def prepared_proposal(self):
        image=Image.new('RGB',(160,120),'white');d=ImageDraw.Draw(image)
        d.rectangle((10,60,110,90), fill='#00BFA5');d.rectangle((35,70,38,73),fill='white')
        d.rectangle((125,10,145,30),fill='#00BFA5')
        code,data,calls=self.create_image(image,[{'action':'extract','color':'#00BFA5','crop':[0,0,160,120]},
            observe_candidate, choose_candidate, {'action':'centerline'}, mock_observe,
            self.needs_revision, {'action':'observe_holes'}, propose])
        self.assertEqual(code,201,data);self.assertEqual(data['hole_proposal']['status'],'pending',data)
        return data,self.root/data['task_id']/'task',calls

    @staticmethod
    def needs_revision(request):
        result=mock_review(request);result['result']='needs_revision';return result

    def decision(self,data,decision='approve'):
        return {'proposal_id':data['hole_proposal']['proposal_id'],'decision':decision,
                'confirm_source_reviewed':True,'reason':'人工mock核对原图；只用于协议验收'}

    def test_approve_rebuild_then_reobserve_and_deliver(self):
        data,p,calls=self.prepared_proposal();before=(p/'extraction/mask.png').read_bytes()
        self.assertEqual(len(calls),8)
        ep='/api/tasks/'+data['task_id']+'/holes/decide';selection=self.decision(data)
        self.assertEqual(self.request(ep,dict(selection,confirm_source_reviewed=False))[0],409)
        self.assertEqual((p/'extraction/mask.png').read_bytes(),before)
        code,_,body=self.request(ep,selection);self.assertEqual(code,200,body)
        self.assertNotEqual((p/'extraction/mask.png').read_bytes(),before)
        state=load(p);self.assertNotIn('route_review',state);self.assertEqual(state['hole_correction']['added_pixels'],16)
        self.assertEqual(self.request(ep,selection)[0],409)
        server,captured=self.model([observe_after,mock_review,{'action':'deliver','summary':'mock重建后复核；未验证候选，不可导航'}])
        with patch.dict(os.environ,{'RIDE_API_BASE':f'http://127.0.0.1:{server.server_port}/v1','RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
            code,_,body=self.request('/api/tasks/'+data['task_id']+'/resume',{'consent_upload':True,'answer':'请重新观察补孔后的当前版本并复核'})
        self.assertEqual(code,200,body);result=json.loads(body)
        self.assertEqual(result['status'],'needs_confirmation',result)
        self.assertFalse(result['verified']);self.assertFalse(result['original_gps']);self.assertEqual(len(captured),3)

    def test_reject_no_change_repeat_and_security(self):
        data,p,_=self.prepared_proposal();before=(p/'extraction/mask.png').read_bytes()
        ep='/api/tasks/'+data['task_id']+'/holes/decide';selection=self.decision(data,'reject')
        for headers in ({'X-CSRF-Token':'bad'},{'Origin':'https://evil.test'}):
            self.assertEqual(self.request(ep,selection,headers=headers)[0],403)
        self.assertEqual(self.request(ep,dict(selection,fill_hole_ids=[2]))[0],400)
        self.assertEqual(self.request(ep,dict(selection,proposal_id='wrong'))[0],409)
        other,other_folder,_=self.prepared_proposal()
        self.assertEqual(self.request('/api/tasks/'+other['task_id']+'/holes/decide',selection)[0],409)
        self.assertEqual(self.request('/api/tasks/'+data['task_id']+'/resume',{'local_only':True,'color':'#ffffff'})[0],409)
        self.assertEqual(self.request('/api/tasks/'+data['task_id']+'/holes/preview',{})[0],409)
        self.assertEqual(self.request(ep,selection)[0],200)
        self.assertEqual((p/'extraction/mask.png').read_bytes(),before)
        state=load(p);a={'action':'propose_holes',**{k:state['hole_proposal'][k] for k in ('review_id','fingerprint','hashes','holes')}}
        with self.assertRaises(ValueError):hole_proposal.propose(state,p,a)
        self.assertEqual(load(p)['hole_proposal']['user_reason'],selection['reason'])

    def test_stale_and_missing_evidence_invalid_ids_and_symlink(self):
        data,p,_=self.prepared_proposal();state=load(p)
        a={'action':'propose_holes',**{k:state['hole_proposal'][k] for k in ('review_id','fingerprint','hashes','holes')}}
        state['hole_proposal_seen']=[]
        for bad in ([True],[99],[1,1]):
            action=copy.deepcopy(a);action['holes']=[{'id':i,'kind':'arrow','reason':'测试'} for i in bad]
            with self.assertRaises(ValueError):hole_proposal.propose(state,p,action)
        state.pop('hole_presented')
        with self.assertRaises(ValueError):hole_proposal.propose(state,p,a)
        state['hole_presented']=state['hole_observation'];state['hole_observation']['holes'][0]['eligible']=False
        with self.assertRaises(ValueError):hole_proposal.propose(state,p,a)
        # 中心线、source、mask任一改变均拒绝；当前测试改变中心线文件。
        graph=p/'centerline/pixel-graph.json';old=graph.read_bytes();graph.write_bytes(old+b' ')
        ep='/api/tasks/'+data['task_id']+'/holes/decide'
        self.assertEqual(self.request(ep,self.decision(data))[0],409);graph.write_bytes(old)
        for name in ('extraction/source-crop.png','extraction/mask.png'):
            path=p/name;old=path.read_bytes();path.write_bytes(old+b' ')
            self.assertEqual(self.request(ep,self.decision(data))[0],409);path.write_bytes(old)
        # 真实生成超面积/超最长边封闭孔，不能只信模型提供的id或bbox。
        import numpy as np
        from review_mask_holes import enclosed_holes
        mask=np.ones((70,70),dtype=bool);mask[5:25,5:25]=False;mask[40:41,5:40]=False
        actual=enclosed_holes(mask)
        self.assertEqual([h['eligible'] for h in actual],[False,False])
        state['hole_observation']['holes']=actual
        with self.assertRaises(ValueError):hole_proposal.propose(state,p,a)
        source=p/'extraction/source-crop.png';source.unlink();source.symlink_to(p/'extraction/overlay.png')
        self.assertEqual(self.request(ep,self.decision(data))[0],403)
