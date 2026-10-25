"""只读失败证据静态replay与合成mock；从不上传原图或请求公网。"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import research_decision as decision
import research_online as live
import research_plan
import ride_agent
import test_research_review_recovery as recovery

FIXTURE = Path(__file__).parent/'fixtures/landmark-only-research-failure.json'
RECORDED = Path(__file__).resolve().parents[1]/'outputs/web/1e3439f6-bfec-403f-bc7a-246efb7ff115/task/task.json'


class RoadSeedEvidenceTests(unittest.TestCase):
    def test_static_recorded_failure_types_node_reject_and_honest_stop(self):
        f = json.loads(FIXTURE.read_text())
        self.assertEqual(f['model_calls'],13)
        self.assertEqual(f['budget'],{'requests':4,'searches':3})
        self.assertEqual(f['node_reject']['status'],'non_road_boundary')
        self.assertEqual(f['way']['tags']['leisure'],'park')
        self.assertNotIn('highway',f['way']['tags'])
        self.assertIn(563692335,f['way']['nodes'])
        state = {'messages':[], 'research_online':True,'research_budget_used':f['budget'],
                 'research_location':f['location'],'research_requests':f['requests'],
                 'research_discoveries':copy.deepcopy(f['discoveries'])}
        types = {(i['osm_type'],i['osm_id']):live.search_candidate_type(i) for i in f['search_returns']}
        for d in state['research_discoveries']: d.update(types[(d['type'],d['id'])])
        archive = {'objects':{('way',f['way']['id']):{'object':f['way']}}}
        with patch.object(live,'_get') as get:
            plan = decision.planning_evidence(state,archive,True)
            self.assertTrue(plan['road_seed_plan']['stop_without_road_seed'])
            self.assertEqual(plan['road_seed_plan']['risk'],'search_exhausted_without_road_seed')
            self.assertEqual(plan['uninspected_candidates'],[])
            self.assertEqual(decision.gate(state,archive,{'action':'inspect_map_object','object_type':'node',
                'object_id':563692335,'evidence':'Recorded park boundary'})['status'],'non_road_boundary')
            self.assertEqual(plan['localization']['status'],'stale')
            self.assertEqual(plan['localization']['recorded_status'],'unlocated')
            get.assert_not_called()

    @unittest.skipUnless(RECORDED.exists(),'Optional real local record not shipped')
    def test_real_task_bytes_remain_read_only_and_match_static_evidence(self):
        before = RECORDED.read_bytes()
        f = json.loads(FIXTURE.read_text())
        s = json.loads(before)
        self.assertEqual(s['research_requests'],f['requests'])
        self.assertEqual(s['research_location'],f['location'])
        self.assertEqual(s['research_plan']['model_calls'],13)
        self.assertTrue(s['research_plan']['checkpoint_local_only'])
        self.assertEqual(RECORDED.read_bytes(),before)

    def test_classification_uses_returned_tags_never_road_like_names(self):
        for item, expected in (({'category':'leisure','type':'park','display_name':'Mock Road'},'non_road'),
              ({'class':'highway','type':'residential','display_name':'Mock Park'},'road_candidate'),
              ({'display_name':'Mock Road'},'unknown'),
              ({'extratags':{'highway':'pedestrian','area':'yes'}},'non_road'),
              ({'extratags':{'highway':'cycleway'}},'road_candidate')):
            self.assertEqual(live.search_candidate_type(item)['candidate_kind'],expected)


class RoadSeedWorkflowTests(recovery.ResearchReviewRecoveryTests):
    def note(self):
        return {'action':'research_location','anchors':[
            {'label':'Mock Mall','kind':'landmark','image_evidence':'Synthetic endpoint label'},
            {'label':'Mock Park','kind':'landmark','image_evidence':'Synthetic independent label'},
            {'label':'Mock Station','kind':'landmark','image_evidence':'Third mock landmark'},
            {'label':'Fixture Road','kind':'road','image_evidence':'Synthetic road label near turnback'}],
            'query_plan':['Mock Mall','Mock Park','Mock Station'],
            'map_comparison':'Not yet queried','image_comparison':'Synthetic relationships, unregistered',
            'status':'unlocated','next_step':'Choose independent evidence and a relevant road'}

    def test_once_only_dynamic_feedback_no_fixed_quota_and_nonroad_full_boundary(self):
        self.state['research_location'] = self.note()
        self.state['research_budget_used'] = {'requests':2,'searches':2}
        self.state['research_requests'] = [{'tool':'search_map_evidence','outcome':'succeeded',
            'request':{'names':[n]}} for n in ('Mock Mall','Mock Park')]
        archive = live.archive(self.folder)
        a = {'action':'search_map_evidence','names':['Mock Station'],'evidence':'Third visible landmark'}
        self.assertEqual(decision.gate(self.state,archive,a)['status'],'road_seed_planning')
        self.assertIsNone(decision.gate(self.state,archive,a)) # one feedback, not repeated focus gates
        self.state['research_plan'].pop('road_planning_feedback')
        self.state['research_location']['query_plan'] = ['Mock Mall','Fixture Road']
        self.assertIsNone(decision.gate(self.state,archive,a)) # explicit road plan: not fixed 2+1
        road = {**a,'names':['Fixture Road']}
        self.assertIsNone(decision.gate(self.state,archive,road))
        self.state['research_discoveries'] = [{'type':'way','id':5,'candidate_kind':'non_road'}]
        inspect = {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Re-exclude park'}
        self.assertEqual(decision.gate(self.state,archive,inspect)['status'],'non_road_candidate')
        self.state['research_discoveries'][0]['candidate_kind'] = 'unknown'
        self.assertIsNone(decision.gate(self.state,archive,inspect))
        node = {**inspect,'object_type':'node','object_id':123}
        self.assertEqual(decision.gate(self.state,archive,node)['status'],'non_road_boundary')

    def test_mock_landmarks_then_road_full_final_image_review_export(self):
        plan = {'schema_version':1,'coordinate_system':'WGS84',
            'source':{'kind':'model','evidence':'Synthetic outbound and repeated reverse'},
            'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]},
                    {'road':{'name':'Fixture Road'},'via':[{'node_id':2},{'node_id':1}]}]}
        def decide(n):
            if n == 1:return self.note()
            if n == 2:return {**self.search,'names':['Mock Mall']}
            if n == 3:
                self.assertTrue(decision.location_snapshot(self.state)['stale'])
                return {**self.search,'names':['Mock Park']}
            if n == 4:return {**self.search,'names':['Mock Station']}
            if n == 5:
                self.assertIn('road_seed_planning',self.state['messages'][-1]['content'])
                self.assertEqual(self.state['research_budget_used']['searches'],2)
                return {**self.search,'names':['Fixture Road']}
            if n == 6:
                p = decision.planning_evidence(self.state,live.archive(self.folder),True)
                self.assertEqual(p['road_seed_plan']['road_candidates'][0]['id'],5)
                return self.checkpoint() # no focus ritual required; network seed is executable
            if n == 7:
                self.assertTrue(self.state['research_progress']['planner_repair'])
                return {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Relevant returned road seed'}
            return {8:self.observe(),9:{**self.hypothesis(),'ordered_roads':['Fixture Road','Fixture Road']},
                    10:{'action':'inspect_roads','names':['Fixture Road']},
                    11:{'action':'build_corridor','hypothesis':plan},
                    12:{**self.observe(),'region':[20,20,50,50]},13:self.review('consistent'),
                    14:{'action':'deliver','summary':'Synthetic unverified repeated-road candidate only'}}[n]
        def hit(ident,category,kind):
            return json.dumps([{'osm_type':'way','osm_id':ident,'category':category,'type':kind,
                               'display_name':'Mock return','boundingbox':['20','20.001','10','10.001']}]).encode()
        with patch.object(live,'_get',side_effect=[hit(6,'landuse','commercial'),hit(7,'leisure','park'),
             hit(5,'highway','residential'),json.dumps({'elements':[self.way]+self.nodes}).encode()]) as get:
            state,payloads = self.model_run(decide,budget=18,consent=True)
        self.assertEqual(len(payloads),14)
        self.assertEqual(get.call_count,4)
        self.assertEqual(state['research_budget_used'],{'requests':4,'searches':3})
        self.assertEqual(state['research_plan']['counts']['correction'],0)
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertFalse(state['verified'])
        self.assertEqual(state['research_hypothesis']['ordered_roads'],['Fixture Road','Fixture Road'])
        geo = next(self.folder/p for p in state['corridor_artifacts'] if p.endswith('.geojson'))
        self.assertEqual(json.loads(geo.read_text())['features'][0]['geometry']['coordinates'],
                         [[10,20],[10.001,20.001],[10,20]])

    def test_no_search_no_road_seed_stops_before_another_image_or_checkpoint(self):
        self.state['research_budget_used'] = {'requests':4,'searches':3}
        self.state['research_discoveries'] = [{'type':'way','id':7,'candidate_kind':'non_road','bbox_wgs84':[10,20,10.001,20.001]}]
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(lambda n:self.observe(),consent=True)
        self.assertEqual(payloads,[])
        self.assertEqual(state['research_progress']['blocked_reason'],'search_exhausted_without_road_seed')
        self.assertEqual(state['status'],'paused')
        get.assert_not_called()

    def test_location_note_versions_preserve_claim_without_promoting_success(self):
        self.state['research_location'] = {**self.note(), 'verified':False,
            'evidence_basis':[self.state['research_version'],0]}
        fresh = decision.location_snapshot(self.state)
        self.assertFalse(fresh['stale'])
        self.assertEqual(fresh['status'],'unlocated')
        self.state['research_requests'] = [{'outcome':'succeeded','request':{'names':['Mock Mall']}}]
        snapshot = decision.location_snapshot(self.state)
        self.assertTrue(snapshot['stale'])
        self.assertEqual(snapshot['status'],'stale')
        self.assertEqual(snapshot['recorded_status'],'unlocated')
        self.assertEqual(self.state['research_location']['map_comparison'],'Not yet queried')
        self.assertFalse(snapshot['verified'])

    def test_one_landmark_can_transition_directly_to_road_seed(self):
        self.state['research_location'] = self.note()
        self.state['research_requests'] = [{'tool':'search_map_evidence','outcome':'succeeded',
            'request':{'names':['Mock Mall']}}]
        self.state['research_budget_used'] = {'requests':1,'searches':1}
        archive = live.archive(self.folder)
        p = decision.planning_evidence(self.state,archive,True)
        action = p['road_seed_plan']['road_search_options'][0]
        self.assertEqual(action['names'],['Fixture Road'])
        self.assertIsNone(decision.gate(self.state,archive,action))
        self.assertNotIn('road_planning_feedback',self.state['research_plan'])

    def test_checkpoint_road_permission_still_requires_fresh_consent(self):
        self.prime()
        self.state['research_seen_ids'].append(['way',9])
        def decide(n):
            if n==1:return {**self.checkpoint(),'next_local_action':{**self.observe(),'region':[1,1,30,30]}}
            return {'action':'inspect_map_object','object_type':'way','object_id':9,'evidence':'Unauthorized returned ID'}
        with patch.object(live,'_get') as get:
            state,_ = self.model_run(decide,consent=False)
        get.assert_not_called()
        self.assertIn('consent_required',str(state['messages']))
        self.assertEqual(state['research_budget_used']['requests'],6)
