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
        with patch.dict(os.environ, {'AMAP_API_KEY':'synthetic-map-key-only'}):
            self.cli('start','--demo','--image',str(self.image),'--task-dir',str(folder),'--goal','corridor','--map-provider','amap')
        queried = self.cli('resume','--task-dir',str(folder),'--local-only','--map-provider','osm','--osm-file',str(self.osm),
                          '--road-names','合成道路A','合成道路B')
        self.assertEqual(queried['status'], 'waiting_for_user')
        self.assertTrue((folder/'corridor-inspection.json').is_file())
        hypothesis = self.root/'plan.json'
        hypothesis.write_text(json.dumps(self.hypothesis))
        done = self.cli('resume','--task-dir',str(folder),'--local-only','--map-provider','osm','--road-hypothesis-file',str(hypothesis))
        self.assertEqual(done['status'], 'needs_confirmation')
        state = agent.load(folder)
        self.assertFalse(state.get('registered',False))
        self.assertFalse(state.get('extracted',False))
        self.assertEqual(len(done['artifacts']),4)
        old = state['corridor_artifacts'][0]
        self.assertTrue(artifact_current(state,old))
        changed = self.cli('resume','--task-dir',str(folder),'--local-only','--map-provider','osm','--osm-file',str(self.osm))
        self.assertEqual(changed['status'],'waiting_for_user')
        self.assertFalse(artifact_current(agent.load(folder),old))
        # 换图后紧接非法假设也必须先失效，而非留下旧产物为当前。
        hypothesis.write_text(json.dumps(self.hypothesis))
        self.cli('resume','--task-dir',str(folder),'--local-only','--map-provider','osm','--road-names','合成道路A','合成道路B')
        self.cli('resume','--task-dir',str(folder),'--local-only','--map-provider','osm','--road-hypothesis-file',str(hypothesis))
        hypothesis.write_text('{}')
        failed = subprocess.run([sys.executable,'src/ride_agent.py','resume','--task-dir',str(folder),
                                 '--local-only','--map-provider','osm','--osm-file',str(self.osm),'--road-hypothesis-file',str(hypothesis)],capture_output=True)
        self.assertEqual(failed.returncode,2)
        self.assertFalse(agent.load(folder)['corridor_complete'])

    def test_legacy_model_corridor_refused_without_mutating_local_map(self):
        state, folder = self.task()
        state['map_provider'] = 'osm'
        class Model:
            semantic_review = True
            def decide(self, *args): raise AssertionError('旧OSM任务不得启动模型')
        with patch('amap_fetch._request') as amap, patch('map_fetch._download') as osm:
            with self.assertRaisesRegex(ValueError, '本次'):
                agent.run(state, folder, Model(), consent_map=True)
            amap.assert_not_called(); osm.assert_not_called()
        self.assertEqual((folder/'map.osm').read_text(), XML)

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

    def test_web_osm_upload_refused_before_cli_and_task_mutation(self):
        server = RideServer(0, output_root=self.root/'web', cli_timeout=20)
        self.addCleanup(server.server_close)
        with patch.dict(os.environ, {'AMAP_API_KEY':'synthetic-only'}):
            created = server.create({'mode':'demo','goal':'corridor','map_provider':'amap','image_base64':base64.b64encode(self.image.read_bytes()).decode()})
        folder = server.root/created['task_id']/'task'
        before = (folder/'task.json').read_bytes()
        from ride_web import WebError
        with patch.object(server, 'run_cli') as cli:
            with self.assertRaises(WebError):
                server.resume(created['task_id'], {'local_only':True,'osm_xml':XML})
            cli.assert_not_called()
        self.assertEqual((folder/'task.json').read_bytes(), before)
        self.assertFalse((folder/'map.osm').exists())
        self.assertFalse(allowed_artifact('corridors/../../passwd'))

    def test_legacy_api_corridor_never_uploads_or_queries(self):
        state, folder = self.task()
        state['map_provider'] = 'osm'
        class Model:
            semantic_review = True
            def decide(self, *args): raise AssertionError('旧OSM任务不得启动模型')
        with patch('amap_fetch._request') as amap, patch('map_fetch._download') as osm:
            with self.assertRaisesRegex(ValueError, '本次'):
                agent.run(state, folder, Model(), consent_map=True)
            amap.assert_not_called(); osm.assert_not_called()
        self.assertEqual((folder/'map.osm').read_text(), XML)

    def test_missing_osm_or_missing_names_never_generates_coordinates(self):
        folder = self.root/'missing'
        state = agent.start(self.image,folder,True,'corridor')
        done = agent.run(state,folder,agent.LocalPlanner())
        self.assertEqual(done['status'],'waiting_for_user')
        self.assertNotIn('corridor_artifacts',done)
        (folder/'map.osm').write_text(XML)
        done['map_provider'] = 'osm'
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
