"""Offline regressions for premature delivery; no fabricated stage evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from ride_agent import (start, run, load, PROMPT, MAX_DELIVERY_CORRECTIONS,
                        delivery_requirements)


class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = 0
        self.feedback = []

    def decide(self, state, folder):
        self.calls += 1
        self.feedback = [json.loads(m['content'].removeprefix('LOCAL_TOOL_RESULT '))
                         for m in state['messages']
                         if m['role'] == 'user' and m['content'].startswith('LOCAL_TOOL_RESULT ')]
        return next(self.actions)


DELIVER = {'action':'deliver', 'summary':'未验证候选，请人工检查。'}
EXTRACT = {'action':'extract', 'crop':[0,0,80,60], 'color':'#00BFA5'}
CENTERLINE = {'action':'centerline'}


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)/'task'
        self.source = Path(self.tmp.name)/'source.png'
        image = Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(image).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        image.save(self.source)

    def state(self, goal='pixels'):
        state = start(self.source,self.folder,True,goal)
        if goal == 'roads': state['map_provider'] = 'osm'
        return state

    def rejections(self, state):
        return [json.loads(m['content'].removeprefix('LOCAL_TOOL_RESULT '))
                for m in state['messages']
                if m['role']=='user' and m['content'].startswith('LOCAL_TOOL_RESULT ')
                and json.loads(m['content'].removeprefix('LOCAL_TOOL_RESULT ')).get('tool')=='deliver']

    def test_early_delivery_recovers_through_real_pixel_tools_without_controls(self):
        planner = ScriptedPlanner([DELIVER, EXTRACT, DELIVER, CENTERLINE, DELIVER])
        result = run(self.state(),self.folder,planner)
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertFalse(result['verified'])
        self.assertFalse(result.get('registered',False))
        self.assertFalse((self.folder/'controls.json').exists())
        self.assertEqual([r['missing_tools'] for r in self.rejections(result)],
                         [['extract','centerline'],['centerline']])
        for artifact in result['artifacts']:
            self.assertTrue((self.folder/artifact).exists())
        self.assertEqual(load(self.folder)['status'],'needs_confirmation')

    def test_repeated_delivery_is_bounded_paused_and_not_success(self):
        planner = ScriptedPlanner([DELIVER]*20)
        state = self.state()
        state.update(summary='stale',artifacts=['stale'],error='old error')
        result = run(state,self.folder,planner,budget=100)
        self.assertEqual(planner.calls,MAX_DELIVERY_CORRECTIONS)
        self.assertEqual(result['status'],'paused')
        self.assertFalse(result['extracted'])
        self.assertFalse(result['centerlined'])
        self.assertFalse(result['verified'])
        for key in ('summary','artifacts','error'):
            self.assertNotIn(key,result)
        rejections = self.rejections(load(self.folder))
        self.assertEqual(len(rejections),MAX_DELIVERY_CORRECTIONS)
        self.assertEqual(rejections[-1]['corrections_remaining'],0)
        self.assertTrue(all(r['status']=='rejected' for r in rejections))

    def test_loop_budget_includes_delivery_corrections(self):
        planner = ScriptedPlanner([DELIVER]*20)
        result = run(self.state(),self.folder,planner,budget=1)
        self.assertEqual(planner.calls,1)
        self.assertEqual(result['status'],'paused')
        self.assertNotIn('summary',result)
        # A fresh invocation can recover; retry limits are per run like tool budgets.
        result = run(load(self.folder),self.folder,
                     ScriptedPlanner([EXTRACT,CENTERLINE,DELIVER]))
        self.assertEqual(result['status'],'needs_confirmation')

    def test_registration_delivery_requests_user_controls_not_error(self):
        state = self.state('registration')
        state.update(extracted=True,centerlined=True)
        planner = ScriptedPlanner([DELIVER,{'action':'ask','question':'请提供控制点文件。'}])
        with patch('ride_agent.register') as register:
            result = run(state,self.folder,planner)
        register.assert_not_called()
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertEqual(planner.feedback[-1]['missing_tools'],['register'])
        self.assertIn('--controls-file',planner.feedback[-1]['next_step'])
        self.assertFalse(result.get('registered',False))
        self.assertNotIn('summary',result)

    def test_registration_tool_then_delivery_after_feedback(self):
        state = self.state('registration')
        state.update(extracted=True,centerlined=True,
                     extraction_config={'crop':[0,0,80,60]},controls_crop=[0,0,80,60])
        (self.folder/'controls.json').write_text('{}')
        report = dict(fit_rmse_m=0,check_rmse_m=0,
                      independent_error_gate_passed=True,warnings=[])
        with patch('ride_agent.register',return_value=report) as register:
            result = run(state,self.folder,
                         ScriptedPlanner([DELIVER,{'action':'register'},DELIVER]))
        register.assert_called_once()
        self.assertTrue(result['registered'])
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertFalse(result['verified'])

    def test_roads_missing_match_recovers_and_preserves_no_match_truth(self):
        state = self.state('roads')
        state.update(extracted=True,centerlined=True,registered=True,registration_gate_passed=True)
        (self.folder/'map.osm').write_text('<osm/>')
        report = dict(status='no_match',matched_segment_count=0,input_segment_count=2,
                      matched_segment_indices=[],missing_segment_indices=[0,1],
                      coverage_fraction=0,warnings=['unverified'])
        planner = ScriptedPlanner([DELIVER,{'action':'match'},DELIVER])
        with patch('ride_agent.match',return_value=report) as match:
            result = run(state,self.folder,planner)
        match.assert_called_once()
        self.assertEqual(self.rejections(result)[0]['missing_tools'],['match'])
        self.assertTrue(result['match_attempted'])
        self.assertEqual(result['road_diagnostics']['coverage_fraction'],0)
        self.assertFalse(result['road_diagnostics']['verified'])
        self.assertIn('no_match',result['road_notice'])
        self.assertEqual(result['status'],'needs_confirmation')

    def test_roads_missing_all_stages_lists_ordered_requirements(self):
        state = self.state('roads')
        self.assertEqual(delivery_requirements(state,self.folder)['missing_tools'],
                         ['extract','centerline','register','match'])
        state.update(extracted=True,centerlined=True,registered=True)
        rejection = delivery_requirements(state,self.folder)
        self.assertIn('independent error gate failed',rejection['next_step'])
        state['registration_gate_passed'] = True
        self.assertIn('local-only',delivery_requirements(state,self.folder)['next_step'])
        state['map_download_requested'] = True
        self.assertIn('fresh separate consents',delivery_requirements(state,self.folder)['next_step'])

    def test_pixels_delivery_does_not_require_other_goals_or_controls(self):
        state = self.state()
        state.update(extracted=True,centerlined=True)
        with patch('ride_agent.controls_current',side_effect=AssertionError('pixels needs no controls')):
            result = run(state,self.folder,ScriptedPlanner([DELIVER]))
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertNotIn('registration_notice',result)

    def test_bad_summary_still_errors_without_success(self):
        state = self.state()
        state.update(extracted=True,centerlined=True)
        result = run(state,self.folder,ScriptedPlanner([{'action':'deliver','summary':''}]))
        self.assertEqual(result['status'],'error')
        self.assertNotIn('summary',result)

    def test_prompt_allows_gray_without_forcing_ranked_candidate(self):
        self.assertIn('Gray and low-saturation lines are valid extraction targets',PROMPT)
        self.assertIn('no_signal inspection does not prove there is no route',PROMPT)
        self.assertIn('never automatically treat the highest rank as the route',PROMPT)
        self.assertIn('For goal=pixels, extract and centerline are sufficient',PROMPT)


if __name__ == '__main__':
    unittest.main()
