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

    def test_region_is_single_actual_query_with_distinct_audit_cache_and_archive(self):
        from urllib.parse import urlsplit, parse_qs
        body = [{'osm_type':'way','osm_id':5,'display_name':'Mock only'}]
        first, _ = self.call({**self.search,'names':['长风路']}, body)
        second, get = self.call({**self.search,'names':['长风路'],'region':'上海'}, body)
        params = parse_qs(urlsplit(get.call_args.args[1]).query)
        self.assertEqual(params, {'q':['上海 长风路'],'format':['jsonv2'],'limit':['5'],'addressdetails':['0']})
        self.assertEqual(self.budget, {'requests':2,'searches':2})
        self.assertEqual(self.state['research_search_queries'], ['长风路','上海 长风路'])
        self.assertNotEqual(first['source']['url'], second['source']['url'])
        self.assertEqual(first['source']['sha256'], second['source']['sha256'])
        self.assertEqual(len(list((self.folder/'research').glob('search-*.json'))), 2)
        self.assertEqual(self.state['research_requests'][1]['request'], {'names':['长风路'],'region':'上海','q':'上海 长风路'})
        # Equivalent q via names alone (including whitespace/case normalization) is duplicate.
        with self.assertRaises(MapFetchError):
            self.call({**self.search,'names':[' 上海   长风路 ']}, [])
        self.assertEqual(self.budget['requests'], 2)

    def test_region_and_names_reject_url_controls_query_syntax_and_extra_parameters(self):
        for field in ('region','names'):
            for text in ('https://evil.example','Shanghai&limit=99','//evil.example','x\nY','x\x7fY','x\u202eY','x\u200bY','x%26format=json','x?key=value','', 'x'*101):
                action = {**self.search, field: [text] if field == 'names' else text}
                with self.subTest(field=field,text=repr(text)), patch.object(live,'_get') as get:
                    with self.assertRaises(MapFetchError) as caught:
                        live.execute(self.state,self.folder,action,self.budget,True)
                    self.assertNotIsInstance(caught.exception,live.ModelShapeError)
                    get.assert_not_called()
        for key in ('url','limit','format','countrycodes','viewbox','timeout'):
            with patch.object(live,'_get') as get, self.assertRaises(MapFetchError) as caught:
                live.execute(self.state,self.folder,{**self.search,key:'arbitrary'},self.budget,True)
            self.assertNotIsInstance(caught.exception,live.ModelShapeError)
            get.assert_not_called()
        self.assertEqual(self.budget, {'requests':0,'searches':0})

    def test_real_report_shape_mock_http_search_region_repair_inspect_continue(self):
        from urllib.parse import urlsplit, parse_qs
        actions = [
            {**self.search,'names':['长风路']},
            {'action':'search_map_evidence','names':['Independent Landmark'],'region':'Mock Region'},
            {**self.search,'names':['Independent Landmark'],'region':'Mock Region'},
            {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Returned search ID'},
            {'action':'ask','question':'Mock geometry gathered; correspondence remains uncertain'},
        ]
        payloads = []
        def model(request, **kwargs):
            payloads.append(json.loads(request.data))
            if len(payloads) == 3:
                result = json.loads(self.state['messages'][-1]['content'].removeprefix('LOCAL_TOOL_RESULT '))
                self.assertFalse(result['terminal'])
                self.assertEqual(result['status'], 'invalid_model_shape')
                self.assertIn('evidence', result['schema']['search_map_evidence']['required'])
                self.assertEqual(result['budget'], {'requests':1,'searches':1})
                self.assertEqual(len(result['available_evidence']['discovery_ids']),4)
                self.assertEqual(result['available_evidence']['object_count'],0)
            if len(payloads) == 5:
                self.assertEqual(self.state['research_object_count'],3)
            response = unittest.mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps({'choices':[{'message':{'content':json.dumps(actions[len(payloads)-1])}}]}).encode()
            return response
        hits = [{'osm_type':'way','osm_id':i,'display_name':'Mock only'} for i in (5,6,7,8)]
        responses = [json.dumps(hits).encode(),json.dumps(hits).encode(),json.dumps({'elements':[self.way]+self.nodes}).encode()]
        with patch.object(live,'_get',side_effect=responses) as get, patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = model
            state = ride_agent.run(self.state,self.folder,ride_agent.APIPlanner(),budget=5,consent_map=True)
        self.assertEqual(len(payloads),5)
        self.assertEqual(get.call_count,3)
        self.assertEqual(parse_qs(urlsplit(get.call_args_list[1].args[1]).query)['q'],['Mock Region Independent Landmark'])
        self.assertEqual(state['research_budget_used'], {'requests':3,'searches':2})
        self.assertEqual(state['research_failures'][0]['corrections_used'],1)
        self.assertTrue(state['research_failures'][0]['resolved'])
        self.assertNotIn('research_diagnostic',state)
        self.assertNotIn('invalid_input',state['question'])
        self.assertEqual(state['status'],'waiting_for_user')
        self.assertFalse(state.get('corridor_complete'))

    def test_shape_correction_has_two_turn_limit_no_network_or_extra_model_budget(self):
        class Planner:
            semantic_review = True
            calls = 0
            def decide(inner, state, folder):
                inner.calls += 1
                return {'action':'search_map_evidence','names':['Mock Road']}  # missing evidence
        planner = Planner()
        with patch.object(live,'_get') as get:
            state = ride_agent.run(self.state,self.folder,planner,budget=12,consent_map=True)
        self.assertEqual(planner.calls,3)
        self.assertTrue(state['research_diagnostic']['terminal'])
        self.assertEqual([x.get('corrections_used') for x in state['research_failures']], [1,2,None])
        self.assertEqual(state['research_budget_used'], {'requests':0,'searches':0})
        get.assert_not_called()
        planner = Planner()
        with patch.object(live,'_get') as get:
            ride_agent.run(self.state,self.folder,planner,budget=1,consent_map=True)
        self.assertEqual(planner.calls,1)
        get.assert_not_called()

    def test_schema_correction_does_not_soften_auth_unknown_id_or_extra_keys(self):
        for action, consent in (({'action':'search_map_evidence','names':['Mock']},False),
                ({**self.search,'url':'https://evil.example'},True),
                ({**self.search,'region':'https://evil.example'},True),
                ({'action':'search_map_evidence','names':['https://evil.example']},True),
                ({'action':'search_map_evidence','names':'https://evil.example'},True),
                ({'action':'inspect_map_object','object_type':'way','object_id':999},True),
                ({'action':'inspect_map_object','object_type':'way','object_id':999,'evidence':'Invented'},True)):
            class Planner:
                semantic_review = True
                calls = 0
                def decide(inner,state,folder):
                    inner.calls += 1
                    return action
            planner = Planner()
            with patch.object(live,'_get') as get:
                state = ride_agent.run(self.state,self.folder,planner,budget=12,consent_map=consent)
            self.assertEqual(planner.calls,1)
            self.assertTrue(state['research_diagnostic']['terminal'])
            get.assert_not_called()

    def test_missing_evidence_does_not_reopen_duplicate_or_search_budget(self):
        self.discover()
        for action, budget in (({'action':'search_map_evidence','names':self.search['names']}, self.budget),
                ({'action':'search_map_evidence','names':['New Mock Road']}, {'requests':3,'searches':3}),
                ({'action':'search_map_evidence','names':['New Mock Road']}, {'requests':8,'searches':1})):
            with patch.object(live,'_get') as get, self.assertRaises(MapFetchError) as caught:
                live.execute(self.state,self.folder,action,budget,True)
            self.assertNotIsInstance(caught.exception,live.ModelShapeError)
            get.assert_not_called()

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
            self.assertEqual(conn.call_args.args,('nominatim.openstreetmap.org',))
            self.assertLessEqual(conn.call_args.kwargs['timeout'],25)
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

    def test_default_research_does_not_mask_model_network_failure(self):
        import urllib.error
        with patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch('ride_agent.urllib.request.build_opener') as opener, patch.object(live,'_get') as maps:
            opener.return_value.open.side_effect=urllib.error.URLError('mock network unavailable')
            planner=ride_agent.APIPlanner()
            state=ride_agent.run(self.state,self.folder,planner,consent_map=True)
        self.assertEqual(state['error_category'],'api_network')
        self.assertEqual(state['error_stage'],'planner')
        self.assertEqual(state['map_provider'],'research')
        self.assertFalse(state['corridor_complete'])
        maps.assert_not_called()

    def test_mock_model_http_image_revision_rebuilds_and_reviews_before_delivery(self):
        self.run_corridor_loop(False, model_http=True, revise=True)

    def test_mock_model_http_node_search_discovers_parent_then_full_geometry(self):
        self.run_corridor_loop(False, model_http=True, node_first=True)

    def test_mock_model_http_full_research_loop_preserves_turnback(self):
        self.run_corridor_loop(False, model_http=True)

    def test_agent_real_action_loop_dynamic_versions(self):
        self.run_corridor_loop(False)

    def test_two_searches_then_timeout_still_inspects_and_builds_corridor(self):
        self.run_corridor_loop(True)

    def run_corridor_loop(self, partial_failure, model_http=False, node_first=False, revise=False):
        class Planner:
            semantic_review=True
            index=0
            def decide(inner,state,folder):
                inner.index+=1
                step = inner.index
                if partial_failure:
                    if step == 2:
                        return {**self.search,'names':['Alternate visible road hypothesis']}
                    if step == 3:
                        return {**self.search,'names':['Uncertain visible landmark']}
                    if step == 4:
                        failure = state['research_failures'][0]
                        self.assertFalse(failure['terminal'])
                        self.assertEqual(failure['budget']['requests'],3)
                        self.assertEqual(failure['budget']['searches'],3)
                        self.assertIn(['way',5],failure['available_evidence']['discovery_ids'])
                        self.assertEqual(failure['available_evidence']['object_count'],0)
                        self.assertIn('unavailable',state['messages'][-1]['content'])
                    if step > 3:
                        step -= 2
                revision_review = revise and step == 8
                if revise and step > 8:
                    step = {9:4,10:5,11:6,12:7,13:8,14:9}[step]
                actions={1:self.search,2:{'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Search ID'},
                    3:{'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Actual way member'},
                    4:{'action':'research_hypothesis','version':state['research_version'],'ordered_roads':['Fixture Road','Fixture Road'],'evidence':'Unit repeated legs','limitations':'Fixture only'},
                    5:{'action':'inspect_roads','names':['Fixture Road']},
                    6:{'action':'build_corridor','hypothesis':{'schema_version':1,'coordinate_system':'WGS84','source':{'kind':'model','evidence':'Unit only'},'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]},{'road':{'name':'Fixture Road'},'via':[{'node_id':2},{'node_id':1}]}]}},
                    7:{'action':'observe_region','region':[0,0,60,60],'hypothesis':'repeated_order','reason':'Unit crop comparison'},
                    8:{'action':'review_research','version':state['research_version'],'result':'consistent','evidence':'Mock presented crop only','limitations':'Not real E2E'},
                    9:{'action':'deliver','summary':'Unit unverified candidate'}}
                if revision_review:
                    actions[8] = {**actions[8], 'result':'needs_revision','evidence':'Mock screenshot comparison requires revised candidate; no real visual claim'}
                if step==8 and not model_http:
                    # Script planner模拟成功模型传输标记；真实API分支单独测试，不将脚本称为视觉验收。
                    state['research_presented_version']=state['research_version']
                if node_first and step in (2,3):
                    return {'action':'inspect_map_object','object_type':'node' if step==2 else 'way','object_id':1 if step==2 else 5,'evidence':'Actual search or returned parent ID'}
                return actions[step]
        bodies=[[{'osm_type':'way','osm_id':5,'display_name':'Fixture Road'}],{'elements':[self.way]+self.nodes},{'elements':[self.way]}]
        if node_first:
            bodies=[[{'osm_type':'node','osm_id':1,'display_name':'Fixture junction'}], {'elements':[self.way]}, {'elements':[self.way]+self.nodes}]
        responses = [json.dumps(b).encode() for b in bodies]
        if partial_failure:
            responses[1:1] = [responses[0],MapFetchError('remote body withheld',code='timeout')]
        script = Planner()
        def mock_model(request, **kwargs):
            payload = json.loads(request.data)
            self.assertIn('CURRENT_STAGE_STATE', json.dumps(payload))
            response = unittest.mock.MagicMock()
            action = script.decide(self.state, self.folder)
            response.__enter__.return_value.read.return_value = json.dumps({'choices':[{'message':{'content':json.dumps(action)}}]}).encode()
            return response
        with patch.object(live,'_get',side_effect=responses) as get, patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = mock_model
            planner = ride_agent.APIPlanner() if model_http else script
            planner.map_query_authorized = True
            state=ride_agent.run(self.state,self.folder,planner,budget=None if model_http else 14 if revise else 11 if partial_failure else 9,consent_map=True)
            if model_http and revise:
                # 旧测试只改写复核结论、重跑相同假说/节点/区域，不是新证据。
                # 现在须确定性拒绝这条假收敛链，保留needs_revision而非交付。
                self.assertEqual(opener.return_value.open.call_count,11)
                self.assertEqual(state['status'],'paused')
                self.assertEqual(state['research_progress']['blocked_reason'],'no_progress_limit')
                self.assertEqual(state['research_review']['result'],'needs_revision')
                self.assertFalse(state.get('artifacts'))
                self.assertFalse(state['corridor_complete'])
                self.assertEqual(get.call_count,3)
                return
            if model_http:
                self.assertEqual(opener.return_value.open.call_count,9)
                self.assertEqual(state['research_presented_version'],state['research_version'])
                self.assertEqual([call.args[1] for call in get.call_args_list][1:], ['/api/0.6/node/1/ways.json','/api/0.6/way/5/full.json'] if node_first else ['/api/0.6/way/5/full.json','/api/0.6/node/1/ways.json'])
                geo = next(self.folder/name for name in state['corridor_artifacts'] if name.endswith('.geojson'))
                coordinates = json.loads(geo.read_text())['features'][0]['geometry']['coordinates']
                self.assertEqual(coordinates[0],coordinates[-1])
                self.assertIn([10.001,20.001],coordinates)

        self.assertEqual(get.call_count,5 if partial_failure else 3)
        self.assertEqual(state['status'],'needs_confirmation')
        audit=json.loads((self.folder/'research-audit.json').read_text())
        self.assertTrue(audit['network_used'])
        self.assertEqual(len(audit['requests']),5 if partial_failure else 3)
        self.assertEqual(audit['requests'][2]['outcome'],'failed' if partial_failure else 'succeeded')
        self.assertFalse(audit['verified'])
        self.assertEqual(audit['source_sha256'],self.state['source_sha256'])
        if partial_failure:
            self.assertEqual(state['research_budget_used']['requests'],5)
            self.assertEqual(state['research_budget_used']['searches'],3)
            self.assertNotIn('remote body withheld',(self.folder/'task.json').read_text())
            # 失败时的证据快照不能被后续对象发现原地改写。
            self.assertEqual(state['research_failures'][0]['available_evidence']['discovery_ids'],[['way',5]])



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

    def test_failed_search_is_audited_but_new_authorization_can_retry(self):
        with patch.object(live,'_get',side_effect=MapFetchError('withheld',code='http_429')):
            with self.assertRaises(MapFetchError):
                live.execute(self.state,self.folder,self.search,self.budget,True)
        self.assertNotIn('research_search_queries',self.state)
        self.assertEqual(self.state['research_requests'][0]['outcome'],'failed')
        self.state['research_search_queries']=[self.search['names'][0].casefold()]  # 旧版失败污染
        self.budget={'requests':0,'searches':0}  # 下一次显式授权，而非自动重试。
        self.call(self.search,[])
        self.assertEqual(len(self.state['research_search_queries']),1)

    def test_all_searches_fail_then_specific_image_ambiguity(self):
        class Planner:
            semantic_review=True
            index=0
            def decide(inner,state,folder):
                inner.index += 1
                if inner.index <= 3:
                    return {**self.search,'names':['Visible hypothesis '+str(inner.index)]}
                if inner.index == 4:
                    return {'action':'observe_region','region':[0,0,60,60],'hypothesis':'uncertain','reason':'Check ambiguous visible text, not automatic spelling correction'}
                return {'action':'ask','question':'Please confirm the uncertain landmark text in the crop.'}
        with patch.object(live,'_get',side_effect=MapFetchError('secret',code='network_error')) as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=6,consent_map=True)
        self.assertEqual(get.call_count,3)
        self.assertEqual(state['status'],'waiting_for_user')
        self.assertEqual(state['research_object_count'],0)
        self.assertIn('Visible hypothesis 3',state['question'])
        self.assertIn('review_region',state)
        self.assertFalse(state.get('corridor_complete'))

    def test_optional_failure_cannot_retry_same_query_in_invocation(self):
        class Planner:
            semantic_review=True
            def decide(inner,state,folder): return self.search
        with patch.object(live,'_get',side_effect=MapFetchError('secret',code='timeout')) as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=12,consent_map=True)
        self.assertEqual(get.call_count,1)
        self.assertEqual(state['research_budget_used']['requests'],1)
        self.assertEqual(state['research_diagnostic']['reason_code'],'invalid_input')
        self.assertEqual(state['status'],'waiting_for_user')

    def test_failed_object_cannot_be_automatically_retried(self):
        self.discover()
        action={'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Actual returned ID'}
        with patch.object(live,'_get',side_effect=MapFetchError('secret',code='timeout')) as get:
            for attempt in range(2):
                with self.assertRaises(MapFetchError) as caught:
                    live.execute(self.state,self.folder,action,self.budget,True)
                self.assertEqual(caught.exception.code,'timeout' if attempt==0 else 'invalid_input')
        self.assertEqual(get.call_count,1)
        self.assertEqual(self.budget['requests'],2)  # 一次发现 + 一次失败对象请求

    def test_hard_total_request_budget_stops_after_failed_eighth_request(self):
        class Planner:
            semantic_review=True
            index=0
            def decide(inner,state,folder):
                inner.index+=1
                if inner.index==1: return self.search
                return {'action':'inspect_map_object','object_type':'way','object_id':inner.index+3,'evidence':'Returned search ID; mock budget test'}
        # 七个不同返回ID，不能用重复已成功endpoint人为耗尽预算。
        self.state['research_seen_ids'] = [['way',i] for i in range(5,12)]
        responses=[json.dumps([{'osm_type':'way','osm_id':5}]).encode()]+[json.dumps({'elements':[{**self.way,'id':i}]+self.nodes}).encode() for i in range(5,11)]+[MapFetchError('secret',code='timeout')]
        with patch.object(live,'_get',side_effect=responses) as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=12,consent_map=True)
        self.assertEqual(get.call_count,8)
        self.assertEqual(state['research_budget_used']['requests'],8)
        self.assertTrue(state['research_diagnostic']['terminal'])
        self.assertEqual(state['research_object_count'],8)
        self.assertFalse(state.get('corridor_complete'))
        self.assertEqual(state['status'],'waiting_for_user')

    def test_loop_budget_after_failure_has_query_without_extra_request(self):
        class Planner:
            semantic_review=True
            def decide(inner,state,folder): return self.search
        with patch.object(live,'_get',side_effect=MapFetchError('secret',code='timeout')) as get:
            state=ride_agent.run(self.state,self.folder,Planner(),budget=1,consent_map=True)
        self.assertEqual(get.call_count,1)
        self.assertIn('Fixture Road',state['question'])
        self.assertIn('上限',state['question'])

    @unittest.skipIf(__import__('os').name == 'nt','POSIX flock contention test')
    def test_lock_wait_is_bounded_and_does_not_send_http(self):
        import fcntl
        import time
        with patch.object(live,'RATE_FILE',self.root/'rate'), patch.object(live,'TIMEOUT',0.06), patch.object(live.http.client,'HTTPSConnection') as conn:
            with (self.root/'rate').open('w+') as locked:
                fcntl.flock(locked,fcntl.LOCK_EX)
                begin=time.monotonic()
                with self.assertRaises(MapFetchError) as caught:
                    live._get('nominatim.openstreetmap.org','/search?q=fixture')
                self.assertLess(time.monotonic()-begin,0.5)
                self.assertEqual(caught.exception.code,'timeout')
                conn.assert_not_called()

    def test_same_deadline_interrupts_slow_headers_and_body(self):
        import threading
        import time
        import http.client
        for stage in ('headers','body'):
            wake=threading.Event()
            sock=unittest.mock.Mock()
            sock.shutdown.side_effect=lambda *args: wake.set()
            response=unittest.mock.Mock(status=200,fp=None)
            response.getheader.side_effect=lambda key,default=None: 'application/json' if key=='Content-Type' else default
            def stall(*args):
                wake.wait(1)
                raise http.client.RemoteDisconnected()
            with patch.object(live,'RATE_FILE',self.root/('rate-'+stage)), patch.object(live,'TIMEOUT',0.06), patch.object(live.http.client,'HTTPSConnection') as conn:
                conn.return_value.sock=sock
                if stage=='headers':
                    conn.return_value.getresponse.side_effect=stall
                else:
                    conn.return_value.getresponse.return_value=response
                    response.read1.side_effect=stall
                begin=time.monotonic()
                with self.assertRaises(MapFetchError) as caught:
                    live._get('nominatim.openstreetmap.org','/search?q=fixture')
                self.assertEqual(caught.exception.code,'timeout')
                self.assertLess(time.monotonic()-begin,0.5)
                conn.return_value.request.assert_called_once()

    def test_refresh_revokes_old_local_observation(self):
        self.state.update(review_region={'region_original_xyxy':[0,0,10,10]},research_presented_version='old',review_presented_version='old',review_presented_regions={'old':[]})
        live.refresh(self.state,self.folder)
        for key in ('review_region','research_presented_version','review_presented_version','review_presented_regions'):
            self.assertNotIn(key,self.state)

    def test_actual_api_branch_marks_crop_presented_only_after_success(self):
        import urllib.request
        self.discover()
        self.call({'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Search'}, {'elements':[self.way]+self.nodes})
        ride_agent.route_review.observe(self.state,self.folder,{'action':'observe_region','region':[0,0,60,60],'hypothesis':'uncertain','reason':'Unit local image'})
        self.state['review_region']['research_version']=self.state['research_version']
        self.state['research_failures']=[{'tool':'search_map_evidence','request':{'names':['Uncertain visible text']},'reason_code':'timeout'}]
        self.state['research_budget_used']={'requests':3,'searches':3}
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({'choices':[{'message':{'content':json.dumps({'action':'ask','question':'Mock only'})}}]}).encode()
        with patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch.object(urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=response
            planner=ride_agent.APIPlanner()
            planner.decide(self.state,self.folder)
            request=opener.return_value.open.call_args.args[0]
            body=json.loads(request.data)
            self.assertIn('LOCAL_REVIEW_EVIDENCE',json.dumps(body))
            snapshot=next(item['text'] for msg in body['messages'] if isinstance(msg.get('content'),list)
                          for item in msg['content'] if item.get('type')=='text' and item['text'].startswith('CURRENT_STAGE_STATE'))
            self.assertIn('Uncertain visible text',snapshot)
            self.assertIn('research_budget_used',snapshot)
            self.assertIn('timeout',snapshot)
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
    def test_default_create_needs_no_relay_configuration_and_keeps_old_providers(self):
        def cli(args):
            with patch.object(sys,'argv',['ride_agent.py',*args]):
                ride_agent.main()
        image=io.BytesIO()
        Image.new('RGB',(30,30),'white').save(image,format='PNG')
        with patch.dict('os.environ',{'OSM_SERVICE_URL':'','OSM_SERVICE_TOKEN':''}), patch.object(self.server,'run_cli',side_effect=cli), patch.object(live,'_get') as net, patch('osm_relay.require_configuration') as relay:
            code,_,body=self.request('/api/tasks',{'mode':'demo','image_base64':base64.b64encode(image.getvalue()).decode()})
            self.assertEqual(code,201,body)
            state=json.loads(body)
            self.assertEqual((state['map_provider'],state['goal']),('research','corridor'))
            self.assertTrue(state['research_online'])
            relay.assert_not_called();net.assert_not_called()
            config=json.loads(self.request('/api/config')[2])
            self.assertEqual(config['default_map_provider'],'research')
        for provider in ('amap','osm_relay','osm'):
            task=self.state(map_provider=provider,mode='api')
            before=(self.root/task/'task'/'task.json').read_bytes()
            with patch.object(self.server,'run_cli') as runner:
                code,_,body=self.request('/api/tasks/'+task+'/resume',{'consent_upload':True,'consent_map':True})
                if provider=='osm':
                    self.assertEqual(code,409,body)
                    runner.assert_not_called()
                else:
                    self.assertEqual(code,200,body)
                    args=runner.call_args.args[0]
                    self.assertEqual(args[args.index('--map-provider')+1],provider)
            self.assertEqual((self.root/task/'task'/'task.json').read_bytes(),before)

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
