"""历史行政层级解析仅注入本地合成对象；公共OSM联网入口始终拒绝。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import admin_area
import map_fetch
from test_named_map_query import FIXTURE


def relation(ident, name, level, child=None):
    return {'type': 'relation', 'id': ident,
            'tags': {'name': name, 'boundary': 'administrative', 'type': 'boundary', 'admin_level': str(level)},
            'members': [] if child is None else [{'type': 'relation', 'ref': child, 'role': 'subarea'}]}


def witness(parent, child):
    return {'type': 'evidence', 'id': 1, 'tags': {'parent_id': str(parent), 'child_id': str(child)}}


def hierarchy(parent='上海市', parent_id=1, child_id=2):
    child = relation(child_id, '普陀区', 6)
    return {'elements': [relation(parent_id, parent, 4, child_id), child, witness(parent_id, child_id),
                         {'type': 'area', 'id': 3600000000+child_id, 'tags': child['tags']}]}


class AdminAreaTests(unittest.TestCase):
    def fetch(self, area, response, explicit=None):
        from unittest.mock import Mock
        transport = Mock(return_value=json.dumps(response).encode())
        levels = admin_area.levels_for(area, explicit, map_fetch.MapFetchError)
        ident, raw, resolution = admin_area.resolve(levels, transport, 1,
            map_fetch.MapFetchError, map_fetch._identifier, map_fetch._reject_constant)
        return {'area_id': ident, 'area_resolution': resolution, 'verified': False}, transport.call_args_list

    def test_offline_hierarchy_constrains_distinct_parent_ids(self):
        for parent, pid, cid in [('上海市', 1, 2), ('舟山市', 3, 4)]:
            with self.subTest(parent=parent):
                result, calls = self.fetch(parent+'普陀区', hierarchy(parent, pid, cid))
                self.assertEqual(len(calls), 1)
                q = calls[0].args[0]
                self.assertIn('if(p0.count(relations) == 1)', q)
                self.assertNotIn('out body', q)
                self.assertIn('rel(r.p0:"subarea")["name"=', q)
                self.assertIn('make evidence parent_id=p0.min(id()),child_id=p1.min(id())', q)
                self.assertIn('rel(r.p0:"subarea")', q)
                self.assertIn('rel.members1(area.a0)', q)
                self.assertIn('["name"="'+parent+'"]', q)
                self.assertNotIn('area["name"="'+parent+'普陀区"]', q)
                self.assertEqual(result['area_id'], 3600000000+cid)
                self.assertEqual(calls[0].kwargs['deadline'], 1)
                self.assertEqual(result['area_resolution']['query_levels'], [parent, '普陀区'])
                self.assertFalse(result['verified'])

    def test_explicit_hierarchy_and_three_levels(self):
        response = hierarchy()
        a, _ = self.fetch('上海市普陀区', response, ['上海市', '普陀区'])
        self.assertEqual(a['area_id'], 3600000002)
        self.assertEqual(admin_area.levels_for('某自治区某市市中区', ['某自治区', '某市', '市中区'], map_fetch.MapFetchError), ['某自治区', '某市', '市中区'])
        child = relation(3, '某县', 6)
        response = {'elements': [relation(1, '某省', 4, 2), relation(2, '某市', 5, 3), child, witness(1, 2), witness(2, 3),
                                 {'type': 'area', 'id': 3600000003, 'tags': child['tags']}]}
        result, calls = self.fetch('某省某市某县', response)
        self.assertIn('if(p1.count(relations) == 1)', calls[0].args[0])
        self.assertEqual(result['area_id'], 3600000003)

    def test_parent_unknown_ambiguous_or_missing_never_road_query(self):
        cases = [({'elements': []}, 'area_not_found'),
                 ({'elements': [relation(1, '上海市', 4), relation(9, '上海市', 4)]}, 'area_ambiguous'),
                 ({'elements': [relation(1, '上海市', 4)]}, 'area_membership_unverified')]
        for response, code in cases:
            with self.subTest(code=code), self.assertRaises(map_fetch.MapFetchError) as caught:
                self.fetch('上海市普陀区', response)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.diagnostic()['area_resolution']['query_levels'], ['上海市', '普陀区'])
        response = {'elements': [{'type': 'area', 'id': 3600000000+i, 'tags': {'name': '普陀区', 'boundary': 'administrative'}} for i in (2, 4)]}
        with self.assertRaises(map_fetch.MapFetchError) as caught:
            self.fetch('普陀区', response)
        self.assertEqual(caught.exception.code, 'area_not_found')
        self.assertEqual(caught.exception.area_resolution['query_levels'], ['普陀区'])

    def test_foreign_child_missing_membership_or_derived_area_rejected(self):
        base = hierarchy()
        cases = []
        bad = copy.deepcopy(base); bad['elements'].pop(2); cases.append(bad)
        bad = copy.deepcopy(base); bad['elements'][1]['tags']['admin_level'] = '4'; cases.append(bad)
        bad = copy.deepcopy(base); bad['elements'][3]['id'] = 3600000004; cases.append(bad)
        bad = copy.deepcopy(base); bad['elements'].insert(1, relation(4, '普陀区', 6)); cases.append(bad)
        bad = copy.deepcopy(base); bad['elements'][2]['tags']['parent_id'] = '999'; cases.append(bad)
        bad = copy.deepcopy(base); bad['elements'].insert(2, witness(1, 2)); cases.append(bad)
        bad = copy.deepcopy(base); bad['elements'][2]['tags']['child_id'] = 'SECRET'; cases.append(bad)
        for response in cases:
            with self.subTest(response=response), self.assertRaises(map_fetch.MapFetchError):
                self.fetch('上海市普陀区', response)

    def test_malicious_names_invalid_hierarchy_no_network_no_consent(self):
        with tempfile.TemporaryDirectory() as d, patch('map_fetch._download') as transport:
            for area, explicit in [('上海市"];way(1);', None), ('上海市\n普陀区', None), ('上海市普陀区', ['舟山市', '普陀区']),
                                   ('上海市普陀区', ['上海市', '普陀区', '坏区']), ('上海市普陀区', {'parent': '上海市'}),
                                   ('上海普陀', None)]:
                with self.subTest(area=area), self.assertRaises(map_fetch.MapFetchError):
                    map_fetch.fetch_named_roads(area, ['合成路'], Path(d)/'map.osm', consent_download=True, area_hierarchy=explicit)
            with self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch.fetch_named_roads('上海市普陀区', ['合成路'], Path(d)/'map.osm')
            self.assertEqual(caught.exception.code, 'osm_network_disabled')
            transport.assert_not_called()

    def test_failure_keeps_safe_candidates_without_remote_body(self):
        response = hierarchy(); response['elements'].pop(2)
        response['secret'] = 'REMOTE_BODY'
        with self.assertRaises(map_fetch.MapFetchError) as caught:
            self.fetch('上海市普陀区', response)
        diagnostic = caught.exception.diagnostic()
        self.assertNotIn('REMOTE_BODY', json.dumps(diagnostic))
        self.assertEqual(diagnostic['area_resolution']['candidates'][1]['relation_ids'], [2])
        with patch('map_fetch._download') as transport:
            with tempfile.TemporaryDirectory() as d, self.assertRaises(map_fetch.MapFetchError) as caught:
                map_fetch.fetch_named_roads('上海市普陀区', ['合成路'], Path(d)/'map.osm', consent_download=True)
            transport.assert_not_called()
        self.assertEqual(caught.exception.code, 'osm_network_disabled')
