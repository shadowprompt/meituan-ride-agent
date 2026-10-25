"""仅合成输入与mock传输；不为真实旧任务推断地域/根因。"""
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
from PIL import Image
import map_fetch
import amap_fetch
from test_amap_provider import raw, KEY
import ride_agent as agent
import ride_web
from test_named_map_query import AREA, FIXTURE, response

ACTION = {'action':'query_map', 'area_name':'合成区', 'names':['A & B'], 'evidence':'合成可见文字，非用户确认'}


class DiagnosticsTests(unittest.TestCase):
    def test_reason_codes_no_body_or_retry_and_old_map_preserved(self):
        cases=[(b'[]','response_invalid'),(b'bad','response_invalid'),(b'{"status":"0","infocode":"10001","info":"SECRET_REMOTE_BODY"}','amap_credentials_rejected'),(b'{"status":"0","infocode":"10003"}','amap_quota'),(raw(['1,2']*11),'response_limit')]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'map.osm';path.write_text('old')
            for payload,code in cases:
                with self.subTest(code=code),patch.dict('os.environ',{'AMAP_API_KEY':KEY}),patch('amap_fetch._request',return_value=payload) as transport,patch('map_fetch._download') as osm:
                    with self.assertRaises(map_fetch.MapFetchError) as caught:
                        amap_fetch.fetch_location_clues('合成区',['A & B'],consent_download=True)
                    self.assertEqual(caught.exception.code,code)
                    self.assertNotIn('SECRET',json.dumps(caught.exception.diagnostic()))
                    self.assertEqual(transport.call_count,1);osm.assert_not_called()
                    self.assertEqual(path.read_text(),'old')

    def test_hierarchy_failure_agent_and_main_result_show_safe_levels(self):
        from test_amap_provider import Planner
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'synthetic.png';Image.new('RGB',(20,20),'white').save(image)
            folder=Path(d)/'task';state=agent.start(image,folder,True,'corridor')
            agent.select_map_provider(state,folder,'amap')
            action=dict(ACTION,area_name='合成市合成区',area_hierarchy=['合成市','合成区'])
            with patch('ride_agent.fetch_location_clues',side_effect=map_fetch.MapFetchError('SECRET',code='amap_service_error')):
                done=agent.run(state,folder,Planner(action),consent_map=True)
            diagnostic=done['map_query_diagnostic']
            self.assertEqual(diagnostic['request']['area_hierarchy'],['合成市','合成区'])
            self.assertEqual(diagnostic['reason_code'],'amap_service_error')
            self.assertNotIn('SECRET',json.dumps(diagnostic))
            start=ride_web.PAGE.index('function resultSummary(');end=ride_web.PAGE.index('\nfunction ',start+10)
            script=ride_web.PAGE[start:end]+'\nconsole.log(resultSummary('+json.dumps(done)+'));'
            output=subprocess.run(['node','-e',script],capture_output=True,text=True,check=True).stdout
            self.assertIn('amap_service_error',output)
            self.assertIn('合成市合成区',output)

    def test_partial_road_match_is_not_success_or_old_map_replacement(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'map.osm';path.write_text('old evidence')
            with patch.dict('os.environ',{'AMAP_API_KEY':KEY}),patch('amap_fetch._request',side_effect=[raw(),raw(())]) as transport:
                result=amap_fetch.fetch_location_clues('合成区',['A & B','合成缺失路'],consent_download=True)
            self.assertEqual(result['clues'][1]['status'],'not_found')
            self.assertFalse(result['supports_corridor'])
            self.assertEqual(transport.call_count,2)
            self.assertEqual(path.read_text(),'old evidence')

    def test_transport_http_timeout_limits_classified_safely(self):
        import time
        for exception,code in [(TimeoutError('SECRET'),'timeout'),(OSError('SECRET'),'network_error')]:
            with self.subTest(code=code),patch('amap_fetch.http.client.HTTPSConnection',side_effect=exception) as transport:
                with self.assertRaises(map_fetch.MapFetchError) as caught:
                    amap_fetch._request({'address':'合成区'},KEY,time.monotonic()+30)
                self.assertEqual(caught.exception.code,code)
                self.assertFalse(caught.exception.diagnostic()['automatic_retry'])
                self.assertNotIn('SECRET',str(caught.exception.diagnostic()))
                self.assertEqual(transport.call_count,1)

    def test_bad_names_and_object_budget(self):
        with patch('amap_fetch._request') as transport:
            for names in ([], ['a']*4,['a','a'],[False],['bad\ninput'],['\ud800']):
                with self.assertRaises(map_fetch.MapFetchError) as caught:
                    amap_fetch.fetch_location_clues('合成区',names,consent_download=True)
                self.assertEqual(caught.exception.code,'invalid_input')
            transport.assert_not_called()
        with self.assertRaises(map_fetch.MapFetchError) as caught:
            amap_fetch._candidates(raw(['1,2']*11))
        self.assertEqual(caught.exception.code,'response_limit')

    def test_agent_failure_keeps_request_redacts_key_no_map_and_refresh_no_retry(self):
        class Planner:
            semantic_review=True
            def decide(self,*args):return {**ACTION, 'evidence':'合成文字 SECRET_KEY'}
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'task';image=Path(d)/'synthetic.png'
            Image.new('RGB',(20,20),'white').save(image)
            state=agent.start(image,folder,True,'corridor')
            agent.select_map_provider(state,folder,'amap')
            attempts=[]
            def fail(*args,**kwargs):
                started=json.loads((folder/'map-query-diagnostic.json').read_text())
                self.assertEqual(started['outcome'],'started')
                attempts.append(started['attempt_id'])
                raise map_fetch.MapFetchError('SECRET_REMOTE_BODY',code='area_ambiguous')
            with patch.dict('os.environ',{'RIDE_API_KEY':'SECRET_KEY'}),patch('ride_agent.fetch_location_clues',side_effect=fail) as fetch:
                done=agent.run(state,folder,Planner(),consent_map=True)
                self.assertEqual(done['status'],'waiting_for_user')
                diagnostic=done['map_query_diagnostic']
                self.assertEqual(diagnostic['attempt_id'],attempts[0])
                self.assertEqual(diagnostic['request']['area_name'],'合成区')
                self.assertEqual(diagnostic['request']['names'],['A & B'])
                self.assertIn('[已隐藏]',diagnostic['request']['evidence'])
                self.assertNotIn('SECRET', (folder/'map-query-diagnostic.json').read_text())
                self.assertIn('area_ambiguous',done['question'])
                self.assertFalse((folder/'map.osm').exists())
                before=(folder/'map-query-diagnostic.json').read_bytes()
                agent.load(folder)
                resumed=agent.run(done,folder,Planner())
                self.assertEqual(fetch.call_count,1)
                self.assertEqual((folder/'map-query-diagnostic.json').read_bytes(),before)
                self.assertIn('预算',resumed['question'])

    def test_success_summary_and_diagnostic_replaced_after_failure(self):
        from test_amap_provider import Planner
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'synthetic.png';folder=Path(d)/'task';Image.new('RGB',(20,20),'white').save(image)
            state=agent.start(image,folder,True,'corridor')
            agent.select_map_provider(state,folder,'amap')
            with patch('ride_agent.fetch_location_clues',side_effect=map_fetch.MapFetchError('fixed',code='network_error')):
                state=agent.run(state,folder,Planner(),consent_map=True)
            old_id=state['map_query_diagnostic']['attempt_id']
            with patch.dict('os.environ',{'AMAP_API_KEY':KEY}),patch('amap_fetch._request',return_value=raw()):
                done=agent.run(state,folder,Planner(),consent_map=True)
            diagnostic=done['map_query_diagnostic']
            self.assertNotEqual(diagnostic['attempt_id'],old_id)
            self.assertEqual(diagnostic['outcome'],'succeeded')
            self.assertEqual(diagnostic['result_summary']['candidate_counts'],[1])
            self.assertFalse((folder/'map.osm').exists())
            self.assertTrue((folder/'amap-location-clues.json').exists())

    def test_primary_summary_renders_specific_failure_without_json(self):
        # 执行实际前端函数；仅传合成状态，不需要浏览器/网络。
        page=ride_web.PAGE if hasattr(ride_web,'PAGE') else ride_web.HTML
        if isinstance(page,bytes):page=page.decode()
        start=page.index('function resultSummary(d)')
        end=page.index('\nfunction render(d)',start)
        diagnostic={'outcome':'failed', 'reason_code':'area_ambiguous','message':'行政区有歧义',
                    'retry_advice':'请核对全称', 'request':{'area_name':'合成区','names':['合成路']}}
        script=page[start:end]+'\nconsole.log(resultSummary('+json.dumps({'execution_state':'succeeded','status':'waiting_for_user','goal':'corridor','map_query_diagnostic':diagnostic})+'));'
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,check=True).stdout
        for text in ('area_ambiguous','合成区','合成路','核对全称','不等于业务完成'):self.assertIn(text,result)


class WebDiagnosticTests(unittest.TestCase):
    import test_ride_web as fixtures
    setUp=fixtures.WebTests.setUp
    tearDown=fixtures.WebTests.tearDown
    state=fixtures.WebTests.state

    def test_diagnostic_available_even_without_map_refresh_read_only(self):
        diagnostic={'outcome':'failed','reason_code':'roads_not_found','request':{'area_name':'合成区','names':['合成路']}}
        task=self.state(goal='corridor',map_query_diagnostic=diagnostic)
        (self.root/task/'task'/'map-query-diagnostic.json').write_text(json.dumps(diagnostic))
        with patch.object(self.server,'run_cli') as cli:
            result=self.server.status(task)
            self.assertEqual(result['map_query_diagnostic'],diagnostic)
            self.assertFalse(result['map_available'])
            artifact=next(x for x in result['artifacts'] if x['name']=='map-query-diagnostic.json')
            self.assertTrue(artifact['current'])
            self.server.status(task);cli.assert_not_called()
