"""合成原图+mock模型HTTP+mock地图HTTP，绝不上传真实截图或请求公网。"""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from PIL import Image
import map_fetch
import ride_agent as agent
from test_map_fetch import FIXTURE as RAW_FIXTURE
FIXTURE = json.loads(json.dumps(RAW_FIXTURE))
FIXTURE['elements'][2]['tags'].pop('access')
from test_road_corridor import plan, leg

AREA = {'elements':[{'type':'area','id':3600000001,'tags':{'name':'合成区','boundary':'administrative'}}]}


def response(data):
    r = Mock()
    r.__enter__ = Mock(return_value=r)
    r.__exit__ = Mock(return_value=False)
    r.status = 200
    r.headers = {}
    r.read.side_effect = io.BytesIO(json.dumps(data).encode()).read
    return r


class NamedMapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root/'synthetic.png'
        Image.new('RGB',(30,20),'white').save(self.image)

    def test_model_image_query_inspect_build_full_chain(self):
        state = agent.start(self.image,self.root/'task',False,'corridor')
        folder = self.root/'task'
        p = plan(leg('A & B',1,2))
        p['source']['kind'] = 'model'
        actions = [{'action':'query_map','area_name':'合成区','names':['A & B'],'evidence':'合成图文识别测试，不是真实图结果'},
                   {'action':'inspect_roads','names':['A & B']},
                   {'action':'build_corridor','hypothesis':p}]
        requests = []
        def model_http(req, **kwargs):
            body = json.loads(req.data)
            requests.append(body)
            index = len(requests)-1
            self.assertTrue(any(isinstance(m.get('content'),list) and any(c.get('type')=='image_url' for c in m['content']) for m in body['messages']))
            if index == 1:
                self.assertIn('map_query_authorized',str(body))
                self.assertIn('query_map',str(body))
            if index == 2:
                self.assertIn('osm_sha256',str(body))
            return response({'choices':[{'message':{'content':json.dumps(actions[index])}}]})
        opener = Mock()
        opener.open.side_effect = [response(AREA),response(FIXTURE)]
        model_opener = Mock();model_opener.open.side_effect = model_http
        def build(*handlers):
            return opener if any(isinstance(h, map_fetch._NoRedirect) for h in handlers) else model_opener
        with patch.dict('os.environ',{'RIDE_API_BASE':'https://model.invalid/v1','RIDE_API_KEY':'synthetic','RIDE_MODEL':'mock'}), patch('urllib.request.build_opener',side_effect=build):
            done = agent.run(state,folder,agent.ConsentedPlanner(),consent_map=True)
        self.assertEqual(done['status'],'needs_confirmation',done)
        self.assertEqual(len(requests),3)
        self.assertEqual(opener.open.call_count,2)
        self.assertFalse(done['registered'])
        self.assertEqual(done['map_query']['osm_sha256'],done['corridor_inspection']['osm_sha256'])
        self.assertIn('https://www.openstreetmap.org/way/3',done['map_query']['source_urls'])
        self.assertTrue((folder/'map-query.json').exists())
        # 文件改变后旧inspection/产物必须失效，不能交付旧坐标。
        (folder/'map.osm').write_text('<osm/>')
        class Ask:
            semantic_review=True
            def decide(self,*a):return {'action':'ask','question':'新地图缺少道路'}
        changed = agent.run(done,folder,Ask())
        self.assertFalse(changed['corridor_complete'])
        self.assertNotIn('corridor_inspection',changed)

    def test_no_consent_replay_and_one_query_budget(self):
        class Model:
            semantic_review=True
            def decide(self,*a):return {'action':'query_map','area_name':'合成区','names':['A & B'],'evidence':'可见地名'}
        for consent in (False,True):
            folder=self.root/str(consent)
            state=agent.start(self.image,folder,True,'corridor')
            opener=Mock();opener.open.side_effect=[response(AREA),response(FIXTURE)]
            with patch('map_fetch.urllib.request.build_opener',return_value=opener):
                done=agent.run(state,folder,Model(),consent_map=consent)
                self.assertEqual(opener.open.call_count,2 if consent else 0)
                self.assertEqual(done['status'],'waiting_for_user')
                agent.run(done,folder,Model())
                self.assertEqual(opener.open.call_count,2 if consent else 0)

    def test_exact_escaping_ambiguity_and_limits(self):
        path=self.root/'map.osm';path.write_text('old')
        for area in ({'elements':[]},{'elements':AREA['elements']*2},{'remark':'secret remote error','elements':AREA['elements']}):
            with patch('map_fetch._download',return_value=json.dumps(area).encode()) as download:
                with self.assertRaises(map_fetch.MapFetchError):
                    map_fetch.fetch_named_roads('合成区',['A & B'],path,consent_download=True)
                self.assertEqual(download.call_count,1)
                self.assertEqual(path.read_text(),'old')
        with patch('map_fetch._download') as download:
            for names in ([],['a']*4,[False],['x\nQL']):
                with self.assertRaises(map_fetch.MapFetchError):map_fetch.fetch_named_roads('合成区',names,path,consent_download=True)
            download.assert_not_called()
        # 33个返回对象触发预算，而非默默接受截断结果。
        fixture=json.loads(json.dumps(FIXTURE));fixture['elements'] += [dict(FIXTURE['elements'][2],id=i) for i in range(4,36)]
        with patch('map_fetch._download',side_effect=[json.dumps(AREA).encode(),json.dumps(fixture).encode()]):
            with self.assertRaisesRegex(map_fetch.MapFetchError,'budget'):map_fetch.fetch_named_roads('合成区',['A & B'],path,consent_download=True)
        self.assertEqual(path.read_text(),'old')


