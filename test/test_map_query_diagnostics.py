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
import ride_agent as agent
import ride_web
from test_named_map_query import AREA, FIXTURE, response

ACTION = {'action':'query_map', 'area_name':'合成区', 'names':['A & B'], 'evidence':'合成可见文字，非用户确认'}


class DiagnosticsTests(unittest.TestCase):
    def test_reason_codes_no_body_or_retry_and_old_map_preserved(self):
        cases = [
            ([{'elements':[]}], 'area_not_found'),
            ([{'elements':AREA['elements']*2}], 'area_ambiguous'),
            ([{'remark':'SECRET_REMOTE_BODY','elements':[]}], 'response_incomplete'),
            ([[]], 'response_invalid'),
            ([AREA, {'elements':[]}], 'roads_not_found'),
            ([AREA, {'remark':'SECRET_REMOTE_BODY','elements':[]}], 'response_incomplete'),
        ]
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'map.osm';path.write_text('old')
            for replies, code in cases:
                with self.subTest(code=code), patch('map_fetch._download', side_effect=[json.dumps(r).encode() for r in replies]) as transport:
                    with self.assertRaises(map_fetch.MapFetchError) as caught:
                        map_fetch.fetch_named_roads('合成区',['A & B'],path,consent_download=True)
                    self.assertEqual(caught.exception.code,code)
                    self.assertNotIn('SECRET_REMOTE_BODY',json.dumps(caught.exception.diagnostic()))
                    self.assertEqual(transport.call_count,len(replies))
                    self.assertEqual(path.read_text(),'old')

    def test_transport_http_timeout_limits_classified_safely(self):
        errors = [(urllib.error.HTTPError('https://secret.invalid/KEY',429,'SECRET_REMOTE_BODY',{},None),'http_429'),
                  (urllib.error.HTTPError('https://secret.invalid/KEY',503,'SECRET_REMOTE_BODY',{},None),'http_error'),
                  (socket.timeout('SECRET_REMOTE_BODY'),'timeout'),
                  (urllib.error.URLError(socket.timeout('SECRET_REMOTE_BODY')),'timeout'),
                  (urllib.error.URLError('SECRET_REMOTE_BODY'),'network_error')]
        for error,code in errors:
            opener=Mock();opener.open.side_effect=error
            with self.subTest(code=code),patch('map_fetch.urllib.request.build_opener',return_value=opener):
                with self.assertRaises(map_fetch.MapFetchError) as caught:map_fetch._download('synthetic')
                diag=caught.exception.diagnostic()
                self.assertEqual(diag['reason_code'],code)
                self.assertFalse(diag['automatic_retry'])
                self.assertNotIn('SECRET',str(diag));self.assertNotIn('secret.invalid',str(diag))
                self.assertEqual(opener.open.call_count,1)
        r=response(AREA);r.headers={'Content-Length':'2000001'}
        opener=Mock();opener.open.return_value=r
        with patch('map_fetch.urllib.request.build_opener',return_value=opener):
            with self.assertRaises(map_fetch.MapFetchError) as caught:map_fetch._download('synthetic',max_bytes=2000000)
        self.assertEqual(caught.exception.code,'response_limit')

    def test_bad_names_and_object_budget(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'map.osm'
            with patch('map_fetch._download') as transport:
                for names in ([], ['a']*4,['a','a'],[False],['bad\ninput'],['\ud800']):
                    with self.assertRaises(map_fetch.MapFetchError) as caught:
                        map_fetch.fetch_named_roads('合成区',names,path,consent_download=True)
                    self.assertEqual(caught.exception.code,'invalid_input')
                transport.assert_not_called()
            fixture=json.loads(json.dumps(FIXTURE))
            fixture['elements'] += [dict(FIXTURE['elements'][2],id=i) for i in range(4,36)]
            with patch('map_fetch._download',side_effect=[json.dumps(AREA).encode(),json.dumps(fixture).encode()]):
                with self.assertRaises(map_fetch.MapFetchError) as caught:map_fetch.fetch_named_roads('合成区',['A & B'],path,consent_download=True)
            self.assertEqual(caught.exception.code,'response_limit')

    def test_agent_failure_keeps_request_redacts_key_no_map_and_refresh_no_retry(self):
        class Planner:
            semantic_review=True
            def decide(self,*args):return {**ACTION, 'evidence':'合成文字 SECRET_KEY'}
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)/'task';image=Path(d)/'synthetic.png'
            Image.new('RGB',(20,20),'white').save(image)
            state=agent.start(image,folder,True,'corridor')
            attempts=[]
            def fail(*args,**kwargs):
                started=json.loads((folder/'map-query-diagnostic.json').read_text())
                self.assertEqual(started['outcome'],'started')
                attempts.append(started['attempt_id'])
                raise map_fetch.MapFetchError('SECRET_REMOTE_BODY',code='area_ambiguous')
            with patch.dict('os.environ',{'RIDE_API_KEY':'SECRET_KEY'}),patch('ride_agent.fetch_named_roads',side_effect=fail) as fetch:
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
        class Planner:
            semantic_review=True
            def decide(self,*args):return ACTION
        with tempfile.TemporaryDirectory() as d:
            image=Path(d)/'synthetic.png';folder=Path(d)/'task'
            Image.new('RGB',(20,20),'white').save(image)
            state=agent.start(image,folder,True,'corridor')
            with patch('map_fetch._download',return_value=b'{"elements":[]}'):
                state=agent.run(state,folder,Planner(),consent_map=True)
            old_id=state['map_query_diagnostic']['attempt_id']
            with patch('map_fetch._download',side_effect=[json.dumps(AREA).encode(),json.dumps(FIXTURE).encode()]):
                done=agent.run(state,folder,Planner(),budget=1,consent_map=True)
            diag=done['map_query_diagnostic']
            self.assertNotEqual(diag['attempt_id'],old_id)
            self.assertEqual(diag['outcome'],'succeeded')
            self.assertEqual(diag['result_summary']['way_count'],1)
            self.assertTrue((folder/'map.osm').exists())

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
