"""Offline synthetic graph tests. No network, credentials or real GPS needed."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import road_match


class RoadMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.segments = self.root / 'registered-segments.geojson'
        self.osm = self.root / 'map.osm'
        self.out = self.root / 'out'

    def inputs(self, segments, nodes, ways, tags=''):
        self.segments.write_text(json.dumps({'type': 'FeatureCollection', 'features': [
            {'type': 'Feature', 'properties': {'coordinate_system': 'WGS84'},
             'geometry': {'type': 'LineString', 'coordinates': line}} for line in segments]}))
        self.osm.write_text('<osm version="0.6">' + ''.join(
            f'<node id="{i}" lon="{x}" lat="{y}"/>' for i, (x, y) in enumerate(nodes, 1))
            + ''.join(f'<way id="{k}">' + ''.join(f'<nd ref="{i}"/>' for i in way)
                      + '<tag k="highway" v="residential"/>' + tags + '</way>'
                      for k, way in enumerate(ways, 1)) + '</osm>')

    def run_match(self, tolerance=30):
        return road_match.match(self.segments, self.osm, self.out, tolerance)

    def test_identity_and_artifacts(self):
        line = [[0, 0], [.001, 0], [.002, 0]]
        self.inputs([line], line, [[1, 2, 3]])
        report = self.run_match()
        self.assertEqual(report['status'], 'needs_confirmation')
        self.assertEqual(report['matched_segment_count'], 1)
        self.assertFalse(report['verified'])
        self.assertFalse(report['original_gps'])
        self.assertEqual(json.loads((self.out/'road-match.json').read_text()), report)
        features = json.loads((self.out/'road-candidates.geojson').read_text())['features']
        coords = features[0]['geometry']['coordinates']
        self.assertEqual(coords[0], line[0])
        self.assertEqual(coords[-1], line[-1])
        self.assertTrue(all(p[1] == 0 for p in coords))
        with Image.open(self.out/'road-overlay.png') as image:
            self.assertEqual(image.size, (1000, 780))
            self.assertGreater(len(image.getcolors(1000000)), 2)

    def test_disconnected_no_jump(self):
        line = [[0, 0], [.001, 0], [.002, 0], [.003, 0]]
        self.inputs([line], line, [[1, 2], [3, 4]])
        self.assertEqual(self.run_match()['status'], 'no_match')

    def test_parallel_distant(self):
        reference = [[0, 0], [.001, 0], [.002, 0]]
        self.inputs([reference], [[x, .002] for x, _ in reference], [[1, 2, 3]])
        self.assertEqual(self.run_match()['matched_segment_count'], 0)

    def test_shape_detour_rejected(self):
        # Endpoints coincide but road veers hundreds of metres outside corridor.
        reference = [[0, 0], [.002, 0]]
        road = [[0, 0], [0, .003], [.002, .003], [.002, 0]]
        self.inputs([reference], road, [[1, 2, 3, 4]])
        self.assertEqual(self.run_match()['status'], 'no_match')

    def test_out_and_back_not_shortcut(self):
        reference = [[0, 0], [.001, 0], [.002, 0], [.001, 0], [0, 0]]
        self.inputs([reference], reference[:3], [[1, 2, 3]])
        self.assertEqual(self.run_match()['matched_segment_count'], 1)
        coords = json.loads((self.out/'road-candidates.geojson').read_text())['features'][0]['geometry']['coordinates']
        self.assertEqual(coords[0], coords[-1])
        # Candidate may turn within tolerance, but cannot collapse the return leg.
        self.assertLessEqual(abs(max(p[0] for p in coords) - .002) * 111195, 30)

    def test_independent_branches_and_partial(self):
        a, b, c, far = [0, 0], [.001, 0], [.001, .001], [.02, .02]
        self.inputs([[a, b], [b, c], [far, [.021, .02]]], [a, b, c], [[1, 2], [2, 3]])
        report = self.run_match()
        self.assertEqual(report['input_segment_count'], 3)
        self.assertEqual(report['matched_segment_count'], 2)
        features = json.loads((self.out/'road-candidates.geojson').read_text())['features']
        self.assertEqual([f['properties']['input_segment_index'] for f in features], [0, 1])
        self.assertTrue(all(f['geometry']['type'] == 'LineString' for f in features))

    def test_restricted_ways(self):
        line = [[0, 0], [.001, 0]]
        for tags in ['<tag k="access" v="private"/>', '<tag k="access" v="no"/>',
                     '<tag k="bicycle" v="no"/>', '<tag k="highway" v="motorway"/>',
                     '<tag k="highway" v="motorway_link"/>']:
            with self.subTest(tags=tags):
                self.inputs([line], line, [[1, 2]], tags)
                self.assertEqual(self.run_match()['status'], 'no_match')

    def test_missing_osm_node_does_not_bridge(self):
        line = [[0, 0], [.001, 0]]
        self.inputs([line], line, [[1, 999, 2]])
        self.assertEqual(self.run_match()['status'], 'no_match')

    def test_invalid_inputs(self):
        line = [[0, 0], [.001, 0]]
        for point in [[float('nan'), 0], [float('inf'), 0], [181, 0], [0, 86], [True, 0], [0]]:
            with self.subTest(point=point):
                self.inputs([[line[0], point]], line, [[1, 2]])
                with self.assertRaises(ValueError):
                    self.run_match()
        self.inputs([line], line, [[1, 2]])
        data = json.loads(self.segments.read_text())
        del data['features'][0]['properties']['coordinate_system']
        self.segments.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            self.run_match()
        for tolerance in [0, -1, 501, float('nan'), True, '50']:
            with self.assertRaises(ValueError):
                self.run_match(tolerance)

    def test_osm_safety(self):
        line = [[0, 0], [.001, 0]]
        self.inputs([line], line, [[1, 2]])
        for xml in ['<!DOCTYPE osm [<!ENTITY x "test">]><osm/>', '<osm><node id="1" lon="nan" lat="0"/></osm>', '<bad/>', '<osm>']:
            self.osm.write_text(xml)
            with self.assertRaises(ValueError):
                self.run_match()
        self.inputs([line], line, [[1, 2]])
        with patch.object(road_match, 'MAX_NODES', 1):
            with self.assertRaises(ValueError):
                self.run_match()
        self.segments.write_bytes(b' ' * 5000001)
        with self.assertRaises(ValueError):
            self.run_match()

    def test_dense_registered_line(self):
        line = [[i * .002 / 1200, 0] for i in range(1201)]
        self.inputs([line], [[0, 0], [.001, 0], [.002, 0]], [[1, 2, 3]])
        report = self.run_match()
        self.assertEqual(report['matched_segment_count'], 1)
        self.assertFalse(any('budget exceeded' in w for w in report['warnings']))

    def test_dense_retrace_preserved(self):
        outbound = [[i * .002 / 600, 0] for i in range(601)]
        line = outbound + outbound[-2::-1]
        self.inputs([line], [[0, 0], [.001, 0], [.002, 0]], [[1, 2, 3]])
        report = self.run_match()
        self.assertEqual(report['matched_segment_count'], 1)
        coords = json.loads((self.out/'road-candidates.geojson').read_text())['features'][0]['geometry']['coordinates']
        self.assertEqual(coords[0], coords[-1])
        self.assertLessEqual(abs(max(p[0] for p in coords) - .002) * 111195, 30)

    def test_simplify_finite_segment_retains_reversal(self):
        import numpy as np
        points = np.asarray([[0., 0.], [50., 0.], [100., 0.], [50., 0.], [0., 0.]])
        reduced = road_match._simplify(points, 1.)
        self.assertEqual(reduced.tolist(), [[0., 0.], [100., 0.], [0., 0.]])
        with self.assertRaises(ValueError):
            road_match._sample(np.asarray([[0., 0.], [20000., 0.]]), 1.)

    def test_coincident_invalid(self):
        self.inputs([[[0, 0], [0, 0]]], [[0, 0]], [])
        with self.assertRaises(ValueError):
            self.run_match()


if __name__ == '__main__':
    unittest.main()
