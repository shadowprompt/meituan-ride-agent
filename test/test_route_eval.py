import json
import math
from pathlib import Path
import tempfile
import unittest
from route_eval import load_route, score_route, evaluate, RoadIndex


def geo(points):
    return {'type': 'Feature', 'properties': {'coordinate_system': 'WGS84'},
            'geometry': {'type': 'LineString', 'coordinates': points}}


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.a = [[121.5, 31.2], [121.501, 31.2], [121.501, 31.201]]

    def test_identity(self):
        result = score_route(self.a, self.a)
        self.assertAlmostEqual(result['score'], 1)
        self.assertTrue(result['geometry_gate_passed'])

    def test_density_invariance(self):
        dense = [self.a[0], [121.5005, 31.2], self.a[1], self.a[2]]
        self.assertLess(score_route(self.a, dense)['frechet_m'], 0.001)

    def test_shift_rejected(self):
        shifted = [[x, y-0.00225] for x, y in self.a]
        self.assertFalse(score_route(self.a, shifted)['geometry_gate_passed'])

    def test_reverse_penalized(self):
        self.assertLess(score_route(self.a, self.a[::-1])['score'], 0.6)

    def test_retrace_not_collapsed(self):
        journey = self.a + self.a[-2::-1]
        shortcut = score_route(journey, self.a)
        self.assertFalse(shortcut['geometry_gate_passed'])
        self.assertAlmostEqual(shortcut['length_ratio'], 0.5)
        self.assertGreater(score_route(journey, journey)['score'], shortcut['score'])

    def test_invalid_tolerance(self):
        for value in [0, -1, math.nan, math.inf]:
            with self.assertRaises(ValueError):
                score_route(self.a, self.a, value)

    def test_input_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'route.json'
            for points in [[], [[1, 2]], [[True, 2], [3, 4]], [[math.nan, 2], [3, 4]],
                           [[181, 2], [3, 4]], [[1, 2], [1, 2]]]:
                path.write_text(json.dumps(geo(points)))
                with self.assertRaises(ValueError):
                    load_route(path)
            path.write_text(json.dumps(geo(self.a+self.a[-2::-1])))
            self.assertEqual(len(load_route(path)), 5)
            obj = geo(self.a)
            obj['properties'] = {}
            path.write_text(json.dumps(obj))
            with self.assertRaises(ValueError):
                load_route(path)

    def test_report_never_promotes_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'route.json'
            path.write_text(json.dumps(geo(self.a)))
            report = evaluate(path, [path])
            self.assertFalse(report['verified'])
            self.assertTrue(report['ambiguous'])
            self.assertIsNone(report['candidates'][0]['road_check'])
            self.assertEqual(report['status'], 'needs_confirmation')

    def test_exact_osm_edges_not_jumps(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'map.osm'
            path.write_text('''<osm><node id="1" lon="121.5" lat="31.2"/>
<node id="2" lon="121.501" lat="31.2"/><node id="3" lon="121.501" lat="31.201"/>
<way id="4"><nd ref="1"/><nd ref="2"/><nd ref="3"/><tag k="highway" v="residential"/></way></osm>''')
            index = RoadIndex(path)
            self.assertTrue(index.check(self.a)['all_segments_are_osm_edges'])
            self.assertTrue(index.check(self.a[::-1])['all_segments_are_osm_edges'])
            self.assertFalse(index.check([self.a[0], self.a[2]])['all_segments_are_osm_edges'])
            self.assertFalse(index.check(self.a)['cycling_legality_verified'])


if __name__ == '__main__':
    unittest.main()
