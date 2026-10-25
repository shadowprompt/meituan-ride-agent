"""Offline recorded evidence analysis + synthetic transport, NEVER live replay/upload."""
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import research_decision as decision
import research_online as live
import research_plan
import ride_agent
import test_research_review_recovery as recovery

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ROOT/'outputs/web/0bd74825-ca5f-4434-a118-fb1a3a2672ab/task/task.json'


class RecordedFailureTests(unittest.TestCase):
    def test_fixed_public_response_replay_exposes_candidate_spread_and_missing_anchor(self):
        record=json.loads((ROOT/'test/fixtures/hospital-research-failure.json').read_text())
        pairs=decision.discovery_comparisons(record['discoveries'])
        self.assertEqual(record['searched_names'],['水城路','中环路'])
        self.assertEqual(record['budget'],{'requests':8,'searches':2})
        self.assertGreater(max(p['bbox_center_distance_km'] for p in pairs),10)
        self.assertLess(min(p['bbox_center_distance_km'] for p in pairs),3)
        self.assertTrue(all(p['screenshot_match']=='unknown' for p in pairs))
        # Pure evidence presentation: the response contains both nearby and remote arterial
        # hits; none proves a match. The unqueried hospital must come from image reading,
        # not from invented road lookup/GPS or a hardcoded default anchor.
        state={'messages':[],'research_online':True,'research_discoveries':record['discoveries'],
               'research_requests':record['requests'],'research_budget_used':record['budget']}
        facts=decision.planning_evidence(state,{'objects':{}},False)
        self.assertEqual(facts['localization']['status'],'unlocated')
        self.assertEqual(facts['remaining']['searches'],1)
        self.assertEqual(facts['remaining']['requests'],0)
        self.assertIn('independent landmark',facts['localization']['priority'])
        self.assertFalse(facts['network_allowed_this_invocation'])

    @unittest.skipUnless(RECORDED.exists(), 'Local real record is optional; not shipped as fixture')
    def test_recorded_hospital_not_previous_park_no_network_or_task_changes(self):
        before = RECORDED.read_bytes()
        with patch.object(live, '_get') as get:
            state = json.loads(before)
            actions = [json.loads(m['content']) for m in state['messages'] if m['role']=='assistant']
            searches = [a['names'][0] for a in actions if a['action']=='search_map_evidence']
            self.assertEqual(searches, ['水城路','中环路'])
            self.assertTrue(any('同仁医院' in json.dumps(a,ensure_ascii=False) for a in actions))
            self.assertNotIn('同仁医院',searches)
            self.assertEqual(state['research_budget_used'], {'requests':8,'searches':2})
            self.assertEqual(sum(a['action']=='observe_region' for a in actions),6)
            self.assertEqual(sum(r['tool']=='inspect_map_object' for r in state['research_requests']),6)
            self.assertEqual(state['research_hypothesis']['ordered_roads'],[None])
            # Do not migrate/reset/resume the exhausted recorded task even in a replay test.
            self.assertFalse(state['research_progress']['can_resume'])
            get.assert_not_called()
        self.assertEqual(RECORDED.read_bytes(),before)


