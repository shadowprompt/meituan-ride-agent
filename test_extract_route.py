import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image, ImageDraw
from extract_route import extract, segment, parse_color


class ExtractionTests(unittest.TestCase):
    def sample(self):
        im = Image.new('RGB', (100, 80), 'white')
        d = ImageDraw.Draw(im)
        d.line([(10, 60), (60, 60), (60, 10), (40, 10), (60, 10)], fill='#00BFA5', width=5)
        d.point((90, 75), fill='#00BFA5')
        d.rectangle((75, 10, 85, 20), fill='red')
        return im

    def test_color_and_noise(self):
        mask, stats = segment(self.sample(), parse_color('#00BFA5'))
        self.assertTrue(mask[60, 20])
        self.assertTrue(mask[10, 45])
        self.assertFalse(mask[75, 90])
        self.assertFalse(mask[15, 80])
        self.assertEqual(len(stats['components']), 1)

    def test_hue_wrap(self):
        image = Image.new('RGB', (10, 10), '#ff0010')
        mask, _ = segment(image, (255, 0, 0), min_component=1)
        self.assertTrue(mask.all())

    def test_invalid_settings(self):
        for value in ['red', '#fff', '#GGFFFF']:
            with self.assertRaises(ValueError):
                parse_color(value)
        for kwargs in [{'hue_tolerance': float('nan')}, {'min_component': 0},
                       {'min_saturation': 2}]:
            with self.assertRaises(ValueError):
                segment(self.sample(), (0, 191, 165), **kwargs)
        with self.assertRaises(ValueError):
            segment(self.sample(), (128, 128, 128))

    def test_artifacts_and_crop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, out = Path(tmp)/'input.png', Path(tmp)/'out'
            self.sample().save(path)
            report = extract(path, out, '#00BFA5', [5, 5, 70, 70])
            for name in ('mask.png', 'overlay.png'):
                with Image.open(out/name) as artifact:
                    self.assertEqual(artifact.size, (65, 65))
            self.assertEqual(report['status'], 'needs_confirmation')
            self.assertFalse(report['original_gps'])
            self.assertEqual(json.loads((out/'extraction.json').read_text())['crop_xyxy'], [5,5,70,70])
            self.assertEqual(report['components'][0]['bbox_crop_xyxy'][0], 5)
            for crop in [[-1,0,10,10], [5,5,5,10], [0,0,101,80]]:
                with self.assertRaises(ValueError):
                    extract(path, out, '#00BFA5', crop)

    def test_no_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'white.png'
            Image.new('RGB', (20,20), 'white').save(path)
            report = extract(path, Path(tmp)/'out', '#00BFA5')
            self.assertEqual(report['status'], 'no_detection')
            self.assertEqual(report['selected_pixels'], 0)

    def test_multiple_components_preserved(self):
        image = Image.new('RGB', (40,40), 'white')
        draw = ImageDraw.Draw(image)
        for x in (2, 25):
            draw.rectangle((x, 2, x+5, 8), fill='#00BFA5')
        mask, stats = segment(image, (0,191,165))
        self.assertEqual(len(stats['components']), 2)
        self.assertEqual(int(mask.sum()), 84)


if __name__ == '__main__':
    unittest.main()
