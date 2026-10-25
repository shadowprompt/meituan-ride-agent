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

    def test_model_image_query_returns_amap_gcj02_clues_not_corridor(self):
        from test_amap_provider import raw, KEY
        state=agent.start(self.image,self.root/'task',False,'corridor'); folder=self.root/'task'
        state['map_provider']='amap'
        action={'action':'query_map','area_name':'合成区','names':['A & B'],'evidence':'合成图文'}
        requests=[]
        def model_http(req,**kwargs):
            payload=json.loads(req.data); requests.append(payload)
            self.assertTrue(any(isinstance(m.get('content'),list) and any(c.get('type')=='image_url' for c in m['content']) for m in payload['messages']))
            return response({'choices':[{'message':{'content':json.dumps(action)}}]})
        with patch.dict('os.environ',{'RIDE_API_BASE':'https://model.invalid/v1','RIDE_API_KEY':'synthetic','RIDE_MODEL':'mock','AMAP_API_KEY':KEY}), patch('urllib.request.build_opener') as opener, patch('amap_fetch._request',return_value=raw()) as amap, patch('map_fetch._download') as osm:
            opener.return_value.open.side_effect=model_http
            done=agent.run(state,folder,agent.ConsentedPlanner(),consent_map=True)
            osm.assert_not_called();amap.assert_called_once()
        self.assertEqual(len(requests),1)
        self.assertEqual(done['status'],'waiting_for_user')
        self.assertEqual(done['location_clues']['coordinate_system'],'GCJ02')
        self.assertFalse(done['corridor_complete'])
        self.assertFalse((folder/'map.osm').exists())
        self.assertNotIn('corridor_inspection',done)

    def test_no_consent_replay_and_one_query_budget(self):
        from test_amap_provider import raw, KEY, Planner
        for consent in (False,True):
            folder=self.root/str(consent); state=agent.start(self.image,folder,True,'corridor')
            state['map_provider']='amap'
            with patch.dict('os.environ',{'AMAP_API_KEY':KEY}),patch('amap_fetch._request',return_value=raw()) as net,patch('map_fetch._download') as osm:
                done=agent.run(state,folder,Planner(),consent_map=consent)
                self.assertEqual(net.call_count,1 if consent else 0)
                self.assertEqual(done['status'],'waiting_for_user')
                agent.run(done,folder,Planner())
                self.assertEqual(net.call_count,1 if consent else 0)
                osm.assert_not_called()

    def test_retired_named_osm_query_refused_without_io(self):
        path=self.root/'map.osm';path.write_text('old')
        with patch('map_fetch._download') as transport, patch('urllib.request.build_opener') as network:
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch.fetch_named_roads('合成区',['A & B'],path,consent_download=True)
            self.assertEqual(caught.exception.code,'osm_network_disabled')
            transport.assert_not_called();network.assert_not_called()
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
                state=agent.start(args[args.index('--image')+1],folder,False,'corridor'); state['map_provider']='amap'; agent.save(state,folder)
        payload={'image_base64':base64.b64encode(image.getvalue()).decode(),'mode':'api','goal':'corridor','map_provider':'amap','consent_upload':True,'consent_map':True}
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
    def test_amap_quoted_name_is_address_data_not_a_program(self):
        from test_amap_provider import raw, KEY
        import amap_fetch
        name='x"];way(1);["'
        with patch.dict('os.environ',{'AMAP_API_KEY':KEY}),patch('amap_fetch._request',return_value=raw()) as transport,patch('map_fetch._download') as osm:
            result=amap_fetch.fetch_location_clues('合成区',[name],consent_download=True)
        self.assertEqual(transport.call_args.args[0], {'address':'合成区'+name})
        self.assertEqual(result['names'],[name])
        osm.assert_not_called()


class QueryActionBoundaryTests(unittest.TestCase):
    def test_model_cannot_supply_url_path_coordinate_or_reuse_old_consent(self):
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'synthetic.png';Image.new('RGB',(12,12),'white').save(image)
            for index,extra in enumerate(({'url':'https://evil.invalid'},{'output_path':'/tmp/map'},{'bounds':[1,2,3,4]})):
                state=agent.start(image,Path(d)/str(index),True,'corridor')
                state['map_provider']='amap'
                action={'action':'query_map','area_name':'合成区','names':['A & B'],'evidence':'合成图中文字',**extra}
                class Bad:
                    semantic_review=True
                    def decide(self,*args):return action
                with patch('map_fetch._download') as transport:
                    done=agent.run(state,Path(d)/str(index),Bad(),consent_map=True)
                transport.assert_not_called()
                self.assertEqual(done['status'],'error')
                self.assertEqual(done['error_category'],'model_action_invalid')
