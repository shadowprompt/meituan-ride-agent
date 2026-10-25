"""Saved failure evidence + actual outgoing mock model payload, never task replay/network."""
import copy
import json
from pathlib import Path
from unittest.mock import patch
import unittest
import research_decision as decision
import research_plan
import research_online as live
import ride_agent
import test_research_review_recovery as recovery

FIXTURE = json.loads((Path(__file__).parent/'fixtures/research-failure-8d470d38.json').read_text())


class GeometryContextTests(unittest.TestCase):
    def setUp(self):
        self.state = copy.deepcopy(FIXTURE['state'])
        self.state['research_online'] = True
        self.archive = {'objects':{(e['type'],e['id']):{'object':e} for e in FIXTURE['elements']}}

    def test_recorded_failure_exposes_information_gap_not_missing_deployment(self):
        messages = FIXTURE['recorded_messages']
        full = json.loads(messages[11]['content'].removeprefix('LOCAL_TOOL_RESULT '))
        self.assertEqual(len(full['objects']),7)  # six nodes + one way, NOT seven nodes
        old = full['spatial_evidence']['ways'][0]
        self.assertEqual(len(old['node_ids']),6)
        self.assertNotIn('nodes',old)  # raw objects existed, planner summary lacked node roles/positions
        self.assertTrue(any('lat' in o['object'] for o in full['objects']))
        checkpoint = json.loads(messages[19]['content'].removeprefix('LOCAL_TOOL_RESULT '))
        self.assertEqual(checkpoint['continuation_scope'],'bounded_planning_with_current_consent')
        self.assertTrue(checkpoint['planning_evidence']['network_allowed_this_invocation'])
        self.assertIn('Before node/ways explain the relevant junction',FIXTURE['baseline_system_prompt'])
        self.assertIn('A second no-information checkpoint stops within closure=2',FIXTURE['baseline_system_prompt'])
        self.assertEqual(self.state['research_plan']['model_calls'],12)
        self.assertEqual(self.state['research_budget_used'],{'requests':4,'searches':3})

    def test_all_actual_nodes_consumable_without_promoting_crossing_or_endpoint(self):
        facts = decision.planning_evidence(self.state,self.archive,True)
        way = facts['known_geometry'][0]
        self.assertEqual(way['node_count'],6)
        self.assertFalse(way['node_summary_truncated'])
        nodes = way['nodes']
        self.assertEqual(nodes[0]['wgs84'],[121.3613242,31.2359913])
        self.assertEqual(nodes[-1]['wgs84'],[121.3603489,31.2332876])
        self.assertEqual(nodes[-1]['role_in_returned_way'],'endpoint')
        self.assertLess(nodes[-1]['offset_from_way_bbox_center_m'][1],0)
        self.assertGreater(nodes[0]['offset_from_way_bbox_center_m'][1],0)
        tagged = next(n for n in nodes if n['id']==3439342477)
        self.assertEqual(tagged['tags']['highway'],'crossing')
        self.assertEqual(tagged['junction_status'],'unknown')
        self.assertEqual(tagged['parent_membership'],'unknown_not_queried')
        self.assertIsNotNone(tagged['bend_degrees'])
        # Even actual road endpoints/crossings are not default query recommendations.
        self.assertFalse(any(a['object_type']=='node' for a in facts['network_suggestions']))
        self.assertIn('falsifier',way['connection_test'])
        relation=next(r for r in way['discovery_relations'] if r['to']==['way',44354955])
        self.assertEqual(relation['map_center_direction'],'southeast')
        self.assertEqual(relation['screenshot_match'],'unknown')
        self.assertIn('Reject remote',way['connection_test']['required_rationale'])

    def test_saved_nonroad_search_receives_targeted_reading_correction(self):
        report = json.loads(FIXTURE['recorded_messages'][5]['content'].removeprefix('LOCAL_TOOL_RESULT '))
        action = json.loads(FIXTURE['recorded_messages'][4]['content'])
        self.assertEqual(report['status'],'found')
        self.assertTrue(all(o['candidate_kind']=='non_road' for o in report['objects']))
        feedback = decision.outcome(self.archive,action,report)['search_feedback']
        self.assertEqual(feedback['status'],'no_road_candidates_returned')
        self.assertEqual(feedback['road_existence'],'not_excluded')
        self.assertIn('No fuzzy-name',feedback['next_test'])
        empty = decision.outcome(self.archive,action,{'objects':[]})['search_feedback']
        self.assertEqual(empty['returned_count'],0)
        self.assertEqual(empty['road_existence'],'not_excluded')

    def test_checkpoint_migration_and_distinct_proposed_network_after_two_saves(self):
        # Previous saved counts migrated once; request/model/action budgets unchanged.
        plan = research_plan.ledger(self.state)
        self.assertEqual(plan['counts']['closure'],2)  # no message history: cannot infer migration
        self.state['messages'] = copy.deepcopy(FIXTURE['recorded_messages'])
        plan.pop('checkpoint_save_accounting')
        research_plan.ledger(self.state)
        self.assertEqual(plan['counts']['closure'],0)
        old_budget = dict(self.state['research_budget_used'])
        facts = decision.planning_evidence(self.state,self.archive,True)
        action = {'action':'inspect_map_object','object_type':'node','object_id':9231025350,
                  'evidence':'Conditional test only: southern end vs transverse image leg after independent locality check; reject if parents offer no distinct east-west geometry.'}
        progress = research_plan.checkpoint(self.state,'model_checkpoint',{'next_action':action},True,facts)
        self.assertEqual(progress['next_local_action'],action)
        self.assertTrue(progress['can_resume'])
        self.assertEqual(plan['model_calls'],12)
        self.assertEqual(plan['counts']['closure'],0)
        self.assertEqual(self.state['research_budget_used'],old_budget)
        self.assertIsNone(research_plan.network_next(self.state,action,decision.planning_evidence(self.state,self.archive,False)))
        self.assertIsNone(research_plan.network_next(self.state,{**action,'object_id':987654321},facts))

    def test_more_than_two_saves_with_different_executable_work_not_a_budget_increase(self):
        self.state['research_plan']['convergence']={'checkpoints':0,'no_progress':0,
            'no_progress_total':0,'observations':[],'decisions':[]}
        plan=research_plan.ledger(self.state)
        plan['counts']['observe']=0
        for i in range(3):
            action={'action':'observe_region','region':[10+i*40,10,40+i*40,40],
                    'hypothesis':'uncertain','reason':'Different relevant turn structure, local-only mock'}
            progress=research_plan.checkpoint(self.state,'model_checkpoint',
                {'next_local_action':action},True,decision.planning_evidence(self.state,self.archive,False))
            self.assertEqual(progress['status'],'continuing_local')
            research_plan.record_progress(self.state,action)
            plan['model_calls']+=2
            research_plan.charge(self.state,'observe')
        self.assertEqual(progress['checkpoints'],3)
        self.assertEqual(plan['model_limit'],28)
        self.assertEqual(plan['limits']['observe'],6)
        self.assertEqual(self.state['research_budget_used'],{'requests':4,'searches':3})
        self.assertFalse(progress['no_resume'])

    def test_remote_node_never_recommended_merely_because_it_is_endpoint(self):
        remote={'type':'node','id':42,'lon':120,'lat':30}
        way={'type':'way','id':43,'nodes':[42,3001826713],
             'tags':{'highway':'primary','name':'remote reference'}}
        for obj in (remote,way):self.archive['objects'][(obj['type'],obj['id'])]={'object':obj}
        evidence=decision.planning_evidence(self.state,self.archive,True)
        self.assertFalse(any(a['object_type']=='node' for a in evidence['network_suggestions']))
        remote_fact=next(w for w in evidence['known_geometry'] if w['id']==43)['nodes'][0]
        self.assertEqual(remote_fact['wgs84'],[120,30])
        self.assertIn('not_recommended_by_default',remote_fact['query_selection'])


