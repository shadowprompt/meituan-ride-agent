"""真实失败决策模式的合成回归，不重放真实授权，不代表原图已识路。"""
import json
import unittest
from unittest.mock import patch

import research_decision as decision
import research_online as live
import ride_agent
import test_research_online as fixture


class DecisionTests(unittest.TestCase):
    setUp = fixture.OnlineTests.setUp
    call = fixture.OnlineTests.call
    discover = fixture.OnlineTests.discover
    # 复用仅本地白图/Mock HTTP的fixture，不读取用户截图。
    def test_conflicting_readings_require_new_local_observe_and_focus_before_search(self):
        class Planner:
            semantic_review = True
            def __init__(inner):
                inner.i = 0
            def decide(inner, state, folder):
                inner.i += 1
                actions = [
                    {'action':'search_map_evidence','names':['长风路'],'evidence':'Mock uncertain label'},
                    {'action':'search_map_evidence','names':['长宁路'],'evidence':'此前读错，Mock conflicting label'},
                    {'action':'observe_region','region':[0,0,40,40],'hypothesis':'uncertain','reason':'Mock disputed road text only'},
                    {'action':'research_focus','region':[0,0,40,40],
                     'hypotheses':[{'claim':'长风路 or 长宁路','status':'unknown','evidence':'Mock unreadable pixels, neither confirmed'}],
                     'next_query':'Search alternative to compare road bounds','falsifier':'Returned road position inconsistent with local landmark directions'},
                    {'action':'search_map_evidence','names':['长宁路'],'evidence':'此前读错，Mock conflicting label'},
                    {'action':'ask','question':'Mock only; local text remains unknown'}]
                return actions[inner.i-1]
        with patch.object(live,'_get',return_value=b'[]') as get:
            state = ride_agent.run(self.state,self.folder,Planner(),budget=6,consent_map=True)
        self.assertEqual(get.call_count,2)
        reports = [m['content'] for m in state['messages'] if m['role']=='user']
        self.assertTrue(any('needs_image_recheck' in m for m in reports))
        self.assertEqual(state['research_focus']['hypotheses'][0]['status'],'unknown')
        self.assertEqual(state['research_budget_used']['requests'],2)
        self.assertFalse(state['corridor_complete'])

    def test_parcel_nodes_not_intersections_and_returned_branch_is_actionable(self):
        self.discover()
        parcel = {**self.way,'tags':{'name':'Mock mall','landuse':'commercial'}}
        self.call({'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Mock landmark bounds'}, {'elements':[parcel]+self.nodes})
        data = live.archive(self.folder)
        blocked = decision.gate(self.state,data,{'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Mock boundary'})
        self.assertEqual(blocked['status'],'non_road_boundary')
        # 无任何几何相近即匹配之推论；商业地块不产生道路连接。
        self.assertEqual(decision.spatial(data)['ways'][0]['connections'],[])
        self.assertEqual(decision.spatial(data)['ways'][0]['kind'],'non_road_boundary')

    def test_loop_blocks_parcel_chain_and_duplicate_success_without_network(self):
        self.discover()
        parcel = {**self.way,'tags':{'landuse':'commercial'}}
        self.call({'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Mock parcel'}, {'elements':[parcel]+self.nodes})
        self.state['research_seen_ids'].append(['node',1])
        actions = iter([
            {'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Mock wrong boundary expansion'},
            {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Mock repeated full'},
            {'action':'ask','question':'Mock no road intersection; unknown route preserved'}])
        class Planner:
            semantic_review = True
            def decide(inner,state,folder):
                return next(actions)
        with patch.object(live,'_get') as get:
            state = ride_agent.run(self.state,self.folder,Planner(),budget=3,consent_map=True)
        get.assert_not_called()
        reports = ' '.join(m['content'] for m in state['messages'] if m['role']=='user')
        self.assertIn('non_road_boundary',reports)
        self.assertIn('cached_object',reports)
        self.assertFalse(state['corridor_complete'])

    def test_node_parent_branch_name_geometry_gap_and_shared_node_presented(self):
        self.discover()
        self.call({'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Mock road shape'}, {'elements':[self.way]+self.nodes})
        self.state['research_seen_ids'].append(['node',1])
        branch = {'type':'way','id':9,'nodes':[1,3],'tags':{'name':'北翟路','highway':'residential'}}
        result,_ = self.call({'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Mock shared-node turn test'}, {'elements':[self.way,branch]})
        data = live.archive(self.folder)
        facts = decision.spatial(data)
        source = next(w for w in facts['ways'] if w['id']==5)
        self.assertEqual(source['connections'],[{'way_id':9,'name':'北翟路','shared_node_ids':[1],'needs_full':True}])
        self.assertFalse(next(w for w in facts['ways'] if w['id']==9)['coordinates_complete'])
        # API planner 后续上下文真正包括此分支（不是只测摘要函数）。
        self.state['research_seen_ids'].append(['way',9])
        with patch.dict('os.environ',{'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch('ride_agent.urllib.request.build_opener') as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.read.return_value = json.dumps({'choices':[{'message':{'content':json.dumps({'action':'ask','question':'Mock'})}}]}).encode()
            ride_agent.APIPlanner().decide(self.state,self.folder)
            payload = json.loads(opener.return_value.open.call_args.args[0].data)
        current = payload['messages'][-1]['content'][0]['text']
        self.assertIn('research_spatial_evidence',current)
        self.assertIn('北翟路',json.loads(current.removeprefix('CURRENT_STAGE_STATE ').split('. Earlier tool reports')[0])['research_spatial_evidence']['ways'][1]['name'])
        outcome = decision.outcome(data,{'evidence':'Mock turn test'},result)
        self.assertEqual(outcome['supported']['returned_road_ids'],[5,9])
        self.assertIn('screenshot identity',outcome['unknown'])

    def test_discovery_bbox_is_response_bound_not_projected_or_node_geometry(self):
        result,_ = self.call(self.search,[{'osm_type':'way','osm_id':5,'display_name':'Mock', 'boundingbox':['20','21','10','11']}])
        self.assertEqual(result['discovery_bounds'][0]['bbox_wgs84'],[10,20,11,21])
        self.assertNotIn('lat',result['objects'][0]['object'])
        self.assertEqual(self.state['research_object_count'],0)
        result,_ = self.call({**self.search,'names':['Other']},[{'osm_type':'way','osm_id':6,'display_name':'Mock','boundingbox':['nan','21','10','11']}])
        self.assertEqual(result['discovery_bounds'],[])

    def test_spatial_directions_derive_only_from_real_nodes_no_screen_projection(self):
        elements = self.nodes + [self.way,
            {'type':'node','id':3,'lon':11,'lat':21},
            {'type':'node','id':4,'lon':11.1,'lat':21.1},
            {'type':'way','id':8,'nodes':[3,4],'tags':{'name':'Mock northeast','highway':'residential'}}]
        archive = {'objects':{(o['type'],o['id']):{'object':o} for o in elements}}
        facts = decision.spatial(archive)
        self.assertEqual(facts['ways'][0]['bbox_wgs84'],[10,20,10.001,20.001])
        self.assertEqual(facts['ways'][0]['end_to_end_direction'],'northeast')
        self.assertEqual(facts['relative_positions'][0]['bbox_center_direction'],'northeast')
        self.assertEqual(facts['relative_positions'][0]['screenshot_correspondence'],'unknown')
        self.assertEqual(facts['ways'][0]['connections'],[])
        self.assertNotIn('projection',facts['ways'][0])

    def test_failed_tool_outcome_never_excludes_road_or_claims_route(self):
        report = decision.outcome({'objects':{}},{'evidence':'Mock hypothesis test'},{'status':'unavailable'})
        self.assertEqual(report['excluded'],[])
        self.assertEqual(report['supported']['returned_road_ids'],[])
        self.assertIn('ordered traversal including turnbacks',report['unknown'])


if __name__ == '__main__':
    unittest.main()