class NamedMapWebTests(unittest.TestCase):
    import test_ride_web as fixtures
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    request = fixtures.WebTests.request
    state = fixtures.WebTests.state

    def test_web_query_consent_forwarded_without_user_road_inputs(self):
        import base64
        image=io.BytesIO();Image.new('RGB',(20,20),'white').save(image,format='PNG')
        calls=[]
        def cli(args):
            calls.append(args)
            folder=Path(args[args.index('--task-dir')+1])
            if args[0]=='start':
                agent.start(args[args.index('--image')+1],folder,False,'corridor')
        payload={'image_base64':base64.b64encode(image.getvalue()).decode(),'mode':'api','goal':'corridor','consent_upload':True,'consent_map':True}
        with patch.object(self.server,'run_cli',side_effect=cli):
            created=self.server.create(payload)
            self.assertIn('--consent-map',calls[0])
            self.server.resume(created['task_id'],{'consent_upload':True,'consent_map':True})
            self.assertIn('--consent-map',calls[-1])
            self.assertNotIn('--osm-file',calls[-1])
            self.assertNotIn('--road-names',calls[-1])
            self.server.resume(created['task_id'],{'consent_upload':True,'answer':'继续'})
            self.assertNotIn('--consent-map',calls[-1])
            self.server.status(created['task_id'])
            self.assertEqual(len(calls),3)


class QueryEscapingTests(unittest.TestCase):
    def test_quoted_name_is_literal_not_overpass_program(self):
        with tempfile.TemporaryDirectory() as d:
            name='x"];way(1);["'
            area=json.loads(json.dumps(AREA));area['elements'][0]['tags']['name']=name
            fixture=json.loads(json.dumps(FIXTURE));fixture['elements'][2]['tags']['name']=name
            with patch('map_fetch._download',side_effect=[json.dumps(area).encode(),json.dumps(fixture).encode()]) as transport:
                map_fetch.fetch_named_roads(name,[name],Path(d)/'map.osm',consent_download=True)
            expected='["name"='+json.dumps(name,ensure_ascii=False)+']'
            self.assertIn(expected,transport.call_args_list[0].args[0])
            self.assertIn(expected,transport.call_args_list[1].args[0])
            self.assertEqual(transport.call_args_list[1].kwargs['max_bytes'],2000000)


class QueryActionBoundaryTests(unittest.TestCase):
    def test_model_cannot_supply_url_path_coordinate_or_reuse_old_consent(self):
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'synthetic.png';Image.new('RGB',(12,12),'white').save(image)
            for index,extra in enumerate(({'url':'https://evil.invalid'},{'output_path':'/tmp/map'},{'bounds':[1,2,3,4]})):
                state=agent.start(image,Path(d)/str(index),True,'corridor')
                action={'action':'query_map','area_name':'合成区','names':['A & B'],'evidence':'合成图中文字',**extra}
                class Bad:
                    semantic_review=True
                    def decide(self,*args):return action
                with patch('map_fetch._download') as transport:
                    done=agent.run(state,Path(d)/str(index),Bad(),consent_map=True)
                transport.assert_not_called()
                self.assertEqual(done['status'],'error')
                self.assertEqual(done['error_category'],'model_action_invalid')
