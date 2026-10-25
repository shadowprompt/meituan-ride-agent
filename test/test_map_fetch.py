"""Fully offline download tests: every transport is mocked."""
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

    def test_valid_fixture_and_query(self):
        report = self.fetch()
        self.assertEqual(report['bounds'], BOUNDS)
        self.assertEqual((report['node_count'], report['way_count'], report['reference_count']), (2, 1, 2))
        self.assertEqual(report['status'], 'needs_confirmation')
        self.assertFalse(report['verified'])
        self.assertFalse(report['original_gps'])
        self.assertEqual(report['format'], 'osm_xml')
        root = ET.parse(self.path).getroot()
        self.assertEqual(root.tag, 'osm')
        self.assertEqual(root.find('way/tag[@k="name"]').get('v'), 'A & B')
        self.assertEqual(root.find('way/tag[@k="access"]').get('v'), 'private')
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, map_fetch.ENDPOINT)
        self.assertEqual(request.method, 'POST')
        query = urllib.parse.parse_qs(request.data.decode())['data'][0]
        self.assertIn('way["highway"](39.900000000000,116.390000000000,39.910000000000,116.400000000000)', query)
        self.assertIn('(._;>;);out body;', query)
        self.assertNotIn('relation', query)
        self.assertEqual(self.opener.open.call_args.kwargs['timeout'], 30)
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertEqual(list(self.path.parent.glob('.map-fetch-*')), [])

    def test_output_accepted_by_current_road_match(self):
        # Matcher accepts OSM XML, not Overpass JSON. Restricted tags survive
        # conversion so its existing bicycle/access filtering still applies.
        import road_match
        self.fetch()
        nodes, edges = road_match._load_osm(self.path)
        self.assertEqual(len(nodes), 2)
        self.assertEqual(edges, [])

    def test_consent_required_per_call(self):
        for consent in (False, None, 1, 'yes'):
            with self.subTest(consent=consent), self.assertRaisesRegex(map_fetch.MapFetchError, 'consent'):
                map_fetch.fetch_roads(BOUNDS, self.path, consent_download=consent)
        self.build.assert_not_called()

    def test_invalid_bounds_no_network(self):
        for bounds in (None, '1,2,3,4', [], [0, 0, 1], [True, 0, .01, .01],
                       [0, 0, float('nan'), .01], [0, 0, .01, float('inf')],
                       [0, 0, '0.01', .01], [180, 0, -180, .01],
                       [-181, 0, -180, .01], [0, 85, .01, 86],
                       [0, 0, 0, .01], [0, 1, .01, 0], [0, 0, .2, .01],
                       [0, 0, .1, .1]):
            with self.subTest(bounds=bounds), self.assertRaises(map_fetch.MapFetchError):
                self.fetch(bounds)
        self.build.assert_not_called()

    def test_invalid_output_before_network(self):
        for target in (self.path.parent, self.path.parent / 'absent' / 'roads.osm', None):
            with self.subTest(target=target), self.assertRaises(map_fetch.MapFetchError):
                map_fetch.fetch_roads(BOUNDS, target, consent_download=True)
        self.build.assert_not_called()

    def test_network_errors_no_retry(self):
        for error in (urllib.error.URLError('offline'), TimeoutError(), OSError(),
                      urllib.error.HTTPError(map_fetch.ENDPOINT, 429, 'slow down', {}, None)):
            with self.subTest(error=error):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(map_fetch.MapFetchError):
                    self.fetch()
                self.assertEqual(self.opener.open.call_count, 1)
                self.assertFalse(self.path.exists())

    def test_interrupted_body_no_retry_or_output(self):
        self.response.read.side_effect = http.client.IncompleteRead(b'partial')
        with self.assertRaisesRegex(map_fetch.MapFetchError, 'network failure'):
            self.fetch()
        self.assertEqual(self.opener.open.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_unexpected_status_and_encoding(self):
        self.response.status = 503
        with self.assertRaisesRegex(map_fetch.MapFetchError, '503'):
            self.fetch()
        self.response.status = 200
        self.response.headers = {'Content-Encoding': 'gzip'}
        with self.assertRaisesRegex(map_fetch.MapFetchError, 'compressed'):
            self.fetch()

    def test_redirect_refused(self):
        with self.assertRaisesRegex(map_fetch.MapFetchError, 'redirect'):
            map_fetch._NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid/')

    def test_response_byte_limit_and_headers(self):
        for length in ('10000001', '-1', 'not-an-int'):
            self.response.headers = {'Content-Length': length}
            with self.subTest(length=length), self.assertRaises(map_fetch.MapFetchError):
                self.fetch()
        self.response.headers = {}
        with patch('map_fetch.MAX_RESPONSE_BYTES', 10):
            self.set_body(b'x' * 11)
            with self.assertRaisesRegex(map_fetch.MapFetchError, 'byte limit'):
                self.fetch()
            self.set_body(b'x' * 10)
            self.assertEqual(map_fetch._download('test'), b'x' * 10)

    def test_wall_clock_deadline(self):
        with patch('map_fetch.time.monotonic', side_effect=[0, 0, 31, 31]):
            with self.assertRaisesRegex(map_fetch.MapFetchError, 'deadline'):
                self.fetch()

    def test_malformed_and_error_json_preserve_existing(self):
        self.path.write_text('existing')
        for body in (b'not JSON', b'\xff', b'[]', b'{}', b'{"elements": null}',
                     b'{"elements": [], "remark": "runtime error"}',
                     b'{"elements": [NaN]}'):
            self.set_body(body)
            with self.subTest(body=body), self.assertRaises(map_fetch.MapFetchError):
                self.fetch()
            self.assertEqual(self.path.read_text(), 'existing')

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
                self.fetch()
        self.assertFalse(self.path.exists())

    def test_duplicate_elements_and_budgets(self):
        for index in (0, 2):
            data = copy.deepcopy(FIXTURE)
            data['elements'].append(data['elements'][index])
            self.set_body(json.dumps(data).encode())
            with self.assertRaisesRegex(map_fetch.MapFetchError, 'Duplicate'):
                self.fetch()
        for setting in ('MAX_NODES', 'MAX_WAYS', 'MAX_REFERENCES', 'MAX_OUTPUT_BYTES'):
            self.set_body(json.dumps(FIXTURE).encode())
            with self.subTest(setting=setting), patch.object(map_fetch, setting, 0):
                with self.assertRaises(map_fetch.MapFetchError):
                    self.fetch()

    def test_atomic_replace_failure_cleans_temp_preserves_old(self):
        self.path.write_text('existing')
        with patch('map_fetch.os.replace', side_effect=OSError('disk error')):
            with self.assertRaisesRegex(map_fetch.MapFetchError, 'atomically'):
                self.fetch()
        self.assertEqual(self.path.read_text(), 'existing')
        self.assertEqual(list(self.path.parent.glob('.map-fetch-*')), [])

    def test_empty_success(self):
        self.set_body(b'{"elements": []}')
        report = self.fetch()
        self.assertEqual(report['status'], 'no_roads')
        self.assertEqual(report['way_count'], 0)
        self.assertEqual(ET.parse(self.path).getroot().tag, 'osm')


if __name__ == '__main__':
    unittest.main()
