"""仅Mock HTTP/合成单元对象；绝不作为真实原图E2E证据。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import ride_agent
import research_online as live
from map_fetch import MapFetchError


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        Image.new('RGB',(60,60),'white').save(self.root/'fixture.png')
        self.folder = self.root/'task'
        self.state = ride_agent.start(self.root/'fixture.png',self.folder,goal='corridor')
        ride_agent.select_map_provider(self.state,self.folder,'research')
        self.budget = {'requests':0,'searches':0}
        self.search = {'action':'search_map_evidence','names':['Fixture Road region hypothesis'],'evidence':'Unverified visible text'}
        self.way = {'type':'way','id':5,'nodes':[1,2],'tags':{'name':'Fixture Road','highway':'residential'}}
        self.nodes = [{'type':'node','id':1,'lat':20,'lon':10},{'type':'node','id':2,'lat':20.001,'lon':10.001}]

    def call(self, action, body, consent=True):
        with patch.object(live,'_get',return_value=json.dumps(body).encode()) as get:
            result = live.execute(self.state,self.folder,action,self.budget,consent)
        return result,get

    def discover(self):
        self.call(self.search,[{'osm_type':'way','osm_id':5,'display_name':'Fixture Road'}])
        self.state['research_seen_ids'] = [['way',5]]

    def test_live_search_and_strict_membership(self):
        self.discover()
        inspect = {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Actual search ID'}
        result,get = self.call(inspect,{'elements':[self.way]+self.nodes})
        self.assertEqual(get.call_args.args,('api.openstreetmap.org','/api/0.6/way/5/full.json'))
        self.assertEqual(len(result['objects']),3)
        self.assertEqual(result['coordinate_system'],'WGS84')
        self.state['research_seen_ids'].append(['node',1])
        inspect.update(object_type='node',object_id=1)
        result,get = self.call(inspect,{'elements':[self.way]})
        self.assertEqual(get.call_args.args[1],'/api/0.6/node/1/ways.json')
        wrong = {**self.way,'id':6,'nodes':[9,2]}
        with self.assertRaises(MapFetchError):
            self.call(inspect,{'elements':[wrong]})
        self.assertNotIn(('way',6),live.archive(self.folder)['objects'])
        self.assertEqual(self.state['research_requests'][-1]['reason_code'],'response_invalid')

    def test_consent_ids_budget_and_duplicate_search(self):
        for action in (self.search,{'action':'inspect_map_object','object_type':'way','object_id':999,'evidence':'invented'}):
            with patch.object(live,'_get') as get:
                with self.assertRaises(MapFetchError):
                    live.execute(self.state,self.folder,action,self.budget,False if action is self.search else True)
                get.assert_not_called()
        self.discover()
        with self.assertRaises(MapFetchError):
            self.call(self.search,[])
        self.budget['requests']=8
        with self.assertRaises(MapFetchError):
            self.call({**self.search,'names':['different']},[])

    def test_mock_https_fixed_host_headers_no_redirect_or_retry(self):
        class Response:
            status=200
            fp=None
            def __init__(self): self.data=io.BytesIO(b'[]')
            def getheader(self,key,default=None): return {'Content-Type':'application/json'}.get(key,default)
            def read1(self,n): return self.data.read(n)
        with patch.object(live,'RATE_FILE',self.root/'rate'), patch.object(live.http.client,'HTTPSConnection') as conn, patch.object(live.time,'sleep'):
            response=Response()
            conn.return_value.getresponse.return_value=response
            self.assertEqual(live._get('nominatim.openstreetmap.org','/search?q=fixture'),b'[]')
            conn.assert_called_once_with('nominatim.openstreetmap.org',timeout=25)
            self.assertIn('User-Agent',conn.return_value.request.call_args.kwargs['headers'])
            response.status=302
            with self.assertRaises(MapFetchError) as caught:
                live._get('nominatim.openstreetmap.org','/search?q=fixture')
            self.assertEqual(caught.exception.code,'redirect_refused')
            self.assertEqual(conn.return_value.request.call_count,2)

    def test_agent_missing_consent_stops_with_specific_question(self):
        class Planner:
            semantic_review=True
            def decide(inner,state,folder): return self.search
        with patch.object(live,'_get') as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=2)
        get.assert_not_called()
        self.assertEqual(state['status'],'waiting_for_user')
        self.assertIn('Fixture Road',state['question'])
        self.assertEqual(state['research_diagnostic']['reason_code'],'consent_required')

    def test_agent_real_action_loop_dynamic_versions(self):
        class Planner:
            semantic_review=True
            index=0
            def decide(inner,state,folder):
                inner.index+=1
                actions={1:self.search,2:{'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Search ID'},
                    3:{'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Actual way member'},
                    4:{'action':'research_hypothesis','version':state['research_version'],'ordered_roads':['Fixture Road','Fixture Road'],'evidence':'Unit repeated legs','limitations':'Fixture only'},
                    5:{'action':'inspect_roads','names':['Fixture Road']},
                    6:{'action':'build_corridor','hypothesis':{'schema_version':1,'coordinate_system':'WGS84','source':{'kind':'model','evidence':'Unit only'},'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]},{'road':{'name':'Fixture Road'},'via':[{'node_id':2},{'node_id':1}]}]}},
                    7:{'action':'observe_region','region':[0,0,60,60],'hypothesis':'repeated_order','reason':'Unit crop comparison'},
                    8:{'action':'review_research','version':state['research_version'],'result':'consistent','evidence':'Mock presented crop only','limitations':'Not real E2E'},
                    9:{'action':'deliver','summary':'Unit unverified candidate'}}
                if inner.index==8:
                    # Script planner模拟成功模型传输标记；真实API分支单独测试，不将脚本称为视觉验收。
                    state['research_presented_version']=state['research_version']
                return actions[inner.index]
        bodies=[[{'osm_type':'way','osm_id':5,'display_name':'Fixture Road'}],{'elements':[self.way]+self.nodes},{'elements':[self.way]}]
        with patch.object(live,'_get',side_effect=[json.dumps(b).encode() for b in bodies]) as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=9,consent_map=True)
        self.assertEqual(get.call_count,3)
        self.assertEqual(state['status'],'needs_confirmation')
        audit=json.loads((self.folder/'research-audit.json').read_text())
        self.assertTrue(audit['network_used'])
        self.assertEqual(len(audit['requests']),3)
        self.assertFalse(audit['verified'])



    def test_agent_429_records_query_and_does_not_retry(self):
        class Planner:
            semantic_review=True
            def decide(inner,state,folder): return self.search
        with patch.object(live,'_get',side_effect=MapFetchError('secret remote body',code='http_429')) as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=12,consent_map=True)
        self.assertEqual(get.call_count,1)
        self.assertEqual(state['research_requests'][0]['reason_code'],'http_429')
        self.assertIn('Fixture Road',state['question'])
        self.assertNotIn('secret remote body',(self.folder/'task.json').read_text())

    def test_actual_api_branch_marks_crop_presented_only_after_success(self):
        import urllib.request
        self.discover()
        self.call({'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Search'}, {'elements':[self.way]+self.nodes})
        ride_agent.route_review.observe(self.state,self.folder,{'action':'observe_region','region':[0,0,60,60],'hypothesis':'uncertain','reason':'Unit local image'})
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'choices':[{'message':{'content':json.dumps({'action':'ask','question':'Mock only'})}}]}).encode()
        with patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch.object(urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=response
            planner=ride_agent.APIPlanner()
            planner.decide(self.state,self.folder)
            request=opener.return_value.open.call_args.args[0]
            body=json.loads(request.data)
            self.assertIn('LOCAL_REVIEW_EVIDENCE',json.dumps(body))
        self.assertEqual(self.state['research_presented_version'],self.state['research_version'])
        self.state.pop('research_presented_version')
        with patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch.object(urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=TimeoutError()
            with self.assertRaises(Exception):
                ride_agent.APIPlanner().decide(self.state,self.folder)
        self.assertNotIn('research_presented_version',self.state)


# 真实 loopback 网页HTTP -> CLI参数 -> Agent动作 -> Mock公开HTTP；无付费模型/真实地图。
import base64
import sys
import test_ride_web as web_fixture

class OnlineWebTests(web_fixture.WebTests):
    def test_normal_web_research_create_resume_separate_consent_restore(self):
        class Planner:
            semantic_review=True
            def decide(inner,state,folder):
                if state.get('research_search_queries'):
                    return {'action':'ask','question':'已检索；截图道路对应尚未验证。'}
                return {'action':'search_map_evidence','names':['Web Fixture Road'],'evidence':'Mock visible hypothesis'}
        def cli(args):
            with patch.object(sys,'argv',['ride_agent.py',*args]), patch('ride_agent.ConsentedPlanner',side_effect=lambda:Planner()):
                ride_agent.main()
        image=io.BytesIO()
        Image.new('RGB',(30,30),'white').save(image,format='PNG')
        with patch.object(self.server,'run_cli',side_effect=cli), patch.object(live,'_get',return_value=b'[]') as net:
            code,_,body=self.request('/api/tasks',{'mode':'api','goal':'corridor','map_provider':'research','consent_upload':True,'image_base64':base64.b64encode(image.getvalue()).decode()})
            self.assertEqual(code,201,body)
            task=json.loads(body)['task_id']
            net.assert_not_called()
            data=json.loads(body)
            self.assertEqual(data['research_diagnostic']['reason_code'],'consent_required')
            code,_,body=self.request('/api/tasks/'+task+'/resume',{'map_provider':'research','consent_upload':True,'consent_map':True})
            self.assertEqual(code,200,body)
            self.assertEqual(net.call_count,1)
            self.assertEqual(net.call_args.args[0],'nominatim.openstreetmap.org')
            data=json.loads(body)
            self.assertNotIn('research_diagnostic',data)
            self.assertEqual(data['research_requests'][0]['outcome'],'succeeded')
            self.assertFalse(data['corridor_complete'])
            self.request('/api/tasks/'+task)
            self.assertEqual(net.call_count,1)


if __name__=='__main__': unittest.main()
