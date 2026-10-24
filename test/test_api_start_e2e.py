"""真实网页 HTTP→CLI 子进程→mock 模型 HTTP；不上传任何图片到外网。"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
import test_ride_web as fixtures
from test_route_review import mock_observe, mock_review


class APIStartE2ETests(unittest.TestCase):
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    request = fixtures.WebTests.request

    def model(self, actions, tool_calls=False):
        captured = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                index = min(len(captured)-1,len(actions)-1)
                action = actions[index]
                if callable(action):
                    action = action(captured[-1])
                message = ({'content':None,'tool_calls':[{'id':'mock1','type':'function','function':{
                    'name':action['action'],'arguments':json.dumps({k:v for k,v in action.items() if k!='action'})}}]}
                           if tool_calls else {'content':json.dumps(action)})
                body = json.dumps({'choices':[{'message':message}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(cleanup)
        return server, captured

    def create_image(self, image, actions, tool_calls=False):
        server, captured = self.model(actions,tool_calls)
        buffer = io.BytesIO()
        image.save(buffer,format='PNG')
        self.server.cli_timeout = 90
        with patch.dict(os.environ, {'RIDE_API_BASE':f'http://127.0.0.1:{server.server_port}/v1',
                                    'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-vision'}):
            code, _, body = self.request('/api/tasks', {
                'mode':'api','goal':'pixels','consent_upload':True,
                'image_base64':base64.b64encode(buffer.getvalue()).decode()})
        return code,json.loads(body),captured

    def test_actual_http_cli_api_inspection_extract_delivery(self):
        image = Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(image).line([(10,45),(60,45),(60,10)],fill='#00BFA5',width=5)
        code, data, calls = self.create_image(image,[{'action':'inspect_colors'},
            {'action':'extract','color':'#00BFA5','crop':[0,0,80,60]},
            mock_observe, mock_review, {'action':'deliver','summary':'离线mock当前证据复核交付'}])
        self.assertEqual(code,201,data)
        self.assertEqual(data['status'],'needs_confirmation',data)
        self.assertEqual(len(calls),5)
        self.assertTrue(data['artifacts'])
        self.assertNotIn('error_category',data)
        self.assertFalse(data['verified'])
        self.assertTrue(all('tools' not in call for call in calls))

    def test_unsolicited_tool_calls_explicitly_rejected_not_color(self):
        # 当前协议从始至终要求 content JSON，未发布 tools schema；
        # 模拟真实 tool_calls 结构，证明不再静默误报为颜色裁剪问题。
        code, data, calls = self.create_image(Image.new('RGB',(80,60),'white'),
            [{'action':'extract','color':'#00BFA5','crop':[0,0,80,60]}],True)
        self.assertEqual(code,201,data)
        self.assertEqual(data['status'],'error')
        self.assertEqual(data['error_category'],'api_response_invalid')
        self.assertEqual(data['artifacts'],[])
        self.assertEqual(len(calls),1)

    @unittest.skipUnless(os.environ.get('RIDE_REAL_SCREENSHOT'),'需本地原图')
    def test_old_image_real_web_api_path(self):
        with Image.open(os.environ['RIDE_REAL_SCREENSHOT']) as opened:
            image = opened.convert('RGB')
        code,data,calls = self.create_image(image,[{
            'action':'extract','color':'#6A778B','crop':[235,1200,1010,1420]},
            mock_observe, mock_review, {'action':'deliver','summary':'原图候选mock复核，不代表真值'}])
        self.assertEqual(code,201,data)
        self.assertEqual(data['status'],'needs_confirmation',data)
        self.assertEqual(len(calls),4)
        self.assertTrue(any(a['name']=='centerline/pixel-graph.json' and a['current'] for a in data['artifacts']))

    @unittest.skipUnless(os.environ.get('RIDE_TURNBACK_SCREENSHOT'),'需本地折返原图')
    def test_turnback_real_web_api_path(self):
        with Image.open(os.environ['RIDE_TURNBACK_SCREENSHOT']) as opened:
            image = opened.convert('RGB')
        code,data,calls = self.create_image(image,[{
            'action':'extract','color':'#6A778B','crop':[240,1140,1000,1580]},
            {'action':'ask','question':'顶部回转与右斜箭头区域仍不确定，请确认可见目标片段。'}])
        self.assertEqual(code,201,data)
        # 若分量歧义，必须显式停下让用户选择；不以完成状态掩盖安全停顿。
        self.assertIn(data['status'],('needs_confirmation','waiting_for_user'),data)
        self.assertNotIn('error_category',data)
        self.assertTrue(data['artifacts'])
        self.assertEqual(len(calls),2)


if __name__=='__main__':
    unittest.main()
