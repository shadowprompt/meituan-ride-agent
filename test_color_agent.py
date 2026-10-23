"""Color inspection is advisory, never silent extraction or geolocation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw
from ride_agent import start,run,load,DemoPlanner,visual_feedback


class ColorAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)/'task'
        source=Path(self.tmp.name)/'image.png'
        image=Image.new('RGB',(200,160),'white')
        ImageDraw.Draw(image).line([(20,120),(100,120),(100,30)],fill='#00BFA5',width=7)
        image.save(source)
        self.state=start(source,self.folder,True)

    def inspect(self,crop=None):
        class Planner:
            def decide(inner,state,folder):
                if state.get('colors_inspected'):
                    return {'action':'ask','question':'请确认哪种颜色是轨迹。'}
                action={'action':'inspect_colors'}
                if crop is not None:action['crop']=crop
                return action
        return run(self.state,self.folder,Planner())

    def test_inspection_persists_and_does_not_extract(self):
        result=self.inspect([0,0,200,160])
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertTrue(result['colors_inspected'])
        self.assertFalse(result['extracted'])
        self.assertFalse(result['verified'])
        self.assertTrue((self.folder/'inspection/colors.json').exists())
        self.assertTrue((self.folder/'inspection/color-preview.png').exists())
        self.assertTrue(load(self.folder)['color_inspection']['candidates'])
        self.assertEqual(len(visual_feedback(result,self.folder)),2)

    def test_scripted_candidate_to_extraction_delivery(self):
        class Scripted:
            # Fixture policy only, not a vision-model acceptance test.
            def decide(inner,state,folder):
                if not state.get('colors_inspected'):
                    return {'action':'inspect_colors'}
                if not state.get('extracted'):
                    report=state['color_inspection']
                    return {'action':'extract','crop':report['crop'],
                            'color':report['candidates'][0]['color']}
                if not state.get('centerlined'):
                    return {'action':'centerline'}
                return {'action':'deliver','summary':'合成单色夹具，未经路线确认。'}
        result=run(self.state,self.folder,Scripted())
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertTrue(result['centerlined'])
        self.assertFalse(result['verified'])
        self.assertIn('inspection/colors.json',result['artifacts'])
        for artifact in result['artifacts']:
            self.assertTrue((self.folder/artifact).exists())

    def test_no_silent_demo_parameter_selection(self):
        self.inspect()
        result=run(self.state,self.folder,DemoPlanner())
        self.assertEqual(result['status'],'waiting_for_user')
        self.assertFalse(result['extracted'])

    def test_invalid_crop_recovers_without_old_inspection(self):
        self.inspect()
        self.state['colors_inspected']=False
        result=self.inspect([0,0,999,999])
        self.assertEqual(result['status'],'error')
        self.assertFalse(result['colors_inspected'])
        self.assertEqual(visual_feedback(result,self.folder),[])

    def test_cannot_choose_filesystem_paths(self):
        class Bad:
            def decide(self,*args):return {'action':'inspect_colors','image':'/etc/passwd'}
        with patch('ride_agent.inspect_colors') as inspect:
            result=run(self.state,self.folder,Bad())
            inspect.assert_not_called()
            self.assertEqual(result['status'],'error')

    def test_inspection_keeps_existing_extraction_valid(self):
        self.state.update(extracted=True,centerlined=True)
        result=self.inspect()
        self.assertTrue(result['extracted'])
        self.assertTrue(result['centerlined'])


if __name__=='__main__':unittest.main()
