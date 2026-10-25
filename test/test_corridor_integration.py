"""Agent/真实CLI/Web离线链路；全部坐标/截图为明确合成夹具，非真实图成功证明。"""
import base64
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import ride_agent as agent
from ride_web import RideServer, artifact_current, allowed_artifact
from test_road_corridor import XML, plan, leg


class CorridorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root/'synthetic.png'
        Image.new('RGB', (80,60), 'white').save(self.image)
        self.osm = self.root/'synthetic.osm'
        self.osm.write_text(XML)
        self.hypothesis = plan(leg('合成道路A',1,2), leg('合成道路B',2,4))

    def task(self):
        folder = self.root/'task'
        state = agent.start(self.image, folder, True, 'corridor')
        (folder/'map.osm').write_text(XML)
        return state, folder

    def cli(self, *args):
        result = subprocess.run([sys.executable, 'src/ride_agent.py', *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_real_cli_local_only_never_calls_model_or_registration(self):
        folder = self.root/'cli'
        self.cli('start','--demo','--image',str(self.image),'--task-dir',str(folder),'--goal','corridor')
        queried = self.cli('resume','--task-dir',str(folder),'--local-only','--osm-file',str(self.osm),
                          '--road-names','合成道路A','合成道路B')
        self.assertEqual(queried['status'], 'waiting_for_user')
        self.assertTrue((folder/'corridor-inspection.json').is_file())
        hypothesis = self.root/'plan.json'
        hypothesis.write_text(json.dumps(self.hypothesis))
        done = self.cli('resume','--task-dir',str(folder),'--local-only','--road-hypothesis-file',str(hypothesis))
        self.assertEqual(done['status'], 'needs_confirmation')
        state = agent.load(folder)
        self.assertFalse(state.get('registered',False))
        self.assertFalse(state.get('extracted',False))
        self.assertEqual(len(done['artifacts']),4)
        old = state['corridor_artifacts'][0]
        self.assertTrue(artifact_current(state,old))
        changed = self.cli('resume','--task-dir',str(folder),'--local-only','--osm-file',str(self.osm))
        self.assertEqual(changed['status'],'waiting_for_user')
        self.assertFalse(artifact_current(agent.load(folder),old))
        # 换图后紧接非法假设也必须先失效，而非留下旧产物为当前。
        hypothesis.write_text(json.dumps(self.hypothesis))
        self.cli('resume','--task-dir',str(folder),'--local-only','--road-names','合成道路A','合成道路B')
        self.cli('resume','--task-dir',str(folder),'--local-only','--road-hypothesis-file',str(hypothesis))
        hypothesis.write_text('{}')
        failed = subprocess.run([sys.executable,'src/ride_agent.py','resume','--task-dir',str(folder),
                                 '--local-only','--osm-file',str(self.osm),'--road-hypothesis-file',str(hypothesis)],capture_output=True)
        self.assertEqual(failed.returncode,2)
        self.assertFalse(agent.load(folder)['corridor_complete'])

    def test_mock_model_inspect_build_delivery_no_pixel_gate(self):
        state, folder = self.task()
        class Model:
            semantic_review = True
            def decide(inner, *args):
                if not state.get('corridor_inspection'):
                    return {'action':'inspect_roads','names':['合成道路A','合成道路B']}
                return {'action':'build_corridor','hypothesis':self.hypothesis}
        with patch('urllib.request.urlopen',side_effect=AssertionError('no network')):
            done = agent.run(state,folder,Model())
        self.assertEqual(done['status'],'needs_confirmation')
        report = json.loads((folder/next(n for n in done['artifacts'] if n.endswith('/road-corridor.json'))).read_text())
        self.assertEqual(report['path_node_ids'],[1,2,4])
        self.assertFalse(report['verified'])
        self.assertFalse(done.get('registered',False))
        agent.invalidate_extraction(done)
        self.assertFalse(done['corridor_complete'])
        self.assertNotIn('corridor_inspection',done)

    def test_model_missing_inspection_user_impersonation_and_paths_rejected(self):
        for kind, extra in [('model',{}),('user',{}),('model',{'osm_path':'/etc/passwd'})]:
            with self.subTest(kind=kind,extra=extra):
                # 每次使用独立任务，避免前一次错误影响状态。
                folder = self.root/(kind+str(len(extra)))
                state = agent.start(self.image, folder, True, 'corridor')
                (folder/'map.osm').write_text(XML)
                p = json.loads(json.dumps(self.hypothesis)); p['source']['kind']=kind
                class Bad:
                    semantic_review=True
                    def decide(inner,*args):return {'action':'build_corridor','hypothesis':p,**extra}
                state = agent.run(state,folder,Bad())
                self.assertEqual(state['status'],'error')
                self.assertFalse(state.get('corridor_complete',False))

    def test_web_real_cli_upload_inspect_continue_and_stale_artifacts(self):
        server = RideServer(0, output_root=self.root/'web', cli_timeout=20)
        self.addCleanup(server.server_close)
        image = base64.b64encode(self.image.read_bytes()).decode()
        created = server.create({'mode':'demo','goal':'corridor','image_base64':image})
        task = created['task_id']
        inspected = server.resume(task, {'local_only':True,'osm_xml':XML,'road_names':['合成道路A','合成道路B']})
        self.assertTrue(any(a['name']=='corridor-inspection.json' and a['current'] for a in inspected['artifacts']))
        done = server.resume(task, {'local_only':True,'road_hypothesis_json':json.dumps(self.hypothesis)})
        self.assertEqual(done['status'],'needs_confirmation')
        corridors = [a for a in done['artifacts'] if a['name'].startswith('corridors/')]
        self.assertEqual(len(corridors),3)
        self.assertTrue(all(a['current'] and allowed_artifact(a['name']) for a in corridors))
        stale = server.resume(task, {'local_only':True,'osm_xml':XML})
        self.assertTrue(all(not a['current'] for a in stale['artifacts'] if a['name'].startswith('corridors/')))
        self.assertFalse(allowed_artifact('corridors/../../passwd'))

    def test_actual_api_payload_mock_inspection_then_build(self):
        state, folder = self.task()
        state['mode'] = 'api'
        actions = iter([{'action':'inspect_roads','names':['合成道路A','合成道路B']},
                        {'action':'build_corridor','hypothesis':self.hypothesis}])
        payloads = []
        class Response:
            def __enter__(inner):return inner
            def __exit__(inner,*args):pass
            def read(inner, *args):
                return json.dumps({'choices':[{'message':{'content':json.dumps(next(actions))}}]}).encode()
        def open_request(request, **kwargs):
            payloads.append(json.loads(request.data))
            return Response()
        with patch.dict(os.environ, {'RIDE_API_BASE':'https://mock.invalid/v1','RIDE_API_KEY':'mock','RIDE_MODEL':'mock'}):
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = open_request
                done = agent.run(state,folder,agent.ConsentedPlanner())
        self.assertEqual(done['status'],'needs_confirmation')
        self.assertEqual(len(payloads),2)
        current = payloads[1]['messages'][-1]['content'][0]['text']
        self.assertIn('corridor_inspection',current)
        self.assertIn('node_id',current)
        self.assertIn('WGS84',current)
        self.assertFalse(done.get('registered',False))
        self.assertFalse(done['verified'])

    def test_missing_osm_or_missing_names_never_generates_coordinates(self):
        folder = self.root/'missing'
        state = agent.start(self.image,folder,True,'corridor')
        done = agent.run(state,folder,agent.LocalPlanner())
        self.assertEqual(done['status'],'waiting_for_user')
        self.assertNotIn('corridor_artifacts',done)
        (folder/'map.osm').write_text(XML)
        done['corridor_names'] = ['不存在的合成道路']
        done = agent.run(done,folder,agent.LocalPlanner())
        self.assertEqual(done['status'],'waiting_for_user')
        self.assertEqual(done['corridor_inspection']['missing_names'],['不存在的合成道路'])
        self.assertNotIn('corridor_artifacts',done)

    def test_strict_goal_cannot_bypass_gates_with_corridor(self):
        state, folder = self.task()
        state['goal']='registration'
        self.assertIn('register',agent.delivery_requirements(state,folder)['missing_tools'])
        class Bad:
            def decide(inner,*args):return {'action':'inspect_roads','names':['合成道路A']}
        self.assertEqual(agent.run(state,folder,Bad())['status'],'error')
