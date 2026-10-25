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
import admin_area


class TimeoutDiagnosticsTests(unittest.TestCase):
    def response(self, body):
        response = Mock(status=200, headers={})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = io.BytesIO(body).read
        return response

    def test_connect_timeout_and_wrapped_timeout_safe(self):
        for error in [TimeoutError('SECRET'), urllib.error.URLError(TimeoutError('SECRET'))]:
            opener = Mock(); opener.open.side_effect = error
            with patch('map_fetch.urllib.request.build_opener', return_value=opener), self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch._download('safe')
            d = caught.exception.diagnostic()
            self.assertEqual(d['reason_code'], 'timeout')
            self.assertEqual(d['transport']['phase'], 'connect')
            self.assertFalse(d['transport']['response_received'])
            self.assertNotIn('SECRET', json.dumps(d))
            self.assertEqual(opener.open.call_count, 1)

    def test_response_headers_timeout_after_connection(self):
        def build(*handlers):
            handler = handlers[-1]
            def do_open(connection, req, **kw):
                connection('example.invalid').connect()
                raise TimeoutError('SECRET')
            handler.do_open = do_open
            opener = Mock(); opener.open.side_effect = lambda req, **kw: handler.https_open(req)
            return opener
        with patch('map_fetch.http.client.HTTPSConnection.connect'), patch('map_fetch.urllib.request.build_opener', side_effect=build), self.assertRaises(map_fetch.MapFetchError) as caught:
            map_fetch._download('safe')
        self.assertEqual(caught.exception.transport['phase'], 'response_headers')
        self.assertTrue(caught.exception.transport['connection_established'])
        self.assertFalse(caught.exception.transport['response_received'])

    def test_read_timeout_partial_bytes(self):
        response = self.response(b'')
        response.read.side_effect = [b'{', TimeoutError('SECRET')]
        opener = Mock(); opener.open.return_value = response
        with patch('map_fetch.urllib.request.build_opener', return_value=opener), self.assertRaises(map_fetch.MapFetchError) as caught:
            map_fetch._download('safe')
        self.assertEqual(caught.exception.transport['phase'], 'read_response')
        self.assertTrue(caught.exception.transport['response_received'])
        self.assertEqual(caught.exception.transport['bytes_received'], 1)

    def test_budget_exhausted_does_not_connect(self):
        with patch('map_fetch.urllib.request.build_opener') as build, self.assertRaises(map_fetch.MapFetchError) as caught:
            map_fetch._download('safe', deadline=time.monotonic()-1)
        build.assert_not_called()
        self.assertEqual(caught.exception.transport['phase'], 'total_budget')

    def test_server_remark_does_not_become_empty_candidates(self):
        for remark, code in [('runtime error: Query timed out SECRET', 'server_timeout'), ('SECRET error', 'response_incomplete')]:
            opener = Mock(); opener.open.return_value = self.response(json.dumps({'remark': remark, 'elements': []}).encode())
            with patch('map_fetch.urllib.request.build_opener', return_value=opener), tempfile.TemporaryDirectory() as d, self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch.fetch_named_roads('上海市普陀区', ['合成路'], Path(d)/'map.osm', consent_download=True)
            diagnostic = caught.exception.diagnostic()
            self.assertEqual(diagnostic['reason_code'], code)
            self.assertEqual(diagnostic['transport']['phase'], 'server_execution')
            self.assertEqual(diagnostic['transport']['query_stage'], 'admin_area')
            self.assertTrue(diagnostic['transport']['response_received'])
            self.assertEqual(diagnostic['area_resolution']['status'], 'unavailable')
            self.assertNotIn('SECRET', json.dumps(diagnostic))
            self.assertEqual(opener.open.call_count, 1)

    def test_timeout_not_evidence_for_missing_parent(self):
        with self.assertRaises(map_fetch.MapFetchError) as caught:
            admin_area.resolve(['上海市', '普陀区'], Mock(side_effect=map_fetch.MapFetchError('safe', code='timeout')), 1, map_fetch.MapFetchError, map_fetch._identifier, map_fetch._reject_constant)
        self.assertEqual(caught.exception.area_resolution['status'], 'unavailable')
        self.assertEqual(caught.exception.area_resolution['candidates'], [])

    def test_real_response_path_uses_read1_and_remaining_socket_timeout(self):
        class Response(http.client.HTTPResponse):
            def __init__(self):
                self.status = 200
                self.headers = {}
                self.fp = Mock()
                self.read1 = Mock(side_effect=[b'{}', b''])
                self.read = Mock(side_effect=AssertionError('must use read1'))
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        response = Response()
        opener = Mock(); opener.open.return_value = response
        with patch('map_fetch.urllib.request.build_opener', return_value=opener):
            self.assertEqual(map_fetch._download('safe'), b'{}')
        response.read.assert_not_called()
        self.assertEqual(response.read1.call_count, 2)
        timeout = response.fp.raw._sock.settimeout.call_args.args[0]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)
