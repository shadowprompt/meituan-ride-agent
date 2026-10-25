"""真实失败形态的合成回归；不读取原图、不重放任务或真实网络。"""
import json
from unittest.mock import patch
import research_plan
import research_decision
import research_online as live
import test_research_review_recovery as recovery


class PlannerRepairTests(recovery.ResearchReviewRecoveryTests):
    def failed_prefix(self):
        self.prime()
        self.state['research_seen_ids'] += [['way', 9], ['node', 2]]
        self.state['research_budget_used'] = {'requests':4, 'searches':3}
        self.state['research_hypothesis']['ordered_roads'] = [None]*3
        self.state['research_focus'] = {'action':'research_focus', 'region':[0,0,60,60],
            'hypotheses':[{'claim':'Mock label vs cached road', 'status':'unknown',
                           'evidence':'Synthetic pixels; no registration'}],
            'next_query':'Compare other returned candidates', 'falsifier':'Opposing bend', 'preview_count':3}
        p = research_plan.ledger(self.state)
        p['model_calls'] = 9  # checkpoint之后10/28，与真实失败一致
        p['counts'].update(observe=3, focus=1, network=4, local=1)

    def test_existing_focus_reason_only_repairs_actual_uninspected_way_with_fresh_consent(self):
        self.failed_prefix()
        def decide(n):
            if n == 1: return self.checkpoint()
            if n == 2:
                progress = self.state['research_progress']
                self.assertTrue(progress['planner_repair'])
                self.assertEqual(self.state['research_plan']['model_calls'],11)
                evidence = progress['planning_evidence']
                self.assertTrue(evidence['network_allowed_this_invocation'])
                self.assertEqual(evidence['remaining']['requests'],4)
                self.assertEqual(evidence['remaining']['searches'],0)
                self.assertEqual(evidence['remaining']['model'],18)
                self.assertIn('observed_regions', evidence)
                self.assertEqual(evidence['known_geometry'][0]['id'],5)
                return next(a for a in evidence['uninspected_candidates'] if a['object_id']==9)
            return self.checkpoint()
        branch = {'type':'way','id':9,'nodes':[1,2], 'tags':{'name':'Mock candidate','highway':'residential'}}
        with patch.object(live,'_get',return_value=json.dumps({'elements':[branch]+self.nodes}).encode()) as get:
            state,payloads = self.model_run(decide,consent=True)
        self.assertEqual(get.call_count,1)
        self.assertIn('/way/9/full.json',get.call_args.args[1])
        self.assertEqual(len(payloads),3)
        self.assertEqual(state['research_budget_used'],{'requests':5,'searches':3})
        self.assertEqual(state['research_progress']['blocked_reason'],'checkpoint_limit')
        self.assertEqual(state['research_plan']['planner_repairs'],1)
        self.assertFalse(state.get('artifacts'))
        self.assertFalse(state['verified'])

    def test_existing_focus_can_choose_different_local_region_without_map_permission(self):
        self.failed_prefix()
        def decide(n):
            if n == 1: return self.checkpoint()
            if n == 2:
                self.assertFalse(self.state['research_progress']['planning_evidence']['network_allowed_this_invocation'])
                return {**self.observe(), 'region':[10,10,40,40]}
            return self.checkpoint()
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(decide,consent=False)
        get.assert_not_called()
        self.assertEqual(len(payloads),3)
        self.assertEqual(state['research_plan']['counts']['observe'],4)
        self.assertIn(None,state['research_hypothesis']['ordered_roads'])
        self.assertFalse(state.get('artifacts'))

    def test_repeated_reason_only_has_one_repair_no_new_closure_or_fake_focus(self):
        self.failed_prefix()
        old = dict(self.state['research_focus'])
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(lambda n:self.checkpoint(),consent=True)
        get.assert_not_called()
        self.assertEqual(len(payloads),2)
        self.assertEqual(state['research_plan']['counts']['closure'],2)
        self.assertEqual(state['research_plan']['planner_repairs'],1)
        self.assertEqual(state['research_focus'],old)
        self.assertEqual(state['research_progress']['blocked_reason'],'checkpoint_limit')

    def test_repair_cannot_execute_network_without_consent_or_over_request_limit(self):
        for consent, used in ((False,4),(True,8)):
            with self.subTest(consent=consent,used=used):
                self.setUp()
                self.failed_prefix()
                self.state['research_budget_used']['requests'] = used
                def decide(n):
                    if n == 1:return self.checkpoint()
                    self.assertFalse(self.state['research_progress']['planning_evidence']['network_allowed_this_invocation'])
                    return {'action':'inspect_map_object','object_type':'way','object_id':9,'evidence':'Mock unauthorized attempt'}
                with patch.object(live,'_get') as get:
                    state,_ = self.model_run(decide,consent=consent)
                get.assert_not_called()
                self.assertEqual(state['research_budget_used']['requests'],used)
                self.assertFalse(state.get('artifacts'))

    def test_region_only_search_requires_comparison_then_preserves_search_quota(self):
        self.state['research_requests'] = [{'tool':'search_map_evidence', 'outcome':'succeeded',
            'request':{'names':['Mock Road'], 'region':'Mock City'}, 'object_count':5}]
        self.state['research_last_search_preview'] = 1
        a = {'action':'search_map_evidence','names':['Mock Road'],'region':'Mock District',
             'evidence':'Mock same uncertain text with changed district'}
        archive = live.archive(self.folder)
        self.assertEqual(research_decision.gate(self.state,archive,a)['status'],'search_diversity_recheck')
        self.assertIsNone(research_decision.gate(self.state,archive,{**a,'names':['Independent Landmark']}))
        self.state['review_region'] = {'region_original_xyxy':[0,0,40,40]}
        self.state['research_focus'] = {'preview_count':2,'region':[0,0,40,40], 'falsifier':'Independent label contradicts region',
            'hypotheses':[{'claim':'Mock Road reading','status':'unknown'},
                          {'claim':'Independent Landmark visible near the bend','status':'unknown'}]}
        self.assertIsNone(research_decision.gate(self.state,archive,a))
        self.state['research_focus']['preview_count'] = 1
        self.assertEqual(research_decision.gate(self.state,archive,a)['status'],'search_diversity_recheck')

    def test_same_name_gate_gives_executable_correction_not_terminal_map_failure(self):
        self.state['research_requests'] = [{'tool':'search_map_evidence','outcome':'succeeded',
            'request':{'names':['Mock Road'],'region':'Mock City'}}]
        def decide(n):
            if n == 1:return {'action':'search_map_evidence','names':['Mock Road'],'region':'Mock District','evidence':'Mock region change'}
            if n == 2:
                self.assertIn('search_diversity_recheck',self.state['messages'][-1]['content'])
                self.assertEqual(self.state['research_budget_used']['requests'],0)
                return {'action':'search_map_evidence','names':['Independent Landmark'],'evidence':'Another visible screenshot clue, uncertain'}
            return {'action':'ask','question':'Mock local ambiguity only'}
        with patch.object(live,'_get',return_value=b'[]') as get:
            state,_ = self.model_run(decide,consent=True)
        self.assertEqual(get.call_count,1)
        self.assertEqual(state['research_budget_used'],{'requests':1,'searches':1})
        self.assertFalse(state.get('artifacts'))

    def test_reason_only_repair_mock_mainline_to_turnback_candidate_with_review_gates(self):
        self.failed_prefix()
        plan = {'schema_version':1,'coordinate_system':'WGS84',
            'source':{'kind':'model','evidence':'Synthetic outbound and reverse order only'},
            'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]},
                    {'road':{'name':'Fixture Road'},'via':[{'node_id':2},{'node_id':1}]}]}
        def decide(n):
            if n==1:return self.checkpoint()
            if n==2:
                self.assertTrue(self.state['research_progress']['planner_repair'])
                self.assertIn(None,self.state['research_hypothesis']['ordered_roads'])
                return {**self.observe(),'region':[10,10,40,40]}
            return {
                3:{**self.hypothesis(),'ordered_roads':['Fixture Road','Fixture Road']},
                4:{'action':'inspect_roads','names':['Fixture Road']},
                5:{'action':'build_corridor','hypothesis':plan},
                6:{**self.observe(),'region':[20,20,50,50]},
                7:self.review('consistent'),
                8:{'action':'deliver','summary':'Mock turnback candidate only, unverified and non-navigable'}
            }[n]
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(decide,consent=False)
        get.assert_not_called()
        self.assertEqual(len(payloads),8)
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertEqual(state['research_review']['result'],'consistent')
        self.assertEqual(state['research_hypothesis']['ordered_roads'],['Fixture Road','Fixture Road'])
        self.assertFalse(state['verified'])
        geo = next(self.folder/f for f in state['corridor_artifacts'] if f.endswith('.geojson'))
        coords = json.loads(geo.read_text())['features'][0]['geometry']['coordinates']
        self.assertEqual(coords,[[10,20],[10.001,20.001],[10,20]])
        self.assertEqual(state['research_budget_used'],{'requests':4,'searches':3})