class OutgoingContextTests(recovery.ResearchReviewRecoveryTests):
    def test_real_geometry_summary_in_actual_api_payload_without_original_upload(self):
        # Write public static OSM only to a NEW synthetic-white-image task.
        self.prime()
        with patch.object(ride_agent,'research_archive',return_value={**static_archive(), 'version':'static'}), patch.dict('os.environ',{'RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}), patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps({'choices':[{'message':{'content':json.dumps({'action':'ask','question':'Mock only'})}}]}).encode()
            planner=ride_agent.APIPlanner();planner.map_query_authorized=True
            planner.decide(self.state,self.folder)
            payload=json.loads(opener.return_value.open.call_args.args[0].data)
        current=json.loads(payload['messages'][-1]['content'][0]['text'].removeprefix('CURRENT_STAGE_STATE ').split('. Earlier tool reports')[0])
        nodes=current['research_planning_evidence']['known_geometry'][0]['nodes']
        self.assertEqual(len(nodes),6)
        self.assertEqual(nodes[0]['wgs84'],[121.3613242,31.2359913])
        self.assertIn('does NOT exclude it as a location reference',payload['messages'][0]['content'])
        self.assertIn('next_action as a complete inspect_map_object',payload['messages'][0]['content'])
        self.assertNotIn('A second no-information checkpoint stops within closure=2',payload['messages'][0]['content'])

    def test_transverse_leg_rejects_background_road_but_tests_justified_node_then_new_way(self):
        # Synthetic N-S reference and E-W candidate; no saved task replay.
        self.nodes=[{'type':'node','id':1,'lon':10,'lat':20.001},{'type':'node','id':2,'lon':10,'lat':20}]
        self.prime()
        self.state['research_seen_ids'].append(['node',2])
        self.state['research_budget_used']={'requests':4,'searches':3}
        self.state['research_hypothesis']['ordered_roads']=[None,None]
        p=research_plan.ledger(self.state);p['counts'].update(network=4,local=1);p['model_calls']=8
        branch={'type':'way','id':9,'nodes':[2,3],'tags':{'name':'Transverse candidate','highway':'residential'}}
        third={'type':'node','id':3,'lon':10.001,'lat':20}
        plan={'schema_version':1,'coordinate_system':'WGS84','source':{'kind':'model','evidence':'Mock independent locality and transverse shape, not automatic registration'},'legs':[
            {'road':{'name':'Transverse candidate'},'via':[{'node_id':2},{'node_id':3}]},
            {'road':{'name':'Transverse candidate'},'via':[{'node_id':3},{'node_id':2}]}]}
        def decide(n):
            if n==1:return self.checkpoint()
            if n==2:
                geometry=self.state['research_progress']['planning_evidence']['known_geometry'][0]
                self.assertGreater(geometry['nodes'][0]['offset_from_way_bbox_center_m'][1],0)
                self.assertLess(geometry['nodes'][1]['offset_from_way_bbox_center_m'][1],0)
                self.assertFalse(any(a['object_type']=='node' for a in self.state['research_progress']['planning_evidence']['network_suggestions']))
                return {'action':'inspect_map_object','object_type':'node','object_id':2,'evidence':'Mock independent landmarks put transverse bend south of N-S reference; test southern node for east-going road, reject north endpoint without lookup. No distinct E-W parent/full geometry falsifies this hypothesis; reference itself is NOT ridden.'}
            if n==3:
                self.assertNotIn('research_hypothesis',self.state)
                return {'action':'inspect_map_object','object_type':'way','object_id':9,'evidence':'Mock newly returned shared southern node parent; full geometry must be east-west to support transverse image leg, otherwise revise'}
            return {4:{**self.hypothesis(),'ordered_roads':['Transverse candidate','Transverse candidate']},
                    5:{'action':'inspect_roads','names':['Transverse candidate']},6:{'action':'build_corridor','hypothesis':plan},
                    7:self.observe(),8:self.review('consistent'),9:{'action':'deliver','summary':'Mock only, repeated transverse candidate unverified'}}[n]
        with patch.object(live,'_get',side_effect=[json.dumps({'elements':[self.way,branch]}).encode(),json.dumps({'elements':[branch,self.nodes[1],third]}).encode()]) as get:
            state,payloads=self.model_run(decide,consent=True)
        self.assertEqual(get.call_count,2)
        self.assertIn('/node/2/ways.json',get.call_args_list[0].args[1])
        self.assertIn('/way/9/full.json',get.call_args_list[1].args[1])
        self.assertEqual(state['status'],'needs_confirmation')
        self.assertEqual(state['research_budget_used'],{'requests':6,'searches':3})
        self.assertNotIn('Fixture Road',state['research_hypothesis']['ordered_roads'])
        self.assertFalse(state['verified'])
        self.assertEqual(len(payloads),9)


def static_archive():
    return {'objects':{(e['type'],e['id']):{'object':e} for e in FIXTURE['elements']}}
