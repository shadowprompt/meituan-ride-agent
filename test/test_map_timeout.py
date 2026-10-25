"""离线分阶段超时、预算及远端正文不泄露回归。"""
import io
import http.client
import json
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch
import map_fetch
import amap_fetch
from test_amap_provider import KEY, raw
import admin_area


class TimeoutDiagnosticsTests(unittest.TestCase):
    def response(self, body):
        response = Mock(status=200, headers={})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = io.BytesIO(body).read
        return response

    def test_amap_connect_timeout_and_network_error_safe(self):
        for error in (TimeoutError('SECRET'),OSError('SECRET')):
            with patch('amap_fetch.http.client.HTTPSConnection',side_effect=error) as factory:
                with self.assertRaises(map_fetch.MapFetchError) as caught:
                    amap_fetch._request({},KEY,time.monotonic()+30)
            self.assertEqual(caught.exception.code,'timeout' if isinstance(error,TimeoutError) else 'network_error')
            self.assertNotIn('SECRET',json.dumps(caught.exception.diagnostic()))
            factory.assert_called_once()

    def test_response_headers_timeout_after_connection(self):
        connection=Mock();connection.getresponse.side_effect=TimeoutError('SECRET')
        with patch('amap_fetch.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                amap_fetch._request({},KEY,time.monotonic()+30)
        self.assertEqual(caught.exception.code,'timeout')
        connection.connect.assert_called_once();connection.request.assert_called_once();connection.close.assert_called_once()
        self.assertNotIn('SECRET',str(caught.exception.diagnostic()))

    def test_read_timeout_partial_bytes(self):
        connection=Mock();response=connection.getresponse.return_value
        response.status=200;response.getheader.side_effect=lambda n,d=None: d
        response.read1.side_effect=[b'{',TimeoutError('SECRET')]
        with patch('amap_fetch.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                amap_fetch._request({},KEY,time.monotonic()+30)
        self.assertEqual(caught.exception.code,'timeout')
        self.assertEqual(response.read1.call_count,2)
        connection.close.assert_called_once()
        self.assertNotIn('SECRET',str(caught.exception.diagnostic()))

    def test_budget_exhausted_does_not_connect(self):
        with patch('amap_fetch.http.client.HTTPSConnection') as factory:
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                amap_fetch._request({},KEY,time.monotonic()-1)
        factory.assert_not_called()
        self.assertEqual(caught.exception.code,'timeout')
        with patch('urllib.request.build_opener') as old:
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch._download('safe',deadline=time.monotonic()-1)
            old.assert_not_called()
        self.assertEqual(caught.exception.code,'osm_network_disabled')

    def test_server_remark_does_not_become_empty_candidates(self):
        for payload in (b'{"remark":"SECRET timed out","elements":[]}',b'{"status":"0","info":"SECRET"}'):
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                amap_fetch._candidates(payload)
            self.assertIn(caught.exception.code,('response_invalid','amap_service_error'))
            self.assertNotIn('SECRET',str(caught.exception.diagnostic()))

    def test_timeout_not_evidence_for_missing_parent(self):
        with self.assertRaises(map_fetch.MapFetchError) as caught:
            admin_area.resolve(['上海市', '普陀区'], Mock(side_effect=map_fetch.MapFetchError('safe', code='timeout')), 1, map_fetch.MapFetchError, map_fetch._identifier, map_fetch._reject_constant)
        self.assertEqual(caught.exception.area_resolution['status'], 'unavailable')
        self.assertEqual(caught.exception.area_resolution['candidates'], [])

    def test_real_response_path_uses_read1_and_remaining_socket_timeout(self):
        connection=Mock();response=connection.getresponse.return_value
        response.status=200;response.getheader.side_effect=lambda n,d=None: d
        response.read1.side_effect=[raw(),b'']
        response.read.side_effect=AssertionError('must use read1')
        with patch('amap_fetch.http.client.HTTPSConnection',return_value=connection):
            self.assertEqual(amap_fetch._request({},KEY,time.monotonic()+30),raw())
        response.read.assert_not_called()
        self.assertEqual(response.read1.call_count,2)
        timeout=response.fp.raw._sock.settimeout.call_args.args[0]
        self.assertGreater(timeout,0);self.assertLessEqual(timeout,30)
        connection.close.assert_called_once()
