"""真实HTTP→CLI→loopback模型：候选本地应用后本次授权继续，不上传外网。"""
import hashlib
import json
import os
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from ride_agent import load, save
import test_api_start_e2e as http_fixtures
from test_route_review import current_evidence, mock_observe, mock_review
from test_candidate_review import ambiguous_review, specific_question
from test_hole_proposal import propose


class ContinueTests(unittest.TestCase):
    setUp = http_fixtures.APIStartE2ETests.setUp
    tearDown = http_fixtures.APIStartE2ETests.tearDown
    request = http_fixtures.APIStartE2ETests.request
    model = http_fixtures.APIStartE2ETests.model
    create_image = http_fixtures.APIStartE2ETests.create_image

    def prepared(self):
        image=Image.new('RGB',(160,120),'white');d=ImageDraw.Draw(image)
        d.rectangle((10,60,110,90),fill='#00BFA5');d.rectangle((35,70,38,73),fill='white')
        d.rectangle((125,10,145,30),fill='#00BFA5')
        code,data,calls=self.create_image(image,[{'action':'extract','color':'#00BFA5','crop':[0,0,160,120]},
            {'action':'ask','question':'请在网页选择候选1/2，不猜路线。'}])
        self.assertEqual(code,201,data);self.assertEqual(len(calls),2)
        self.assertTrue(data['component_selection']['requires_selection'])
        endpoint='/api/tasks/'+data['task_id']+'/resume'
        # 创建授权不继承；本地步骤即便错误配置也不初始化模型/联网。
        with patch.dict(os.environ,{'RIDE_API_BASE':'bad','RIDE_API_KEY':'','RIDE_MODEL':''}):
            code,_,body=self.request(endpoint,{'local_only':True,'component_ids':[1,2]})
        self.assertEqual(code,200,body);data=json.loads(body)
        self.assertEqual(data['execution'],{'source':'local','api_responses':0})
        self.assertEqual(data['review_status'],'not_reviewed');self.assertTrue(data['centerlined'])
        self.assertTrue(data['user_components_applied'])
        self.assertEqual(len(calls),2)
        return endpoint,data,self.root/data['task_id']/'task'

    def review(self,endpoint,actions):
        server,calls=self.model(actions)
        with patch.dict(os.environ,{'RIDE_API_BASE':f'http://127.0.0.1:{server.server_port}/v1','RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
            code,_,body=self.request(endpoint,{'review_continue':True,'consent_upload':True})
        self.assertEqual(code,200,body)
        return json.loads(body),calls

    def test_same_task_review_no_answer_no_reextract_and_legacy(self):
        endpoint,data,p=self.prepared();before=load(p)
        hashes={name:hashlib.sha256((p/name).read_bytes()).hexdigest() for name in ('extraction/mask.png','centerline/skeleton.png')}
        # 旧任务无新字段直接继续；不迁移或重建图像。
        before.pop('execution',None);save(before,p)
        legacy=json.loads(self.request(endpoint.removesuffix('/resume'))[2])
        self.assertEqual(legacy['execution']['source'],'local')
        for payload,code in [({'review_continue':True},403),({'review_continue':True,'consent_upload':False},403),
            ({'review_continue':True,'consent_upload':True,'local_only':True},409),
            ({'review_continue':True,'consent_upload':True,'color':'#ffffff'},400),
            ({'review_continue':True,'consent_upload':True,'answer':'stale'},400)]:
            self.assertEqual(self.request(endpoint,payload)[0],code)
        for headers in ({'X-CSRF-Token':'bad'},{'Origin':'https://evil.test'},{'Host':'evil.test'}):
            self.assertEqual(self.request(endpoint,{'review_continue':True,'consent_upload':True},headers=headers)[0],403)
        result,calls=self.review(endpoint,[mock_observe,mock_review,{'action':'deliver','summary':'mock当前图复核'}])
        self.assertEqual(len(calls),3);self.assertEqual(result['task_id'],data['task_id'])
        self.assertEqual(result['review_status'],'consistent');self.assertEqual(result['execution']['source'],'api')
        current,content=current_evidence(calls[0]);self.assertEqual(current['extraction_config']['component_ids'],[1,2])
        self.assertTrue(current['user_component_selection_locked']);self.assertGreaterEqual(sum(x['type']=='image_url' for x in content),2)
        self.assertEqual(load(p)['user_extraction_constraints'],before['user_extraction_constraints'])
        for name,h in hashes.items():self.assertEqual(hashlib.sha256((p/name).read_bytes()).hexdigest(),h)
        self.assertFalse(result['verified']);self.assertFalse(result['original_gps'])
        self.assertEqual(self.request(endpoint,{'review_continue':True})[0],403)

    def test_ambiguity_and_proposal_display(self):
        endpoint,data,p=self.prepared()
        result,calls=self.review(endpoint,[mock_observe,ambiguous_review,specific_question])
        self.assertEqual(result['review_status'],'ambiguous');self.assertTrue(result['question_evidence'])
        endpoint,data,p=self.prepared();mask=(p/'extraction/mask.png').read_bytes()
        result,calls=self.review(endpoint,[{'action':'observe_holes'},propose])
        self.assertEqual(result['hole_proposal']['status'],'pending');self.assertEqual(result['review_status'],'not_reviewed')
        self.assertEqual((p/'extraction/mask.png').read_bytes(),mask)

    def test_offline_and_failed_api_never_claim_review(self):
        endpoint,data,p=self.prepared();state=load(p);state['mode']='demo';save(state,p)
        self.assertEqual(self.request(endpoint,{'review_continue':True,'consent_upload':True})[0],409)
        state['mode']='api';save(state,p)
        result,calls=self.review(endpoint,[{'bad':'response'}])
        self.assertNotEqual(result['review_status'],'consistent');self.assertNotEqual(result['status'],'needs_confirmation')



class ContinueDOMTests(unittest.TestCase):
    def test_dedicated_handler_ignores_stale_fields_and_requires_fresh_consent(self):
        import shutil
        import subprocess
        from ride_web import PAGE
        if not shutil.which('node'):self.skipTest('需要本地Node')
        script=PAGE.split('<script nonce="__TOKEN__">')[1].split('</script>')[0]
        harness="""
const assert=require('assert'),nodes={},calls=[];
global.document={getElementById:id=>nodes[id]||(nodes[id]={checked:false,value:'',textContent:''}),querySelectorAll:()=>[],querySelector:()=>null};
"""+script+"""
action=async fn=>{try{await fn()}catch(e){nodes.error.textContent=e.message}finally{nodes.reviewConsent.checked=false}};
request=async(path,data)=>{calls.push({path,data});return {}};
render=()=>{};
(async()=>{
 for(const id of ['task','localOnly','color','crop','answer','uploadConsent','reviewConsent','error'])$(id);
 nodes.task.value='11111111-1111-4111-8111-111111111111';
 nodes.localOnly.checked=true;nodes.color.value='#ffffff';nodes.crop.value='[0,0,1,1]';nodes.answer.value='stale';
 nodes.uploadConsent.checked=true;
 await nodes.continueReview.onclick();assert.equal(calls.length,0);assert(nodes.error.textContent.includes('本次'));
 nodes.reviewConsent.checked=true;await nodes.continueReview.onclick();assert.equal(calls.length,1);
 assert.deepEqual(calls[0].data,{review_continue:true,consent_upload:true});assert.equal(nodes.reviewConsent.checked,false);
 await nodes.continueReview.onclick();assert.equal(calls.length,1);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
        result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':unittest.main()
