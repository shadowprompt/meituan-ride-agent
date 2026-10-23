import copy
import json
from pathlib import Path
import tempfile
import unittest
from PIL import Image, ImageDraw
from ride_agent import start, run, DemoPlanner, apply_extraction_correction, controls_current


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder=Path(self.tmp.name)/'task'
        source=Path(self.tmp.name)/'source.png'
        image=Image.new('RGB',(80,60),'white')
        ImageDraw.Draw(image).line([(10,40),(50,40),(50,10)],fill='#00BFA5',width=5)
        image.save(source)
        self.state=start(source,self.folder,True)
        self.state['messages'].append({'role':'user','content':json.dumps({'crop':[0,0,80,60],'color':'#00BFA5'})})
        run(self.state,self.folder,DemoPlanner())

    def test_register_refuses_unbound_or_mismatched_controls(self):
        (self.folder/'controls.json').write_text(Path('examples/synthetic-controls.json').read_text())
        self.state['controls_invalidated']=False
        self.assertFalse(controls_current(self.state,self.folder))
        self.state['controls_crop']=[1,0,80,60]
        self.assertFalse(controls_current(self.state,self.folder))
        class Register:
            def decide(self,*args):return {'action':'register'}
        result=run(self.state,self.folder,Register())
        self.assertEqual(result['status'],'error')
        self.state['controls_crop']=[0,0,80,60]
        self.assertTrue(controls_current(self.state,self.folder))

    def test_color_only_preserves_controls_frame(self):
        self.state['controls_invalidated']=False
        apply_extraction_correction(self.state,color='#00bfa5')
        self.assertFalse(self.state['controls_invalidated'])
        result=run(self.state,self.folder,DemoPlanner())
        self.assertEqual(result['status'],'needs_confirmation')
        self.assertEqual(result['extraction_config']['crop'],[0,0,80,60])

    def test_changed_crop_invalidates_controls_and_inspection(self):
        self.state['colors_inspected']=True
        apply_extraction_correction(self.state,crop=[5,5,75,55])
        self.assertTrue(self.state['controls_invalidated'])
        self.assertFalse(self.state['colors_inspected'])
        self.assertFalse(self.state['registered'])
        result=run(self.state,self.folder,DemoPlanner())
        self.assertEqual(result['extraction_config']['crop'],[5,5,75,55])

    def test_bad_correction_does_not_mutate_state(self):
        before=copy.deepcopy(self.state)
        with self.assertRaises(ValueError):apply_extraction_correction(self.state,color='#zzzzzz')
        self.assertEqual(before,self.state)
        with self.assertRaises(ValueError):apply_extraction_correction(self.state,crop=[0,0,900,10])
        self.assertEqual(before,self.state)

    def test_model_crop_change_invalidates_old_controls(self):
        class Change:
            def decide(self,*args):return {'action':'extract','color':'#00BFA5','crop':[5,5,75,55]}
        self.state.update(registered=True,registration_gate_passed=True)
        result=run(self.state,self.folder,Change(),budget=1)
        self.assertTrue(result['controls_invalidated'])
        self.assertFalse(result['registered'])
        self.assertFalse(result['registration_gate_passed'])

    def test_user_constraints_override_model_reextraction(self):
        apply_extraction_correction(self.state,color='#00BFA5')
        run(self.state,self.folder,DemoPlanner())
        class Change:
            def decide(self,*args):return {'action':'extract','color':'#FF0000','crop':[5,5,75,55]}
        result=run(self.state,self.folder,Change(),budget=1)
        self.assertEqual(result['extraction_config'],{'color':'#00BFA5','crop':[0,0,80,60]})

    def test_unknown_old_frame_fails_closed(self):
        self.state.pop('extraction_config')
        class Change:
            def decide(self,*args):return {'action':'extract','color':'#00BFA5','crop':[0,0,80,60]}
        result=run(self.state,self.folder,Change(),budget=1)
        self.assertTrue(result['controls_invalidated'])


if __name__=='__main__':unittest.main()
