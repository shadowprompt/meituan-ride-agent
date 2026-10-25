"""Synthetic API JSON+image transport and persisted resume, never real task replay."""
import json
import unittest
from unittest.mock import patch, MagicMock

import ride_agent
import research_online as live
import research_plan
import test_research_online as fixture


class ResearchReviewRecoveryTests(unittest.TestCase):
    setUp = fixture.OnlineTests.setUp

    def model_run(self, decide, budget=12, consent=False):
        payloads = []
        def model(request, **kwargs):
            payloads.append(json.loads(request.data))
            action = decide(len(payloads))
            response = MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(
                {'choices':[{'message':{'content':json.dumps(action)}}]}).encode()
            return response
        with patch.dict('os.environ', {'RIDE_API_KEY':'mock-only','RIDE_MODEL':'mock-only'}), patch('ride_agent.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = model
            result = ride_agent.run(self.state,self.folder,ride_agent.APIPlanner(),budget=budget,consent_map=consent)
        return result, payloads

    def hypothesis(self):
        return {'action':'research_hypothesis','version':self.state['research_version'],
                'ordered_roads':['Fixture Road',None,'Fixture Road'],
                'evidence':'Synthetic known objects, unknown connecting leg','limitations':'Not real E2E'}

    def review(self, result='ambiguous'):
        return {'action':'review_research','version':self.state['research_version'],
                'result':result,'evidence':'Synthetic screenshot comparison unresolved','limitations':'Unknown leg retained'}

    def checkpoint(self):
        return {'action':'research_checkpoint','reason':'Compare unknown connecting leg with existing localized image and saved objects'}

    def observe(self):
        return {'action':'observe_region','region':[0,0,60,60],'hypothesis':'uncertain','reason':'Synthetic local ambiguity'}

    def prime(self):
        # Seed successful public facts without sending even a mock network call through run.
        with patch.object(live,'_get',side_effect=[json.dumps([{'osm_type':'way','osm_id':5,'display_name':'Fixture'}]).encode(),json.dumps({'elements':[self.way]+self.nodes}).encode()]):
            live.execute(self.state,self.folder,self.search,self.budget,True)
            self.state['research_seen_ids'] = [['way',5],['node',1]]
            live.execute(self.state,self.folder,{'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Synthetic returned ID'},self.budget,True)
        self.state['research_budget_used'] = {'requests':6,'searches':3}
        self.state['research_observed_version'] = self.state['research_version']
        self.state['research_hypothesis'] = self.hypothesis()
        self.state['research_presented_version'] = self.state['research_version']
        self.state['review_region'] = {'region_original_xyxy':[0,0,60,60], 'version':None,
            'research_version':'previous-object-version'}

    def test_object_update_null_hypothesis_ambiguous_without_fresh_image_recovers(self):
        self.prime()
        old = self.state['research_version']
        research_plan.ledger(self.state)['model_calls'] = 12
        action = {'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Returned member, synthetic parent lookup'}
        def decide(n):
            if n == 1: return action
            if n == 2:
                self.assertNotEqual(old,self.state['research_version'])
                self.assertNotIn('research_hypothesis',self.state)
                self.assertNotIn('research_presented_version',self.state)
                return self.hypothesis()
            if n == 3: return self.review()
            if n == 4:
                rejection = json.loads(self.state['messages'][-1]['content'].removeprefix('LOCAL_TOOL_RESULT '))
                self.assertEqual(rejection['status'],'rejected')
                self.assertEqual(rejection['missing_tools'],['observe_region'])
                self.assertNotIn('research_review',self.state)
                return self.observe()
            if n == 5: return self.review()
            if n == 6:
                self.assertEqual(self.state['research_review']['result'],'ambiguous')
                self.assertTrue(ride_agent.delivery_requirements(self.state,self.folder))
                return self.checkpoint()
            self.fail('Unexpected extra model call')
        with patch.object(live,'_get',return_value=json.dumps({'elements':[self.way]}).encode()) as get:
            state,payloads = self.model_run(decide,consent=True)
        self.assertEqual(get.call_count,1)
        self.assertEqual(len(payloads),6)
        self.assertEqual(state['research_plan']['model_calls'],18)
        self.assertEqual(state['research_budget_used'],{'requests':7,'searches':3})
        self.assertEqual(state['research_plan']['counts']['correction'],1)
        self.assertEqual(state['research_progress']['network_requests_remaining'],1)
        self.assertIn(None,state['research_hypothesis']['ordered_roads'])
        self.assertEqual(state['research_review']['version'],state['research_version'])
        self.assertEqual(state['research_presented_version'],state['research_version'])
        self.assertFalse(state.get('corridor_complete'))
        self.assertFalse(state.get('artifacts'))
        self.assertNotIn('error',state)

    def test_saved_error_new_and_legacy_resume_no_free_budget_or_map_calls(self):
        for legacy in (False, True):
            with self.subTest(legacy=legacy):
                if legacy: self.setUp()
                self.prime()
                self.state.pop('research_presented_version')
                self.state.update(status='error',error='任务处理失败；底层错误不公开。',error_stage='review_research',error_category='task_processing_failed')
                self.state['research_budget_used'] = {'requests':7,'searches':3}
                names = ['observe_region']*3 + ['search_map_evidence']*3 + ['inspect_map_object']*4 + ['research_focus']*2 + ['research_hypothesis']*2 + ['review_research']
                self.state['messages'] = [{'role':'assistant','content':json.dumps({'action':name})} for name in names]
                if not legacy: research_plan.ledger(self.state)
                ride_agent.save(self.state,self.folder)
                self.state = ride_agent.load(self.folder)
                with patch.object(live,'_get') as get:
                    state,payloads = self.model_run(lambda n: self.review() if n==1 else self.checkpoint())
                get.assert_not_called()
                self.assertEqual(len(payloads),2)
                self.assertEqual(state['research_plan']['model_calls'],17)
                self.assertEqual(state['research_budget_used']['requests'],7)
                self.assertNotIn('error_stage',state)
                self.assertEqual(state['research_recovery']['reason_code'],'observe_region_required')
                # Persisted correction cap survives subsequent resume invocations.
                ride_agent.save(state,self.folder); self.state=ride_agent.load(self.folder)
                with patch.object(live,'_get') as get:
                    state,_ = self.model_run(lambda n:self.review())
                get.assert_not_called()
                self.assertEqual(state['research_progress']['blocked_reason'],'research_prerequisite_limit')
                self.assertEqual(state['research_plan']['counts']['correction'],2)
                self.assertEqual(state['research_budget_used']['requests'],7)
                self.assertIn('observe_region_required',state['question'])

    def test_hypothesis_version_observed_and_focus_prerequisites_are_structured(self):
        for reason in ('hypothesis_required','version_required','observation_required','observe_region_required'):
            with self.subTest(reason=reason):
                self.setUp();self.prime()
                action = self.review()
                if reason=='hypothesis_required': self.state['research_hypothesis']=None
                if reason=='version_required': action['version']='obsolete-version'
                if reason=='observation_required': self.state.pop('research_observed_version')
                if reason=='observe_region_required':
                    self.state.pop('research_presented_version')
                    action={'action':'research_focus','region':[0,0,40,40],'hypotheses':[], 'next_query':'Mock','falsifier':'Mock'}
                with patch.object(live,'_get') as get:
                    state,_=self.model_run(lambda n:action if n==1 else self.checkpoint())
                get.assert_not_called()
                self.assertEqual(state['research_recovery']['reason_code'],reason)
                self.assertNotEqual(state['status'],'error')

    def test_unknown_build_and_missing_road_inspection_are_bounded_local_rejections(self):
        for unknown in (True, False):
            with self.subTest(unknown=unknown):
                self.setUp(); self.prime()
                if not unknown:
                    self.state['research_hypothesis']['ordered_roads']=['Fixture Road']
                action={'action':'build_corridor','hypothesis':{'schema_version':1,
                    'coordinate_system':'WGS84','source':{'kind':'model','evidence':'Synthetic only'},
                    'legs':[{'road':{'name':'Fixture Road'},'via':[{'node_id':1},{'node_id':2}]}]}}
                with patch.object(live,'_get') as get:
                    state,_=self.model_run(lambda n:action if n==1 else self.checkpoint())
                get.assert_not_called()
                self.assertEqual(state['research_recovery']['reason_code'],
                    'unknown_legs' if unknown else 'road_inspection_required')
                self.assertFalse(state.get('corridor_complete'))

    def test_needs_revision_checkpoint_preserves_explicit_review_not_delivery(self):
        self.prime()
        with patch.object(live,'_get') as get:
            state,_=self.model_run(lambda n: self.observe() if n==1 else
                self.review('needs_revision') if n==2 else self.checkpoint())
        get.assert_not_called()
        self.assertEqual(state['research_progress']['review']['result'],'needs_revision')
        self.assertEqual(state['research_progress']['review']['version'],state['research_version'])
        self.assertEqual(ride_agent.delivery_requirements(state,self.folder)['missing_tools'],['review_research'])
        self.assertNotIn('error',state)
        self.assertFalse(state.get('artifacts'))

    def test_unknown_exception_and_malformed_review_are_not_softened(self):
        self.prime()
        with patch.object(live,'_get') as get:
            state,_=self.model_run(lambda n:{**self.review(),'unsafe':'secret-body'})
        get.assert_not_called()
        self.assertEqual(state['status'],'error')
        self.assertNotIn('research_recovery',state)
        self.assertNotIn('secret-body',state['error'])
        with patch.object(ride_agent.route_review,'text',side_effect=ValueError('private credential remote-body')), patch.object(live,'_get') as get:
            state,_=self.model_run(lambda n:self.review())
        get.assert_not_called()
        self.assertEqual(state['status'],'error')
        self.assertNotIn('private credential',state['error'])
        self.assertNotIn('research_recovery',state)


import shutil
import sys
import uuid
import test_ride_web as web_fixture


class ResearchReviewWebResumeTests(unittest.TestCase):
    setUp = web_fixture.WebTests.setUp
    tearDown = web_fixture.WebTests.tearDown
    request = web_fixture.WebTests.request

    def test_web_resume_saved_error_and_legacy_task_recover_without_map_consent(self):
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                fixture_case=ResearchReviewRecoveryTests()
                fixture_case.setUp()
                self.addCleanup(fixture_case.doCleanups)
                fixture_case.prime()
                fixture_case.state['review_region']['research_version']='stale'
                fixture_case.state.pop('research_presented_version')
                fixture_case.state.update(status='error',error='任务处理失败；底层错误不公开。',error_category='task_processing_failed',error_stage='review_research')
                fixture_case.state['research_budget_used']={'requests':7,'searches':3}
                fixture_case.state['messages']=[{'role':'assistant','content':json.dumps({'action':name})} for name in
                    ['observe_region']*3+['inspect_map_object']*7+['research_hypothesis']*2+['research_focus']*3]
                if not legacy: research_plan.ledger(fixture_case.state)
                ride_agent.save(fixture_case.state,fixture_case.folder)
                task_id=str(uuid.uuid4());folder=self.root/task_id/'task'
                shutil.copytree(fixture_case.folder,folder)
                (folder.parent/'inputs').mkdir()
                class Planner:
                    semantic_review=True
                    calls=0
                    def decide(inner,state,target):
                        inner.calls+=1
                        if inner.calls==1:
                            return {'action':'review_research','version':state['research_version'],'result':'ambiguous',
                                    'evidence':'Mock unresolved local comparison','limitations':'Unknown leg'}
                        return {'action':'research_checkpoint','reason':'Observe disputed original region before review; unknown leg retained'}
                planner=Planner()
                def cli(args):
                    with patch.object(sys,'argv',['ride_agent.py',*args]),patch('ride_agent.ConsentedPlanner',return_value=planner):
                        ride_agent.main()
                with patch.object(self.server,'run_cli',side_effect=cli),patch.object(live,'_get') as get:
                    code,_,body=self.request('/api/tasks/'+task_id+'/resume',{'review_continue':True,'consent_upload':True})
                self.assertEqual(code,200,body)
                data=json.loads(body)
                get.assert_not_called()
                self.assertEqual(planner.calls,2)
                self.assertEqual(data['status'],'paused')
                self.assertEqual(data['research_progress']['recovery']['missing_tools'],['observe_region'])
                self.assertEqual(data['research_budget_used']['requests'],7)
                self.assertEqual(data['research_plan']['model_calls'],17)
                self.assertNotIn('error_stage',data)
                self.assertFalse(data['verified'])
