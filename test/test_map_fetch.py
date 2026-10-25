"""退休联网入口强拒绝；OSM JSON/XML 本地解析、预算与访问限制仍独立覆盖。"""
import copy
import http.client
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET

import map_fetch


BOUNDS = [116.39, 39.90, 116.40, 39.91]
FIXTURE = {'version': 0.6, 'elements': [
    {'type': 'node', 'id': 1, 'lon': 116.39, 'lat': 39.90},
    {'type': 'node', 'id': 2, 'lon': 116.40, 'lat': 39.91},
    {'type': 'way', 'id': 3, 'nodes': [1, 2],
     'tags': {'highway': 'residential', 'name': 'A & B', 'access': 'private'}}]}


class MapFetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'roads.osm'
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.response.status = 200
        self.response.headers = {}
        self.set_body(json.dumps(FIXTURE).encode())
        self.opener = Mock()
        self.opener.open.return_value = self.response
        transport = patch('map_fetch.urllib.request.build_opener', return_value=self.opener)
        self.build = transport.start()
        self.addCleanup(transport.stop)

    def set_body(self, body):
        self.response.read.side_effect = io.BytesIO(body).read

    def fetch(self, bounds=BOUNDS, **kwargs):
        return map_fetch.fetch_roads(bounds, self.path, consent_download=True, **kwargs)

    def test_local_fixture_conversion_preserves_tags(self):
        xml, nodes, ways, refs = map_fetch._convert(json.dumps(FIXTURE).encode())
        self.assertEqual((nodes, ways, refs), (2,1,2))
        self.path.write_bytes(xml)
        root = ET.parse(self.path).getroot()
        self.assertEqual(root.find('way/tag[@k="name"]').get('v'), 'A & B')
        self.assertEqual(root.find('way/tag[@k="access"]').get('v'), 'private')
        self.build.assert_not_called()

    def test_output_accepted_by_current_road_match(self):
        import road_match
        self.path.write_bytes(map_fetch._convert(json.dumps(FIXTURE).encode())[0])
        nodes, edges = road_match._load_osm(self.path)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(edges, [])
        self.build.assert_not_called()

    def test_retired_osm_refuses_every_consent_value(self):
        for consent in (False,None,1,'yes',True):
            with self.subTest(consent=consent),self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch.fetch_roads(BOUNDS,self.path,consent_download=consent)
            self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.build.assert_not_called();self.opener.open.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_invalid_bounds_no_network(self):
        for bounds in (None, '1,2,3,4', [], [0, 0, 1], [True, 0, .01, .01],
                       [0, 0, float('nan'), .01], [0, 0, .01, float('inf')],
                       [0, 0, '0.01', .01], [180, 0, -180, .01],
                       [-181, 0, -180, .01], [0, 85, .01, 86],
                       [0, 0, 0, .01], [0, 1, .01, 0], [0, 0, .2, .01],
                       [0, 0, .1, .1]):
            with self.subTest(bounds=bounds), self.assertRaises(map_fetch.MapFetchError):
                map_fetch._bounds(bounds)
        self.build.assert_not_called()

    def test_retired_osm_refuses_invalid_output_without_io(self):
        for target in (self.path.parent,self.path.parent/'absent'/'roads.osm',None):
            with self.subTest(target=target),self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch.fetch_roads(BOUNDS,target,consent_download=True)
            self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.build.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_retired_osm_never_reaches_failing_transport(self):
        for error in (urllib.error.URLError('offline'),TimeoutError(),OSError(),urllib.error.HTTPError(map_fetch.ENDPOINT,429,'slow',{},None)):
            self.opener.open.side_effect=error
            with self.subTest(error=type(error).__name__),self.assertRaises(map_fetch.MapFetchError) as caught:
                self.fetch()
            self.assertEqual(caught.exception.code,'osm_network_disabled')
            self.opener.open.assert_not_called()
        self.build.assert_not_called();self.assertFalse(self.path.exists())

    def test_retired_osm_never_reads_partial_body(self):
        self.response.read.side_effect=http.client.IncompleteRead(b'partial')
        with self.assertRaises(map_fetch.MapFetchError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.response.read.assert_not_called();self.opener.open.assert_not_called()
        self.build.assert_not_called();self.assertFalse(self.path.exists())

    def test_retired_osm_ignores_transport_status_and_encoding(self):
        for status,headers in ((503,{}),(200,{'Content-Encoding':'gzip'})):
            self.response.status=status;self.response.headers=headers
            with self.subTest(status=status),self.assertRaises(map_fetch.MapFetchError) as caught:
                self.fetch()
            self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.build.assert_not_called();self.response.read.assert_not_called()

    def test_redirect_refused(self):
        with self.assertRaisesRegex(map_fetch.MapFetchError, 'redirect'):
            map_fetch._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid/')

    def test_retired_osm_never_reads_headers_or_body(self):
        for length in ('10000001','-1','not-an-int'):
            self.response.headers={'Content-Length':length}
            with self.subTest(length=length),self.assertRaises(map_fetch.MapFetchError) as caught:
                self.fetch()
            self.assertEqual(caught.exception.code,'osm_network_disabled')
        with patch('map_fetch.MAX_RESPONSE_BYTES',10):
            self.set_body(b'x'*11)
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch._download('test',max_bytes=10)
        self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.build.assert_not_called();self.response.read.assert_not_called()

    def test_retired_download_never_starts_a_time_budget(self):
        with patch('map_fetch.time.monotonic',side_effect=AssertionError('退休入口不应启动预算')) as clock:
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch._download('test',deadline=-1)
            clock.assert_not_called()
        self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.build.assert_not_called()

    def test_malformed_and_error_json_preserve_existing(self):
        self.path.write_text('existing')
        for body in (b'not JSON',b'\xff',b'[]',b'{}',b'{"elements": null}',b'{"elements": [], "remark": "runtime error"}',b'{"elements": [NaN]}'):
            with self.subTest(body=body),self.assertRaises(map_fetch.MapFetchError):
                map_fetch._convert(body)
            self.assertEqual(self.path.read_text(),'existing')
        self.build.assert_not_called()

    def test_invalid_elements(self):
        bad = [None, {}, {'type': 'relation', 'id': 1},
               {'type': 'node', 'id': True, 'lon': 0, 'lat': 0},
               {'type': 'node', 'id': 1, 'lon': 181, 'lat': 0},
               {'type': 'node', 'id': 1, 'lon': 0, 'lat': 86},
               {'type': 'node', 'id': 1, 'lon': True, 'lat': 0},
               {'type': 'way', 'id': 3, 'nodes': [1, 999], 'tags': {'highway': 'road'}},
               {'type': 'way', 'id': 3, 'nodes': [1], 'tags': {'highway': 'road'}},
               {'type': 'way', 'id': 3, 'nodes': [1, False], 'tags': {'highway': 'road'}},
               {'type': 'way', 'id': 3, 'nodes': [1, 2], 'tags': {}},
               {'type': 'way', 'id': 3, 'nodes': [1, 2], 'tags': {'highway': 4}},
               {'type': 'way', 'id': 3, 'nodes': [1, 2], 'tags': {'highway': 'road\x00'}},
               {'type': 'way', 'id': 3, 'nodes': [1, 2], 'tags': {'highway': 'road\ud800'}}]
        for element in bad:
            self.set_body(json.dumps({'elements': FIXTURE['elements'][:2] + [element]}).encode())
            with self.subTest(element=element), self.assertRaises(map_fetch.MapFetchError):
                map_fetch._convert(self.response.read())
        self.assertFalse(self.path.exists())

    def test_duplicate_elements_and_budgets(self):
        for index in (0, 2):
            data = copy.deepcopy(FIXTURE)
            data['elements'].append(data['elements'][index])
            self.set_body(json.dumps(data).encode())
            with self.assertRaisesRegex(map_fetch.MapFetchError, 'Duplicate'):
                map_fetch._convert(self.response.read())
        for setting in ('MAX_NODES', 'MAX_WAYS', 'MAX_REFERENCES', 'MAX_OUTPUT_BYTES'):
            self.set_body(json.dumps(FIXTURE).encode())
            with self.subTest(setting=setting), patch.object(map_fetch, setting, 0):
                with self.assertRaises(map_fetch.MapFetchError):
                    map_fetch._convert(self.response.read())

    def test_retired_fetch_never_replaces_existing_map(self):
        self.path.write_text('existing')
        with patch('map_fetch.os.replace',side_effect=OSError('disk error')) as replace:
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                self.fetch()
            replace.assert_not_called()
        self.assertEqual(caught.exception.code,'osm_network_disabled')
        self.build.assert_not_called()
        self.assertEqual(self.path.read_text(),'existing')
        self.assertEqual(list(self.path.parent.glob('.map-fetch-*')),[])

    def test_local_empty_fixture_conversion(self):
        xml, nodes, ways, refs = map_fetch._convert(b'{"elements": []}')
        self.assertEqual((nodes,ways,refs),(0,0,0))
        self.assertEqual(ET.fromstring(xml).tag,'osm')
        self.build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