class LocalizationTests(recovery.ResearchReviewRecoveryTests):
    def note(self,status='unlocated'):
        return {'action':'research_location', 'anchors':[
            {'label':'Mock Hospital','kind':'landmark','image_evidence':'Synthetic hospital south of lateral route; not the park/turnback screenshot'},
            {'label':'Mock Road','kind':'road','image_evidence':'Synthetic independent north-south road east of route'}],
            'query_plan':['Mock Hospital','Mock Road'],
            'map_comparison':'Returned distinct bounds in one local area; model candidate only' if status=='candidate' else 'Not located',
            'image_comparison':'Separate image claim: lateral start-to-end route north of hospital, west of road; no registration',
            'status':status,'next_step':'Query independent anchor first, then only route-bearing geometry'}

    def test_location_note_is_bounded_optional_and_not_a_verification_gate(self):
        note=self.note('candidate')
        self.assertFalse(decision.location_note(note)['verified'])
        for change in ({'anchors':[]},{'query_plan':['a']*4},{'status':'verified'},{'extra':'url'}):
            with self.subTest(change=change):
                with self.assertRaises(ValueError): decision.location_note({**note,**change})
        self.state['research_location']=decision.location_note(note)
        self.state['research_requests']=[{'tool':'search_map_evidence','outcome':'succeeded',
            'request':{'names':['Mock Road'],'region':'Mock City'}}]
        retry={'action':'search_map_evidence','names':['Mock Road'],'region':'Mock District',
               'evidence':'Independent documented anchor comparison supports candidate region; synthetic only'}
        self.assertIsNone(decision.gate(self.state,live.archive(self.folder),retry))
        # Saving a plan is not permission or evidence of route geometry.
        self.assertIsNotNone(ride_agent.delivery_requirements(self.state,self.folder))
        self.assertFalse(self.state['verified'])

    def test_multi_anchor_plan_search_bounds_before_geometry_to_ordered_candidate(self):
        plan = {'schema_version':1,'coordinate_system':'WGS84',
            'source':{'kind':'model','evidence':'Mock independent anchors and route shape, label obscured; unverified'},
            'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]}]}
        bodies = [
            [{'osm_type':'relation','osm_id':70,'display_name':'Mock Hospital','boundingbox':['20.000','20.001','10','10.001']}],
            [{'osm_type':'way','osm_id':5,'display_name':'Fixture Road','boundingbox':['20.001','20.002','10','10.001']}],
            {'elements':[self.way]+self.nodes}]
        def decide(n):
            if n==1:
                return self.note()
            if n in (2,3):
                return {'action':'search_map_evidence','names':['Mock Hospital' if n==2 else 'Mock Road'],
                        'evidence':'Synthetic independent clue, test locality before road geometry'}
            if n==4:
                evidence=decision.planning_evidence(self.state,live.archive(self.folder),True)
                self.assertEqual(self.state['research_budget_used'],{'requests':2,'searches':2})
                pairs=evidence['discovery_comparisons']
                self.assertEqual(len(pairs),1)
                self.assertEqual(pairs[0]['queries'],['Mock Hospital','Mock Road'])
                self.assertEqual(pairs[0]['screenshot_match'],'unknown')
                self.assertLess(pairs[0]['bbox_center_distance_km'],1)
                return self.note('candidate')
            return {
                5:{'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Mock candidate route-bearing road in anchor locality, test complete geometry'},
                6:{**self.hypothesis(),'ordered_roads':['Fixture Road'],'evidence':'Mock independent landmark and road, compatible geometry; label obscured, no GPS/registration'},
                7:{'action':'inspect_roads','names':['Fixture Road']},
                8:{'action':'build_corridor','hypothesis':plan},
                9:self.observe(),
                10:self.review('consistent'),
                11:{'action':'deliver','summary':'Mock approximate candidate only; latest full original and local image reviewed, not navigation'}
            }[n]
        with patch.object(live,'_get',side_effect=[json.dumps(b).encode() for b in bodies]) as get:
            state,payloads=self.model_run(decide,consent=True)
        self.assertEqual(get.call_count,3)
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertEqual(state['research_budget_used'],{'requests':3,'searches':2})
        self.assertEqual(state['research_plan']['counts']['observe'],1)
        self.assertEqual(state['research_plan']['counts']['focus'],0)
        self.assertFalse(state['verified']);self.assertFalse(state['original_gps'])
        final=payloads[9]['messages'][-1]['content']
        self.assertGreaterEqual(sum(v['type']=='image_url' for v in final),2)
        self.assertIn('research_location',final[0]['text'])
        geo=next(self.folder/f for f in state['corridor_artifacts'] if f.endswith('.geojson'))
        self.assertEqual(json.loads(geo.read_text())['features'][0]['geometry']['coordinates'],[[10,20],[10.001,20.001]])
        audit=json.loads((self.folder/'research-audit.json').read_text())
        self.assertEqual(audit['research_location']['status'],'candidate')

    def test_wrong_anchor_far_bounds_and_ambiguity_keep_unknown_not_export(self):
        self.prime()
        self.state['research_discoveries']=[
            {'type':'way','id':5,'query':'Mock Hospital','bbox_wgs84':[10,20,10.001,20.001]},
            {'type':'way','id':9,'query':'Mock Road','bbox_wgs84':[20,30,20.001,30.001]}]
        self.state['research_budget_used'] = {'requests':6,'searches':2}
        facts=decision.planning_evidence(self.state,live.archive(self.folder),False)
        self.assertGreater(facts['discovery_comparisons'][0]['bbox_center_distance_km'],100)
        self.assertEqual(facts['localization']['status'],'unlocated')
        self.assertFalse(any(a['object_type']=='node' for a in facts['network_suggestions']))
        def decide(n):
            return {1:self.note('ambiguous'),2:self.hypothesis(),3:self.observe(),4:self.review('ambiguous'),
                    5:{'action':'deliver','summary':'Must be rejected'},6:self.checkpoint(),7:self.checkpoint()}[n]
        with patch.object(live,'_get') as get:
            state,_=self.model_run(decide)
        get.assert_not_called()
        self.assertIn(None,state['research_hypothesis']['ordered_roads'])
        self.assertFalse(state.get('artifacts'))
        self.assertNotEqual(state['status'],'needs_confirmation')

    def test_same_version_build_hypothesis_do_not_require_second_crop(self):
        self.prime()
        def decide(n):
            return {1:self.observe(),2:{**self.hypothesis(),'ordered_roads':['Fixture Road']},
                    3:{'action':'inspect_roads','names':['Fixture Road']},
                    4:{'action':'build_corridor','hypothesis':{'schema_version':1,'coordinate_system':'WGS84',
                        'source':{'kind':'model','evidence':'Mock only'},'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]}]}},
                    5:self.review('consistent'),6:{'action':'deliver','summary':'Mock only, same map version original already viewed'}}[n]
        with patch.object(live,'_get') as get:
            state,_=self.model_run(decide)
        get.assert_not_called()
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertEqual(state['research_plan']['counts']['observe'],1)

    def test_budget_wording_and_no_resume_are_honest(self):
        for requests in (7,8):
            state={'messages':[],'research_budget_used':{'requests':requests,'searches':2}}
            research_plan.ledger(state)['counts']['closure']=2
            progress=research_plan.checkpoint(state,'model_checkpoint')
            self.assertTrue(progress['no_resume']);self.assertFalse(progress['can_resume'])
            self.assertIn('请求额度已用尽' if requests==8 else '请求仍有余量',state['question'])
            self.assertIn('搜索 2/3',state['question'])
