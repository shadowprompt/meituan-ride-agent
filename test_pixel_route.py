"""人工有序像素路线的真实回环 HTTP、安全及图片绑定回归，不使用网络 API。"""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import http.client

from PIL import Image
from ride_web import RideServer


class PixelRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = RideServer(0, output_root=self.root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=20)
        h = {'Content-Type': 'application/json', 'X-CSRF-Token': self.server.token}
        h.update(headers or {})
        conn.request('POST' if data is not None else 'GET', path,
                     json.dumps(data) if data is not None else None, h)
        result = conn.getresponse()
        body = result.read()
        conn.close()
        return result.status, body

    def image(self, fmt='PNG', orientation=None):
        image = Image.new('RGB', (80, 60), '#dadada')
        stream = io.BytesIO()
        kwargs = {}
        if orientation:
            exif = image.getexif()
            exif[274] = orientation
            kwargs['exif'] = exif
        image.save(stream, format=fmt, **kwargs)
        return stream.getvalue()

    def create(self, raw=None):
        code, body = self.request('/api/pixel-routes', {'image_base64': base64.b64encode(raw or self.image()).decode()})
        self.assertEqual(code, 201, body)
        data = json.loads(body)
        return '/api/pixel-routes/'+data['task_id'], data

    def payload(self, data, points=None):
        route = data['route']
        return {k: route[k] for k in ('image', 'revision', 'view_crop')} | {'points': points if points is not None else [[10, 20], [30, 40], [10, 20], [30, 40]]}

    def test_end_to_end_repeated_order_crop_export_and_reopen(self):
        with patch('ride_web.subprocess.run') as cli, patch.dict(os.environ, {}, clear=True):
            path, data = self.create()
            self.assertEqual(self.request(path+'/export')[0], 409)
            payload = self.payload(data)
            payload['view_crop'] = [5, 10, 75, 55]
            code, body = self.request(path, payload)
            self.assertEqual(code, 200, body)
            saved = json.loads(body)
            self.assertEqual(saved['route']['revision'], 1)
            self.assertEqual(json.loads(self.request(path)[1]), saved)
            route = json.loads(self.request(path+'/export')[1])
            self.assertEqual(route['points'], payload['points'])
            self.assertEqual(route['segments'], [{'from':0,'to':1},{'from':1,'to':2},{'from':2,'to':3}])
            self.assertEqual(route['view_crop'], payload['view_crop'])
            self.assertFalse(route['verified'])
            self.assertFalse(route['original_gps'])
            self.assertEqual(route['source'], 'manual_click_approximation')
            self.assertNotIn('timestamps', route)
            self.assertNotIn('speeds', route)
            audit = json.loads((self.root/data['task_id']/'manual-audit.json').read_text())
            self.assertEqual(len(audit), 2)
            self.assertEqual(audit[-1]['point_count'], 4)
            self.assertTrue(self.request(path+'/image')[1].startswith(b'\x89PNG'))
            cli.assert_not_called()

    def test_saving_empty_draft_and_conflict(self):
        path, data = self.create()
        self.assertEqual(self.request(path, self.payload(data, []))[0], 200)
        self.assertEqual(self.request(path, self.payload(data))[0], 409)
        self.assertEqual(self.request(path+'/export')[0], 409)

    def test_invalid_points_crops_and_extra_fields(self):
        path, data = self.create()
        for points in ([[True, 5]], [[-1, 0]], [[80, 0]], [[0, 60]], [[float('nan'), 2]],
                       [[float('inf'), 1]], [[1, 2, 3]], 'x', [[1, 2]]*10001):
            self.assertEqual(self.request(path, self.payload(data, points))[0], 400)
        for crop in ([0, 0, 81, 60], [2, 0, 1, 60], [0, 0, 80.0, 60], [False, 0, 80, 60]):
            payload = self.payload(data)
            payload['view_crop'] = crop
            self.assertEqual(self.request(path, payload)[0], 400)
        payload = self.payload(data) | {'verified': True}
        self.assertEqual(self.request(path, payload)[0], 400)

    def test_binding_and_source_change_rejected(self):
        path, data = self.create()
        payload = self.payload(data)
        payload['image'] = payload['image'] | {'sha256':'forged'}
        self.assertEqual(self.request(path, payload)[0], 409)
        (self.root/data['task_id']/'manual-source').write_bytes(self.image('JPEG'))
        self.assertEqual(self.request(path+'/export')[0], 409)
        self.assertEqual(self.request(path+'/image')[0], 409)

    def test_normal_jpeg_exif_and_fake_tiff(self):
        _, data = self.create(self.image('JPEG', orientation=6))
        self.assertEqual((data['route']['image']['width'], data['route']['image']['height']), (60, 80))
        encoded = base64.b64encode(self.image('TIFF')).decode()
        self.assertEqual(self.request('/api/pixel-routes', {'image_base64':encoded})[0], 400)
        self.assertEqual(self.request('/api/pixel-routes', {'image_base64':'%%%bad'})[0], 400)
        big = io.BytesIO()
        Image.new('L', (2001, 2000)).save(big, format='PNG')
        self.assertEqual(self.request('/api/pixel-routes', {'image_base64':base64.b64encode(big.getvalue()).decode()})[0], 400)

    def test_host_origin_csrf_path_allowlist_and_symlinks(self):
        payload = {'image_base64':base64.b64encode(self.image()).decode()}
        for headers in ({'Host':'evil.example'}, {'Origin':'https://evil.example'},
                        {'X-CSRF-Token':''}, {'Sec-Fetch-Site':'cross-site'}):
            self.assertEqual(self.request('/api/pixel-routes', payload, headers)[0], 403)
        path, data = self.create()
        for suffix in ('/manual-source', '/manual-audit.json', '/image/../../.env', '/%2e%2e', '/export?path=x'):
            self.assertIn(self.request(path+suffix)[0], (403, 404))
        folder = self.root/data['task_id']
        source = folder/'manual-source'
        outside = self.root/'other'
        source.rename(outside)
        source.symlink_to(outside)
        self.assertEqual(self.request(path+'/image')[0], 403)
        self.assertEqual(self.request(path, self.payload(data))[0], 403)

    def test_write_symlink_rejected(self):
        path, data = self.create()
        outside = self.root/'outside'
        outside.write_text('untouched')
        (self.root/data['task_id']/'manual-audit.json.pending').symlink_to(outside)
        self.assertEqual(self.request(path, self.payload(data))[0], 403)
        self.assertEqual(outside.read_text(), 'untouched')

    def test_page_csp_no_remote_scripts(self):
        code, body = self.request('/pixel-route')
        self.assertEqual(code, 200)
        self.assertIn('人工有序像素路线'.encode(), body)
        self.assertNotIn(b'https://', body)
        self.assertIn(b'nonce="', body)

    def test_restart_reopen_without_process_state(self):
        path, data = self.create()
        self.assertEqual(self.request(path, self.payload(data))[0], 200)
        other = RideServer(0, output_root=self.root)
        try:
            from pixel_route import load
            self.assertEqual(load(other, data['task_id'])['points'], self.payload(data)['points'])
        finally:
            other.server_close()

    @unittest.skipUnless(os.environ.get('RIDE_REAL_SCREENSHOT'), '需本地正常编码原图')
    def test_real_screenshot(self):
        self.create(Path(os.environ['RIDE_REAL_SCREENSHOT']).read_bytes())

    @unittest.skipUnless(os.environ.get('RIDE_TURNBACK_SCREENSHOT'), '需本地正常编码折返图')
    def test_turnback_screenshot(self):
        self.create(Path(os.environ['RIDE_TURNBACK_SCREENSHOT']).read_bytes())


if __name__ == '__main__':
    unittest.main()
