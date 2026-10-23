import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from ride_agent import start, run, load, save, DemoPlanner, APIPlanner


class AgentTests(unittest.TestCase):
    def fixture(self, root):
        path=Path(root)/'image.png'
        im=Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(im).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        im.save(path)
        return path

    def test_persistent_demo_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=start(self.fixture(tmp),folder,True)
            state=run(state,folder,DemoPlanner())
            self.assertEqual(state['status'],'waiting_for_user')
            state=load(folder)
            state['messages'].append({'role':'user','content':json.dumps({'crop':[0,0,80,60],'color':'#00BFA5'})})
            state=run(state,folder,DemoPlanner())
            self.assertEqual(state['status'],'needs_confirmation')
            self.assertFalse(state['verified'])
            for name in state['artifacts']:
                self.assertTrue((folder/name).exists())
            self.assertEqual(load(folder)['status'],'needs_confirmation')
            with self.assertRaises(ValueError): start(self.fixture(tmp),folder,True)

    def test_unknown_tool_refused(self):
        class Bad:
            def decide(self,*args):return {'action':'shell','command':'echo test'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Bad())
            self.assertEqual(state['status'],'error')
            self.assertIn('Unknown action',state['error'])

    def test_no_false_delivery(self):
        class Bad:
            def decide(self,*args):return {'action':'deliver','summary':'finished'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Bad())
            self.assertEqual(state['status'],'error')

    def test_loop_budget(self):
        class Repeater:
            def decide(self,*args):return {'action':'extract','crop':[0,0,80,60],'color':'#00BFA5'}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)/'task'
            state=run(start(self.fixture(tmp),folder,True),folder,Repeater(),budget=2)
            self.assertEqual(state['status'],'paused')

    def test_api_security(self):
        with patch.dict(os.environ,{'RIDE_API_BASE':'http://example.com/v1','RIDE_API_KEY':'fake','RIDE_MODEL':'fake'}):
            with self.assertRaises(ValueError):APIPlanner()
        with patch.dict(os.environ,{'RIDE_API_BASE':'https://user:pass@example.com/v1','RIDE_API_KEY':'fake','RIDE_MODEL':'fake'}):
            with self.assertRaises(ValueError):APIPlanner()


if __name__=='__main__': unittest.main()
