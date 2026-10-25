"""Synthetic regression of recorded 12-action/6-request failure, never a live replay."""
import json
import unittest
from unittest.mock import patch
import ride_agent
import research_plan
import research_online as live
import research_decision
import test_research_online as fixture


class ResearchPlanTests(unittest.TestCase):
    setUp = fixture.OnlineTests.setUp

    def planner(self, actions):
        class Planner:
            semantic_review = True
            calls = 0
            def decide(inner, state, folder):
                inner.calls += 1
                return next(actions)
        return Planner()

    def historical_pattern(self):
        # Same action types as recorded task; synthetic coordinates/names only.
        names = ['observe_region','search_map_evidence','observe_region','search_map_evidence',
                 'observe_region','research_focus','search_map_evidence','search_map_evidence',
                 'inspect_map_object','observe_region','inspect_map_object','inspect_map_object']
        self.state['messages'] = [{'role':'assistant','content':json.dumps({'action':n})} for n in names]
        self.state['research_budget_used'] = {'requests':6,'searches':3}
        self.state['research_focus'] = {'region':[0,0,40,40], 'hypotheses':[
            {'claim':'Mock label and connection', 'status':'unknown','evidence':'Synthetic ambiguous pixels'}]}

    def test_twelve_actions_six_requests_can_close_locally_and_resume_has_no_free_budget(self):
        self.historical_pattern()
        planner = self.planner(iter([{'action':'research_checkpoint','reason':'Mock local connection comparison remains unknown'}]))
        with patch.object(live,'_get') as get:
            state = ride_agent.run(self.state,self.folder,planner,consent_map=False)
        get.assert_not_called()
        self.assertEqual(planner.calls,1)
        self.assertEqual(state['research_plan']['model_calls'],13)
        progress = state['research_progress']
        self.assertEqual(progress['blocked_reason'],'model_checkpoint')
        self.assertEqual(progress['network_requests_remaining'],2)
        self.assertTrue(progress['can_resume'])
        self.assertEqual(progress['hypotheses'][0]['status'],'unknown')
        self.assertEqual(progress['next_local_action']['region'],[0,0,40,40])
        self.assertFalse(state['corridor_complete'])
        self.assertFalse(state.get('artifacts'))
        self.assertNotIn('请补充',state['question'])
        # Across unlimited resume calls, total model cap is unchanged.
        state['research_plan']['model_calls'] = 28
        planner = self.planner(iter([]))
        for _ in range(3):
            ride_agent.run(state,self.folder,planner,consent_map=True)
        self.assertEqual(planner.calls,0)
        self.assertEqual(state['research_progress']['blocked_reason'],'task_model_limit')
        self.assertFalse(state['research_progress']['can_resume'])
        self.assertEqual(state['research_budget_used']['requests'],6)

    def test_cached_query_local_feedback_does_not_fall_into_terminal_invalid_input(self):
        self.state['research_requests'] = [{'tool':'search_map_evidence','outcome':'succeeded',
            'request':{'names':['Mock Road'],'region':'Mock City','q':'Mock City Mock Road'}}]
        action = {'action':'search_map_evidence','names':[' Mock   Road '], 'region':'Mock City','evidence':'Existing facts'}
        result = research_decision.gate(self.state,live.archive(self.folder),action)
        self.assertEqual(result['status'],'cached_search')
        planner = self.planner(iter([action,action,action,action]))
        with patch.object(live,'_get') as get:
            state = ride_agent.run(self.state,self.folder,planner,consent_map=True)
        get.assert_not_called()
        self.assertEqual(planner.calls,3)
        self.assertEqual(state['research_progress']['blocked_reason'],'correction_limit')
        self.assertEqual(state['research_plan']['counts']['network'],0)

    def test_mentioning_previous_query_does_not_imply_conflicting_reading(self):
        action = {'action':'search_map_evidence','names':['Mock Independent Road'],
                  'evidence':'此前搜索仅返回地点，查询另一独立道路标签。'}
        self.assertFalse(research_decision.conflict(self.state,action))

    def test_reserved_closure_refuses_network_but_accepts_local_checkpoint(self):
        self.historical_pattern()
        research_plan.ledger(self.state)['model_calls'] = 22
        planner = self.planner(iter([
            {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Mock remaining request'},
            {'action':'research_checkpoint','reason':'Use cached evidence locally'}]))
        with patch.object(live,'_get') as get:
            state = ride_agent.run(self.state,self.folder,planner,consent_map=True)
        get.assert_not_called()
        self.assertEqual(planner.calls,2)
        self.assertIn('closure_local_only',str(state['messages']))
        self.assertEqual(state['research_budget_used']['requests'],6)
        self.assertEqual(state['research_progress']['blocked_reason'],'model_checkpoint')

    def test_exhausted_observation_stage_reserves_a_local_closure_turn(self):
        plan = research_plan.ledger(self.state)
        plan['counts']['observe'] = 6
        planner = self.planner(iter([
            {'action':'observe_region','region':[0,0,40,40],'hypothesis':'uncertain','reason':'Mock excess observe'},
            {'action':'research_checkpoint','reason':'Mock retain competing label readings'}]))
        with patch.object(live,'_get') as get:
            state = ride_agent.run(self.state,self.folder,planner,consent_map=False)
        get.assert_not_called()
        self.assertEqual(planner.calls,2)
        self.assertEqual(plan['counts']['observe'],6)
        self.assertEqual(plan['closure_reason'],'observe_action_limit')
        self.assertEqual(state['research_progress']['blocked_reason'],'model_checkpoint')

    def test_model_attempt_error_is_charged_before_upload_and_not_reset(self):
        class Broken:
            semantic_review = True
            def decide(inner,state,folder):
                raise ValueError('Mock malformed model response')
        for _ in range(2):
            ride_agent.run(self.state,self.folder,Broken(),consent_map=False)
        self.assertEqual(self.state['research_plan']['model_calls'],2)
