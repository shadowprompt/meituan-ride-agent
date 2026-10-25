"""合成图验证工具契约；blind4仅为历史已知走廊的本地复现，不是盲测验收。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import road_corridor as rc


# 全为虚构测试坐标，不代表用户骑行或真实OSM对象。
XML = '''<osm version="0.6">
<node id="1" lon="0" lat="0"/><node id="2" lon="0.001" lat="0"/>
<node id="3" lon="0.002" lat="0"/><node id="4" lon="0.001" lat="0.001"/>
<node id="5" lon="0.003" lat="0"/><node id="6" lon="0.004" lat="0"/>
<way id="10"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
<tag k="highway" v="residential"/><tag k="name" v="合成道路A"/></way>
<way id="20"><nd ref="2"/><nd ref="4"/>
<tag k="highway" v="residential"/><tag k="name" v="合成道路B"/></way>
<way id="30"><nd ref="5"/><nd ref="6"/>
<tag k="highway" v="residential"/><tag k="name" v="合成道路A"/></way>
</osm>'''


def leg(name, *nodes):
    return {'road': {'name': name}, 'via': [{'node_id': n} for n in nodes]}


def plan(*legs):
    return {'schema_version': 1, 'coordinate_system': 'WGS84',
            'source': {'kind': 'model', 'evidence': '明确标注的合成测试假设'}, 'legs': list(legs)}


class CorridorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.osm = self.root / 'synthetic.osm'
        self.osm.write_text(XML, encoding='utf-8')

    def build(self, p, **kw):
        return rc.build_corridor(self.osm, p, **kw)

    def test_connected_traceable_no_network(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('禁止网络')):
            result = self.build(plan(leg('合成道路A', 1, 2), leg('合成道路B', 2, 4)))
        report = result['report']
        self.assertEqual(report['path_node_ids'], [1, 2, 4])
        self.assertEqual(report['edge_way_ids'], [10, 20])
        self.assertFalse(report['verified'])
        self.assertFalse(report['original_gps'])
        self.assertFalse(report['navigation_verified'])
        self.assertIsNone(report['uncertainty']['endpoint_error_m'])
        self.assertEqual(report['source']['kind'], 'model')
        self.assertEqual(len(report['osm_sha256']), 64)
        self.assertEqual(result['geojson']['features'][0]['geometry']['coordinates'], [[0., 0.], [.001, 0.], [.001, .001]])

    def test_reversal_and_repeated_legs_not_global_shortest(self):
        result = self.build(plan(leg('合成道路A', 1, 3, 1, 3), leg('合成道路A', 3, 1)))
        self.assertEqual(result['report']['path_node_ids'], [1, 2, 3, 2, 1, 2, 3, 2, 1])
        self.assertEqual(result['report']['edge_way_ids'], [10] * 8)

    def test_inspect_exact_names_and_intersection(self):
        result = rc.inspect_roads(self.osm, ['合成道路A', '合成道路B', 'wrong'])
        self.assertEqual(result['missing_names'], ['wrong'])
        self.assertEqual(result['intersections'], [{'node_id': 2, 'names': ['合成道路A', '合成道路B']}])
        self.assertEqual(len(result['nodes']), 6)

    def test_wrong_name_and_name_way_mismatch(self):
        for road in [{'name': 'wrong'}, {'name': '合成道路B', 'way_ids': [10]}, {'way_ids': [999]}]:
            p = plan(leg('合成道路A', 1, 3))
            p['legs'][0]['road'] = road
            with self.subTest(road=road), self.assertRaises(rc.CorridorError):
                self.build(p)

    def test_way_id_selector(self):
        p = plan(leg('合成道路A', 1, 3))
        p['legs'][0]['road'] = {'way_ids': [10]}
        self.assertEqual(self.build(p)['report']['edge_way_ids'], [10, 10])

    def test_disconnected_same_name_no_bridge(self):
        with self.assertRaisesRegex(rc.CorridorError, '不连通'):
            self.build(plan(leg('合成道路A', 1, 6)))

    def test_no_cross_road_shortcut(self):
        with self.assertRaisesRegex(rc.CorridorError, '禁止吸附'):
            self.build(plan(leg('合成道路A', 1, 4)))

    def test_boundary_and_unknown_endpoint_no_snap(self):
        for p in [plan(leg('合成道路A', 1, 3), leg('合成道路B', 2, 4)), plan(leg('合成道路A', 1, 999))]:
            with self.subTest(p=p), self.assertRaises(rc.CorridorError):
                self.build(p)

    def test_missing_reference_breaks_way(self):
        self.osm.write_text(XML.replace('<nd ref="2"/><nd ref="3"/>', '<nd ref="999"/><nd ref="3"/>'), encoding='utf-8')
        with self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 3)))

    def test_coordinate_coincidence_does_not_create_junction(self):
        self.osm.write_text(XML.replace('lon="0.003"', 'lon="0.002"'), encoding='utf-8')
        with self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 6)))

    def test_access_filters(self):
        for tag in ['<tag k="bicycle" v="no"/>', '<tag k="access" v="private"/>',
                    '<tag k="vehicle" v="no"/>', '<tag k="area" v="yes"/>']:
            self.osm.write_text(XML.replace('<way id="10">', '<way id="10">' + tag), encoding='utf-8')
            with self.subTest(tag=tag), self.assertRaises(rc.CorridorError):
                self.build(plan(leg('合成道路A', 1, 3)))
        for highway in ['motorway', 'construction', 'footway', 'steps']:
            self.osm.write_text(XML.replace('v="residential"', 'v="' + highway + '"'), encoding='utf-8')
            with self.subTest(highway=highway), self.assertRaises(rc.CorridorError):
                self.build(plan(leg('合成道路A', 1, 3)))

    def test_out_of_range_nonfinite_and_duplicate_osm(self):
        variants = [XML.replace('lon="0"', 'lon="181"'), XML.replace('lat="0"', 'lat="86"'),
                    XML.replace('lon="0"', 'lon="nan"'), XML.replace('id="6"', 'id="5"'),
                    '<!DOCTYPE osm [<!ENTITY x "bad">]>' + XML, '<osm><node/></osm>', '<bad/>']
        for text in variants:
            self.osm.write_text(text, encoding='utf-8')
            with self.subTest(text=text[:60]), self.assertRaises(rc.CorridorError):
                self.build(plan(leg('合成道路A', 1, 3)))

    def test_free_coordinates_and_invalid_schema_rejected(self):
        good = plan(leg('合成道路A', 1, 3))
        cases = [None, [], {}, dict(good, coordinate_system='GCJ02'), dict(good, schema_version=True),
                 dict(good, legs=[]), dict(good, shell='x'), dict(good, source={'kind': 'model'}),
                 dict(good, source={'kind': 'model', 'evidence': 'x' * 4097})]
        for value in [True, 1.0, -1, '1', 0, None]:
            p = copy.deepcopy(good)
            p['legs'][0]['via'][0]['node_id'] = value
            cases.append(p)
        p = copy.deepcopy(good)
        p['legs'][0]['via'][0]['coordinates'] = [0., 0.]
        cases.append(p)
        for p in cases:
            with self.subTest(p=p), self.assertRaises(rc.CorridorError):
                self.build(p)

    def test_zero_length_and_budget(self):
        with self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 1)))
        with self.assertRaisesRegex(rc.CorridorError, '预算'):
            self.build(plan(leg('合成道路A', 1, 3)), max_expansions=1)
        for invalid in [True, 0, 1.5, rc.MAX_EXPANSIONS + 1]:
            with self.subTest(invalid=invalid), self.assertRaises(rc.CorridorError):
                self.build(plan(leg('合成道路A', 1, 3)), max_expansions=invalid)

    def test_input_limits_and_query_limits(self):
        with patch.object(rc, 'MAX_OSM_BYTES', 10), self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 3)))
        with patch.object(rc, 'MAX_REFS', 2), self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 3)))
        for names in [[], ['x'] * 17, [1], 'wrong']:
            with self.assertRaises(rc.CorridorError):
                rc.inspect_roads(self.osm, names)
        p = plan(*[leg('合成道路A', 1, 3) for _ in range(65)])
        with self.assertRaises(rc.CorridorError):
            self.build(p)

    def test_long_edge_rejected(self):
        self.osm.write_text(XML.replace('lon="0.002"', 'lon="1"'), encoding='utf-8')
        with self.assertRaisesRegex(rc.CorridorError, '5km'):
            self.build(plan(leg('合成道路A', 1, 3)))

    def test_export_and_no_overwrite(self):
        out = self.root / 'out'
        result = self.build(plan(leg('合成道路A', 1, 3, 1)), output_dir=out)
        self.assertEqual(set(result['artifacts']), {'geojson', 'report', 'overlay'})
        self.assertEqual((out / 'road-corridor.png').read_bytes()[:8], b'\x89PNG\r\n\x1a\n')
        before = (out / 'road-corridor.json').read_bytes()
        with self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 3)), output_dir=out)
        self.assertEqual(before, (out / 'road-corridor.json').read_bytes())

    def test_export_failure_cleanup_and_no_partial_on_route_failure(self):
        out = self.root / 'failed'
        with patch.object(rc, '_render', side_effect=OSError('权限拒绝')), self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 3)), output_dir=out)
        self.assertEqual(list(out.iterdir()), [])
        with self.assertRaises(rc.CorridorError):
            self.build(plan(leg('合成道路A', 1, 6)), output_dir=out)
        self.assertEqual(list(out.iterdir()), [])

    def test_example_and_real_cli(self):
        p = json.loads((ROOT / 'examples/road-corridor-example.json').read_text(encoding='utf-8'))
        self.assertEqual(self.build(p)['report']['path_node_ids'], [1, 2, 3, 2, 1, 2, 4])
        path = self.root / 'hypothesis.json'
        path.write_text(json.dumps(p), encoding='utf-8')
        command = [sys.executable, str(ROOT / 'src/road_corridor.py')]
        result = subprocess.run(command + ['build', '--osm', str(self.osm), '--hypothesis', str(path),
                                '--output', str(self.root / 'cli')], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertFalse(json.loads(result.stdout)['original_gps'])
        query = subprocess.run(command + ['inspect', '--osm', str(self.osm), '--name', '合成道路A'],
                               capture_output=True, text=True, timeout=20)
        self.assertEqual(query.returncode, 0, query.stderr + query.stdout)
        path.write_text('{"bad":1}', encoding='utf-8')
        fail = subprocess.run(command + ['build', '--osm', str(self.osm), '--hypothesis', str(path),
                              '--output', str(self.root / 'cli-fail')], capture_output=True, text=True, timeout=20)
        self.assertEqual(fail.returncode, 2)
        self.assertEqual(json.loads(fail.stdout)['status'], 'rejected')
        self.assertNotIn('Traceback', fail.stderr)

    @unittest.skipUnless((ROOT / 'reconstruction/blind4.osm').is_file(), '需要仓库已提供本地OSM')
    def test_historical_blind4_corridor_local_reproduction_not_blind_acceptance(self):
        # 引用blind6_corrected.py旧有走廊在本地OSM实际存在的节点，不重新发明浮点坐标。
        # 人工已知序列，不证明图片识别成功，不构成真实盲测/导航验收。
        p = plan(leg('锦绣路', 95303588, 95303743),
                 leg('芳甸路', 95303743, 1554365451),
                 leg('丁香路', 1554365451, 8163871898))
        p['source'] = {'kind': 'user', 'evidence': '历史blind6_corrected.py已知走廊的本地复现；非盲测验收'}
        result = rc.build_corridor(ROOT / 'reconstruction/blind4.osm', p, self.root / 'local-reproduction')
        report = result['report']
        self.assertFalse(report['verified'])
        self.assertGreater(len(report['path_node_ids']), 4)
        self.assertEqual(report['path_node_ids'][0], 95303588)
        self.assertEqual(report['path_node_ids'][-1], 8163871898)
        self.assertEqual([x['road']['name'] for x in report['legs']], ['锦绣路', '芳甸路', '丁香路'])
        self.assertEqual(len(report['edge_way_ids']), len(report['path_node_ids']) - 1)
        self.assertTrue(Path(result['artifacts']['overlay']).is_file())


if __name__ == '__main__':
    unittest.main()
