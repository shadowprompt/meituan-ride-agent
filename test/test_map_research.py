"""Offline protocol tests; fabricated unit objects are NOT real-image E2E evidence."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import map_research
import ride_agent


class ScriptPlanner:
    semantic_review = True
    def __init__(self, actions):
        self.actions = iter(actions)
    def decide(self, state, folder):
        return next(self.actions)


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        elements = [{'type':'node','id':1,'lon':10.,'lat':20.},
                    {'type':'node','id':2,'lon':10.001,'lat':20.001},
                    {'type':'way','id':5,'nodes':[1,2],'tags':{'name':'Unit Road','highway':'residential'}}]
        raw = json.dumps({'elements':elements}).encode()
        (self.root/'way.json').write_bytes(raw)
        (self.root/'manifest.json').write_text(json.dumps({'sources':[{
            'url':'https://api.openstreetmap.org/api/0.6/way/5/full.json',
            'sha256':hashlib.sha256(raw).hexdigest(),'saved_as':'way.json'}]}))
        self.archive = map_research.load(self.root/'manifest.json',self.root)

    def test_search_and_inspect_no_network(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('No network')):
            result = map_research.execute(self.archive,{'action':'search_map_evidence','names':['Unit Road'],'evidence':'Unit fixture'})
            self.assertEqual(result['objects'][0]['object']['id'],5)
            result = map_research.execute(self.archive,{'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Returned way member'})
            self.assertEqual(len(result['objects']),2)
            self.assertFalse(result['verified'])
            missing = map_research.execute(self.archive,{'action':'search_map_evidence','names':['Missing'],'evidence':'Uncertain'})
            self.assertEqual(missing['status'],'not_in_archive')

    def test_hash_and_path_guards(self):
        (self.root/'way.json').write_text('{}')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):
            map_research.load(self.root/'manifest.json',self.root)
        manifest = json.loads((self.root/'manifest.json').read_text())
        manifest['sources'][0]['saved_as']='../outside'
        (self.root/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            map_research.load(self.root/'manifest.json',self.root)

    def task(self):
        from PIL import Image
        Image.new('RGB',(80,80),'white').save(self.root/'unit.png')
        folder=self.root/'task'
        state=ride_agent.start(self.root/'unit.png',folder,goal='corridor')
        ride_agent.select_map_provider(state,folder,'research')
        with patch('pathlib.Path.cwd',return_value=self.root):
            ride_agent.install_research(state,folder,self.root/'manifest.json')
        return state,folder

    def test_agent_loop_order_repeat_review_and_delivery(self):
        state,folder=self.task()
        v=state['research_version']
        plan={'schema_version':1,'coordinate_system':'WGS84','source':{'kind':'model','evidence':'Unit hypothesis, not a real ride'},'legs':[
            {'road':{'name':'Unit Road'},'via':[{'node_id':1},{'node_id':2}]},
            {'road':{'name':'Unit Road'},'via':[{'node_id':2},{'node_id':1}]}]}
        actions=[{'action':'search_map_evidence','names':['Unit Road'],'evidence':'Unit visible-name hypothesis'},
                 {'action':'inspect_map_object','object_type':'way','object_id':5,'evidence':'Returned search ID'},
                 {'action':'research_hypothesis','version':v,'ordered_roads':['Unit Road','Unit Road'],'evidence':'Repeated legs','limitations':'Unverified fixture'},
                 {'action':'inspect_roads','names':['Unit Road']},
                 {'action':'build_corridor','hypothesis':plan},
                 {'action':'review_research','version':v,'result':'consistent','evidence':'Unit sequence comparison only','limitations':'No real screenshot identity validation'},
                 {'action':'deliver','summary':'Unit protocol candidate, not real E2E'}]
        with patch('urllib.request.urlopen',side_effect=AssertionError('No network')):
            result=ride_agent.run(state,folder,ScriptPlanner(actions),budget=7)
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertFalse(result['verified'])
        self.assertIn('research-audit.json',result['artifacts'])
        review=result['research_review']
        result['research_review']={**review,'result':'needs_revision'}
        self.assertEqual(ride_agent.delivery_requirements(result,folder)['missing_tools'],['review_research'])
        (folder/'map.osm').write_text('<osm/>')
        with self.assertRaisesRegex(ValueError,'changed'):
            ride_agent.research_archive(result,folder)

    def test_agent_rejects_invented_id_and_unknown_leg_delivery(self):
        state,folder=self.task()
        planner=ScriptPlanner([{'action':'inspect_map_object','object_type':'node','object_id':1,'evidence':'Not observed'}])
        result=ride_agent.run(state,folder,planner,budget=1)
        self.assertNotEqual(result['status'],'needs_confirmation')
        self.assertNotIn('research_observed_version',result)
        # Unknown segments are explicit hypothesis facts, not permission to omit them.
        state,folder = result,folder
        state['research_hypothesis']={'ordered_roads':['Unit Road',None,'Unit Road']}
        state['research_observed_version']=state['research_version']
        plan={'schema_version':1,'coordinate_system':'WGS84','source':{'kind':'model','evidence':'Unit omission attempt'},
              'legs':[{'road':{'name':'Unit Road'},'via':[{'node_id':1},{'node_id':2}]}]}
        result=ride_agent.run(state,folder,ScriptPlanner([{'action':'build_corridor','hypothesis':plan}]),budget=1)
        self.assertFalse(result.get('corridor_complete'))
        self.assertNotEqual(result['status'],'needs_confirmation')

    def test_no_controls_or_admin_prerequisite_in_research_prompt(self):
        self.assertIn('Never ask\nfor country/admin hierarchy, user road names, controls',ride_agent.PROMPT)
        state,folder=self.task()
        self.assertEqual(ride_agent.delivery_requirements(state,folder)['missing_tools'],['review_research'])
        ride_agent.validate_map_invocation('research')


if __name__=='__main__':
    unittest.main()
