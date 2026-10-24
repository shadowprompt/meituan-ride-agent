"""Offline crop coverage and honest extraction feedback regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw
from ride_agent import (APIPlanner, PROMPT, apply_extraction_correction,
                        crop_diagnostics, invalidate_extraction, run, start)


class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = iter(actions)

    def decide(self, state, folder):
        return next(self.actions)


class CropGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root/'source.png'
        image = Image.new('RGB', (200, 100), 'white')
        ImageDraw.Draw(image).line([(10, 50), (190, 50)], fill='#808890', width=7)
        image.save(source)
        self.folder = self.root/'task'
        self.state = start(source, self.folder, demo=True)

    def extract(self, crop):
        return run(self.state, self.folder, ScriptedPlanner([
            {'action':'extract', 'crop':crop, 'color':'#808890'}]), budget=1)

    def test_ratios_warning_thresholds_and_no_semantic_claims(self):
        result = crop_diagnostics([80, 0, 100, 100], [200, 100])
        self.assertEqual(result['width_fraction'], 0.1)
        self.assertEqual(result['height_fraction'], 1)
        self.assertEqual(result['area_fraction'], 0.1)
        self.assertEqual(len(result['warnings']), 3)
        self.assertEqual(result['route_completeness'], 'unverified')
        self.assertEqual(result['route_endpoints'], 'unverified')
        boundary = crop_diagnostics([0, 0, 50, 100], [200, 100])
        self.assertEqual(len(boundary['warnings']), 1)
        full = crop_diagnostics([0, 0, 200, 100], [200, 100])
        self.assertFalse(full['partial_crop'])
        self.assertEqual(full['warnings'], [])
        self.assertEqual(full['route_completeness'], 'unverified')

    def test_user_narrow_crop_preserved_tools_execute_and_delivery_qualified(self):
        crop = [70, 0, 110, 100]
        apply_extraction_correction(self.state, color='#808890', crop=crop)
        result = run(self.state, self.folder, ScriptedPlanner([
            {'action':'centerline'}, {'action':'deliver', 'summary':'候选输出。'}]))
        self.assertEqual(result['status'], 'needs_confirmation')
        self.assertTrue(result['extracted'])
        self.assertTrue(result['centerlined'])
        self.assertEqual(result['extraction_config']['crop'], crop)
        self.assertIn('未验证完整路线或真实起终点', result['summary'])
        self.assertFalse(result['verified'])
        report = json.loads((self.folder/'extraction/extraction.json').read_text())
        feedback = next(json.loads(m['content'].split(' ', 1)[1])
                        for m in result['messages']
                        if m['content'].startswith('LOCAL_TOOL_RESULT '))
        for key in ('crop_xyxy', 'oriented_image_size', 'segmentation_mode'):
            self.assertEqual(feedback[key], report[key])
        self.assertTrue(all(w in feedback['warnings'] for w in report['warnings']))
        self.assertEqual(feedback['crop_diagnostics']['area_fraction'], 0.2)

    def test_current_api_feedback_includes_diagnostics_after_history(self):
        self.extract([70, 0, 110, 100])
        captured = {}
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def read(self, limit):
                return json.dumps({'choices':[{'message':{'content':'{"action":"centerline"}'}}]}).encode()
        class Opener:
            def open(self, request, timeout):
                captured.update(json.loads(request.data))
                return Response()
        with patch.dict('os.environ', {'RIDE_API_KEY':'test', 'RIDE_MODEL':'test'}), \
                patch('ride_agent.urllib.request.build_opener', return_value=Opener()):
            APIPlanner().decide(self.state, self.folder)
        current = captured['messages'][-1]['content'][0]['text']
        self.assertIn('extraction_diagnostics', current)
        self.assertIn('neutral_brightness', current)
        self.assertIn('oriented_image_size', current)
        invalidate_extraction(self.state)
        self.assertNotIn('extraction_diagnostics', self.state)

    def test_reextract_full_image_removes_old_narrow_warning(self):
        self.extract([70, 0, 110, 100])
        self.extract([0, 0, 200, 100])
        diagnostics = self.state['extraction_diagnostics']
        self.assertFalse(diagnostics['crop_diagnostics']['partial_crop'])
        self.assertEqual(diagnostics['crop_diagnostics']['warnings'], [])
        self.assertFalse(any('狭窄裁剪' in w for w in diagnostics['warnings']))

    def test_prompt_covers_interior_color_and_no_endpoint_inference(self):
        for phrase in ('实心线内部', '灰色/蓝灰', '不得把骨架端点',
                       '不要自动拒绝', '不能直接声称完整路线'):
            self.assertIn(phrase, PROMPT)


if __name__ == '__main__':
    unittest.main()
