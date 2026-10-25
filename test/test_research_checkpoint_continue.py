"""主动checkpoint后仅在原调用内推进：合成模型HTTP/本地对象，不重放真实任务。"""
import json
from unittest.mock import patch
import research_plan
import research_online as live
import ride_agent
import test_research_review_recovery as recovery


class CheckpointContinueTests(recovery.ResearchReviewRecoveryTests):
    def prefix(self):
        self.prime()
        p = research_plan.ledger(self.state)
        p['model_calls'] = 11  # next response = actual-pattern twelfth model call
        p['counts'].update(observe=3, network=7, local=1)
        self.state['research_budget_used'] = {'requests':7, 'searches':3}
        self.state['research_hypothesis']['ordered_roads'] = [None]*4
        self.state['review_region']['research_version'] = self.state['research_version']

    def checkpoint_action(self):
        return {**self.checkpoint(), 'next_local_action':{
            **self.observe(), 'region':[0,0,30,30],
            'reason':'Compare a different turnback crop to the cached northeast road direction; contrary bend falsifies correspondence'}}

    def test_twelfth_call_checkpoint_local_compare_review_candidate_export(self):
        self.prefix()
        plan = {'schema_version':1,'coordinate_system':'WGS84',
                'source':{'kind':'model','evidence':'Synthetic direction matches only'},
                'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]}]}
        def decide(n):
            if n == 1: return self.checkpoint_action()
            if n == 2:
                p = self.state['research_progress']
                self.assertEqual(p['status'],'continuing_local')
                self.assertIsNone(p['blocked_reason'])
                self.assertEqual(p['next_local_action'],self.checkpoint_action()['next_local_action'])
                self.assertIn('ordered_hypothesis',p['evidence_summary'])
                return p['next_local_action']
            return {
                3:{'action':'research_focus','region':[0,0,30,30],
                   'hypotheses':[{'claim':'Fixture road northeast segment matches synthetic bend',
                    'status':'supported','evidence':'Mock pixels and cached node 1->2 northeast'}],
                   'next_query':'Local ordered segment comparison only',
                   'falsifier':'Opposite local bend would reject this match'},
                4:{**self.hypothesis(),'ordered_roads':['Fixture Road']},
                5:{'action':'inspect_roads','names':['Fixture Road']},
                6:{'action':'build_corridor','hypothesis':plan},
                7:{**self.observe(),'region':[30,30,60,60]},
                8:self.review('consistent'),
                9:{'action':'deliver','summary':'Synthetic unverified candidate, non-navigable'}
            }[n]
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(decide,budget=17,consent=True)
        get.assert_not_called()
        self.assertEqual(len(payloads),9)
        self.assertEqual(state['research_plan']['model_calls'],20)
        self.assertEqual(state['research_budget_used'],{'requests':7,'searches':3})
        self.assertEqual(state['research_plan']['convergence']['checkpoints'],1)
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertFalse(state['verified'])
        self.assertTrue(any(f.endswith('.geojson') for f in state['corridor_artifacts']))
        self.assertTrue((self.folder/'research-audit.json').is_file())

    def test_same_region_proposed_again_stops_no_fake_next_action(self):
        self.prefix()
        a = {**self.checkpoint(), 'next_local_action':self.observe()}
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(lambda n:a,consent=False)
        get.assert_not_called()
        self.assertEqual(len(payloads),1)
        self.assertEqual(state['research_progress']['blocked_reason'],'no_executable_local_action')
        self.assertIsNone(state['research_progress']['next_local_action'])
        self.assertEqual(state['status'],'paused')

    def test_repeated_checkpoints_consume_original_closure_limit(self):
        self.prefix()
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(lambda n:self.checkpoint_action(),consent=True)
        get.assert_not_called()
        self.assertEqual(len(payloads),2)
        self.assertEqual(state['research_progress']['checkpoints'],2)
        self.assertEqual(state['research_progress']['no_progress_total'],2)
        self.assertEqual(state['research_progress']['blocked_reason'],'checkpoint_limit')
        self.assertEqual(state['research_plan']['counts']['closure'],2)
        self.assertFalse(state.get('artifacts'))

    def test_repeated_observations_and_hypotheses_have_finite_no_progress_stop(self):
        self.prefix()
        with patch.object(live,'_get') as get:
            state,payloads = self.model_run(lambda n:self.observe(),consent=False)
        get.assert_not_called()
        self.assertEqual(len(payloads),3)
        self.assertEqual(state['research_progress']['blocked_reason'],'no_progress_limit')
        self.assertEqual(state['research_plan']['counts']['observe'],3)
        self.assertEqual(state['research_progress']['no_progress_total'],3)
        self.assertFalse(state.get('corridor_complete'))

    def test_ambiguous_after_local_continue_never_delivers(self):
        self.prefix()
        def decide(n):
            if n == 1:return self.checkpoint_action()
            if n == 2:return self.checkpoint_action()['next_local_action']
            if n == 3:return self.review()
            return self.checkpoint()
        with patch.object(live,'_get') as get:
            state,_=self.model_run(decide,consent=False)
        get.assert_not_called()
        self.assertEqual(state['research_review']['result'],'ambiguous')
        self.assertEqual(state['research_progress']['blocked_reason'],'checkpoint_limit')
        self.assertIn(None,state['research_hypothesis']['ordered_roads'])
        self.assertFalse(state.get('artifacts'))
        self.assertEqual(state['research_budget_used'],{'requests':7,'searches':3})

    def test_checkpoint_preserves_current_consent_and_remaining_road_request(self):
        self.prefix()
        attempted={'action':'inspect_map_object','object_type':'node','object_id':1,
                   'evidence':'Mock returned road node after checkpoint'}
        def decide(n):
            if n==1:return self.checkpoint_action()
            if n==2:return attempted
            return self.checkpoint()
        with patch.object(live,'_get',return_value=json.dumps({'elements':[self.way]}).encode()) as get:
            state,_=self.model_run(decide,consent=True)
        self.assertEqual(get.call_count,1)
        self.assertNotIn('closure_local_only',str(state['messages']))
        self.assertEqual(state['research_budget_used'],{'requests':8,'searches':3})
        self.assertEqual(state['research_plan']['counts']['network'],8)
        self.assertEqual(state['research_plan']['counts']['closure'],2)
        self.assertTrue(state['research_progress']['resume_requires_fresh_upload_consent'])

    def test_rephrased_identical_ordered_hypothesis_not_progress(self):
        self.prefix()
        hypothesis={**self.hypothesis(),'ordered_roads':[None]*4}
        research_plan.record_progress(self.state,hypothesis)
        def decide(n):return {**hypothesis,'evidence':f'Same known facts reworded {n}'}
        with patch.object(live,'_get') as get:
            state,payloads=self.model_run(decide,consent=False)
        get.assert_not_called()
        self.assertEqual(len(payloads),3)
        self.assertEqual(state['research_progress']['blocked_reason'],'no_progress_limit')
        self.assertEqual(state['research_plan']['counts']['local'],1)
        self.assertFalse(state.get('artifacts'))

    def test_legacy_reason_only_twelfth_checkpoint_requests_local_falsifiable_focus(self):
        self.prefix()
        def decide(n):
            if n==1:return self.checkpoint()
            if n==2:
                p=self.state['research_progress']
                self.assertEqual(p['next_local_action']['action'],'research_focus')
                self.assertIn('falsifier',p['next_local_action']['task'])
                self.assertNotEqual(p['next_local_action']['action'],'observe_region')
                return {'action':'research_focus','region':[0,0,60,60],
                        'hypotheses':[{'claim':'Synthetic northeast road vs transverse screenshot segment',
                        'status':'unknown','evidence':'Cached node 1->2 northeast; pixels ambiguous'}],
                        'next_query':'Local compare turn direction, not a network query',
                        'falsifier':'Transverse bend cannot identify the northeast road'}
            return self.checkpoint()
        with patch.object(live,'_get') as get:
            state,payloads=self.model_run(decide,consent=False)
        get.assert_not_called()
        self.assertEqual(len(payloads),3)
        self.assertEqual(state['research_plan']['model_calls'],14)
        self.assertEqual(state['research_progress']['checkpoints'],2)
        self.assertEqual(state['research_progress']['blocked_reason'],'checkpoint_limit')
        self.assertEqual(state['research_plan']['counts']['focus'],1)
        self.assertEqual(state['research_budget_used'],{'requests':7,'searches':3})
