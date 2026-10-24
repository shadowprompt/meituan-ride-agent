"""安全诊断与同任务失败恢复：只用合成图、loopback HTTP，绝不访问外网。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import socket
import ssl
import threading
import unittest
import urllib.error
from unittest.mock import patch
from ride_errors import api_diagnostic, failure_category, safe_diagnostic
from ride_agent import load, save
import test_web_review_continue as continue_fixtures
from test_route_review import mock_observe, mock_review


class TypeTests(unittest.TestCase):
    def test_exception_types_not_messages(self):
        for reason, category, code in [(socket.gaierror('SECRET'), 'api_network','dns'),
                (ssl.SSLError('SECRET'),'api_network','tls'),
                (TimeoutError('SECRET'),'api_timeout','timeout'),
                (ConnectionRefusedError('SECRET'),'api_network','connection'),
                ('SECRET timeout dns tls','api_network','network')]:
            error=urllib.error.URLError(reason)
            self.assertEqual(failure_category(error,'planner'),category)
            self.assertEqual(api_diagnostic(error),{'code':code})
        for status, category in [(401,'api_auth'),(403,'api_auth'),(404,'api_http'),
                                  (429,'api_rate_limit'),(500,'api_server'),(503,'api_server')]:
            error=urllib.error.HTTPError('https://SECRET?key=SECRET',status,'SECRET',{},None)
            self.assertEqual(failure_category(error,'planner'),category)
            self.assertEqual(api_diagnostic(error)['http_status'],status)
            self.assertNotIn('SECRET',json.dumps(api_diagnostic(error)))

    def test_retry_after_bounded_and_redaction(self):
        from email.utils import formatdate
        import time
        for raw, expected in [('12',12),('999999',None),('-1',None),('SECRET',None),
                              (formatdate(time.time()+30,usegmt=True),'date')]:
            error=urllib.error.HTTPError('SECRET',429,'SECRET',{'Retry-After':raw},None)
            delay=api_diagnostic(error).get('retry_after_seconds')
            if expected=='date':self.assertTrue(28<=delay<=31)
            else:self.assertEqual(delay,expected)
        self.assertEqual(safe_diagnostic({'code':'dns','endpoint':'SECRET','headers':'SECRET',
            'http_status':'SECRET','retry_after_seconds':True}),{'code':'dns'})
        self.assertEqual(safe_diagnostic({'code':'SECRET'}),{})
        self.assertEqual(safe_diagnostic({'code':[]}),{})


class RecoveryHTTPTests(unittest.TestCase):
    setUp=continue_fixtures.ContinueTests.setUp
    tearDown=continue_fixtures.ContinueTests.tearDown
    request=continue_fixtures.ContinueTests.request
    model=continue_fixtures.ContinueTests.model
    create_image=continue_fixtures.ContinueTests.create_image
    prepared=continue_fixtures.ContinueTests.prepared
    review=continue_fixtures.ContinueTests.review

    def error_model(self,status,delay=None):
        calls=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                calls.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(status)
                if delay is not None:self.send_header('Retry-After',delay)
                self.send_header('X-Private','SECRET')
                self.end_headers();self.wfile.write(b'SECRET endpoint?key=SECRET Authorization private body')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        return server,calls

    def test_http_failure_same_task_then_success_no_reextract(self):
        for status, category in [(401,'api_auth'),(403,'api_auth'),(404,'api_http'),
                                  (429,'api_rate_limit'),(500,'api_server'),(503,'api_server')]:
            with self.subTest(status=status):
                endpoint,data,p=self.prepared();before=load(p)
                names=('extraction/mask.png','centerline/skeleton.png')
                hashes={n:hashlib.sha256((p/n).read_bytes()).hexdigest() for n in names}
                server,calls=self.error_model(status,'2' if status==429 else None)
                with patch.dict(os.environ,{'RIDE_API_BASE':f'http://127.0.0.1:{server.server_port}/v1',
                                             'RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
                    # 未授权及旧创建授权不能触发网络。
                    self.assertEqual(self.request(endpoint,{'review_continue':True})[0],403)
                    self.assertEqual(len(calls),0)
                    code,_,body=self.request(endpoint,{'review_continue':True,'consent_upload':True})
                    self.assertEqual(code,200,body)
                    result=json.loads(body)
                    self.assertEqual(result['error_category'],category)
                    self.assertEqual(result['error_stage'],'planner')
                    self.assertEqual(result['error_diagnostic']['http_status'],status)
                    self.assertEqual(result['execution'],{'source':'api','api_responses':0})
                    self.assertEqual(len(calls),1)  # 收到原图但无成功响应，不能说没上传。
                    if status==429:
                        self.request(endpoint,{'review_continue':True,'consent_upload':True})
                        self.assertEqual(len(calls),1)  # 尊重Retry-After，不重新发送。
                self.assertNotIn(b'SECRET',body)
                self.assertNotIn('SECRET',(p/'task.json').read_text())
                self.assertTrue(result['centerlined']);self.assertTrue(result['user_components_applied'])
                self.assertFalse(result['component_selection']['requires_selection'])
                after=load(p)
                self.assertEqual(after['user_extraction_constraints'],before['user_extraction_constraints'])
                self.assertEqual(after['artifacts'],before['artifacts'])
                self.assertTrue(all(a['current'] for a in result['artifacts'] if a['name'] in names))
                for n,h in hashes.items():self.assertEqual(hashlib.sha256((p/n).read_bytes()).hexdigest(),h)
                # 模拟等待到期，不sleep；真实恢复仍经过CLI和本次授权。
                after.pop('api_retry_not_before',None);save(after,p)
                result,calls=self.review(endpoint,[mock_observe,mock_review,{'action':'deliver','summary':'mock恢复复核'}])
                self.assertEqual(result['task_id'],data['task_id']);self.assertEqual(result['review_status'],'consistent')
                self.assertNotIn('error_category',result);self.assertNotIn('error_diagnostic',result)
                self.assertEqual(len(calls),3)
                for n,h in hashes.items():self.assertEqual(hashlib.sha256((p/n).read_bytes()).hexdigest(),h)

    def test_web_diagnostic_allowlist_and_legacy(self):
        endpoint,data,p=self.prepared();state=load(p)
        state.update(status='error',error='SECRET',error_category='api_http',error_stage='planner',
            error_diagnostic={'code':'http_client','http_status':404,'endpoint':'SECRET?key=SECRET',
                              'headers':'SECRET','body':'SECRET','response':'SECRET'})
        save(state,p)
        body=self.request(endpoint.removesuffix('/resume'))[2]
        self.assertNotIn(b'SECRET',body)
        self.assertEqual(json.loads(body)['error_diagnostic'],{'code':'http_client','http_status':404})
        state.pop('error_diagnostic');state.pop('execution',None);save(state,p)
        result=json.loads(self.request(endpoint.removesuffix('/resume'))[2])
        self.assertNotIn('error_diagnostic',result);self.assertTrue(result['centerlined'])

    def test_same_task_connection_failure_then_success(self):
        endpoint,data,p=self.prepared();before=load(p)
        # 关闭临时loopback端口，真实CLI得到ConnectionRefused/URLError，不访问外网。
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        with patch.dict(os.environ,{'RIDE_API_BASE':f'http://127.0.0.1:{port}/v1',
                                   'RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
            code,_,body=self.request(endpoint,{'review_continue':True,'consent_upload':True})
        result=json.loads(body);self.assertEqual(code,200)
        self.assertEqual(result['error_category'],'api_network')
        self.assertEqual(result['error_diagnostic']['code'],'connection')
        self.assertTrue(result['centerlined']);self.assertTrue(result['user_components_applied'])
        self.assertEqual(load(p)['user_extraction_constraints'],before['user_extraction_constraints'])
        result,calls=self.review(endpoint,[mock_observe,mock_review,{'action':'deliver','summary':'mock连接恢复'}])
        self.assertEqual(result['review_status'],'consistent');self.assertEqual(len(calls),3)

    def test_first_unselected_task_is_not_data_loss(self):
        from PIL import Image
        server,calls=self.error_model(401)
        # 未选候选不能被误报为已有中心线丢失。
        from PIL import ImageDraw
        image=Image.new('RGB',(160,120),'white');draw=ImageDraw.Draw(image)
        draw.rectangle((10,60,110,90),fill='#00BFA5');draw.rectangle((125,10,145,30),fill='#00BFA5')
        code,data,calls=self.create_image(image,[{'action':'extract','color':'#00BFA5','crop':[0,0,160,120]},
            {'action':'ask','question':'请选候选'}])
        endpoint='/api/tasks/'+data['task_id']+'/resume'
        selection=data['component_selection']
        with patch.dict(os.environ,{'RIDE_API_BASE':f'http://127.0.0.1:{server.server_port}/v1',
                                   'RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
            result=json.loads(self.request(endpoint,{'review_continue':True,'consent_upload':True})[2])
        self.assertEqual(result['component_selection'],selection)
        self.assertTrue(result['component_selection']['requires_selection'])
        self.assertFalse(result['centerlined']);self.assertFalse(result['user_components_applied'])
        self.assertEqual(result['task_id'],data['task_id'])
        from ride_agent import start, run, ConsentedPlanner
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            from pathlib import Path
            source=Path(root)/'s.png';Image.new('RGB',(40,40),'white').save(source)
            state=start(source,Path(root)/'task')
            with patch.dict(os.environ,{'RIDE_API_BASE':f'http://127.0.0.1:{server.server_port}/v1',
                                       'RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
                result=run(state,Path(root)/'task',ConsentedPlanner())
            self.assertFalse(result['centerlined']);self.assertFalse(result['extracted'])
            self.assertEqual(result['error_category'],'api_auth')

if __name__=='__main__':unittest.main()
