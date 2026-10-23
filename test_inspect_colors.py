"""Offline synthetic coverage for bounded color proposals."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

import inspect_colors as module


class InspectColorsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'input.png'
        self.out = self.root / 'out'

    def save(self, image):
        image.save(self.source)

    def inspect(self, crop=None):
        return module.inspect_colors(self.source, self.out, crop)

    def test_red_wraparound(self):
        hsv = np.zeros((40, 40, 3), dtype=np.uint8)
        hsv[:, :, 1:] = 255
        hsv[:, 20:, 0] = 255
        self.save(Image.frombytes('HSV', (40, 40), hsv.tobytes()).convert('RGB'))
        result = self.inspect()
        self.assertEqual(len(result['candidates']), 1)
        candidate = result['candidates'][0]
        self.assertEqual(candidate['supporting_pixel_count'], 1600)
        self.assertEqual(candidate['bbox_xyxy'], [0, 0, 40, 40])
        self.assertTrue(candidate['color'].startswith('#FF00'))
        self.assertNotIn('confidence', candidate)

    def test_two_colors_deterministic_and_outputs(self):
        image = Image.new('RGB', (80, 40), 'red')
        image.paste('blue', (40, 0, 80, 40))
        self.save(image)
        first = self.inspect()
        first_preview = (self.out / 'color-preview.png').read_bytes()
        self.assertEqual({c['color'] for c in first['candidates']}, {'#FF0000', '#0000FF'})
        self.assertEqual([c['supporting_pixel_count'] for c in first['candidates']], [1600, 1600])
        self.assertEqual(first, json.loads((self.out / 'colors.json').read_text()))
        self.assertEqual(first, self.inspect())
        self.assertEqual(first_preview, (self.out / 'color-preview.png').read_bytes())
        with Image.open(self.out / 'color-preview.png') as preview:
            self.assertEqual(preview.format, 'PNG')

    def test_grayscale(self):
        self.save(Image.new('RGB', (40, 40), 'gray'))
        result = self.inspect()
        self.assertEqual(result['status'], 'no_signal')
        self.assertEqual(result['candidates'], [])
        self.assertTrue((self.out / 'color-preview.png').is_file())

    def test_transparent_pixels_do_not_propose_hidden_color(self):
        self.save(Image.new('RGBA', (40, 40), (255, 0, 0, 0)))
        self.assertEqual(self.inspect()['candidates'], [])

    def test_invalid_crops(self):
        self.save(Image.new('RGB', (40, 40), 'red'))
        for crop in ([0, 0, 41, 40], [-1, 0, 10, 10], [2, 0, 1, 4],
                     [0, 0, 0, 1], [0., 0, 10, 10], [False, 0, 10, 10],
                     [0, 0, 10], 'full'):
            with self.subTest(crop=crop), self.assertRaises(ValueError):
                self.inspect(crop)
        self.assertFalse(self.out.exists())

    def test_crop_coordinates(self):
        self.save(Image.new('RGB', (100, 80), 'red'))
        result = self.inspect([10, 20, 50, 60])
        self.assertEqual(result['crop'], [10, 20, 50, 60])
        self.assertEqual(result['candidates'][0]['bbox_xyxy'], [10, 20, 50, 60])

    def test_exif_orientation(self):
        image = Image.new('RGB', (60, 30), 'red')
        exif = Image.Exif()
        exif[274] = 6
        image.save(self.source, exif=exif)
        result = self.inspect([0, 30, 30, 60])
        self.assertEqual(result['image_size'], [30, 60])
        self.assertEqual(result['candidates'][0]['bbox_xyxy'], [0, 30, 30, 60])
        with self.assertRaises(ValueError):
            self.inspect([0, 0, 60, 30])

    def test_downsample_bound(self):
        self.save(Image.new('RGB', (2048, 1024), 'red'))
        result = self.inspect()
        self.assertEqual(result['sample_size'], [512, 256])
        self.assertEqual(result['candidates'][0]['supporting_pixel_count'], 512 * 256)
        self.assertEqual(result['candidates'][0]['bbox_xyxy'], [0, 0, 2048, 1024])

    def test_limits_and_format(self):
        self.save(Image.new('RGB', (40, 40), 'red'))
        with patch.object(module, 'MAX_SOURCE_BYTES', 10), self.assertRaises(ValueError):
            self.inspect()
        with patch.object(module, 'MAX_PIXELS', 100), self.assertRaises(ValueError):
            self.inspect()
        Image.new('RGB', (10, 10), 'red').save(self.source, format='BMP')
        with self.assertRaises(ValueError):
            self.inspect()

    def test_source_overwrite_rejected(self):
        self.out.mkdir()
        for name in ('color-preview.png', 'colors.json'):
            source = self.out / name
            Image.new('RGB', (10, 10), 'red').save(source, format='PNG')
            original = source.read_bytes()
            with self.assertRaises(ValueError):
                module.inspect_colors(source, self.out)
            self.assertEqual(source.read_bytes(), original)
            source.unlink()

    def test_source_hardlink_rejected(self):
        self.save(Image.new('RGB', (10, 10), 'red'))
        self.out.mkdir()
        (self.out / 'color-preview.png').hardlink_to(self.source)
        with self.assertRaises(ValueError):
            self.inspect()

    def test_five_candidate_limit(self):
        hsv = np.zeros((40, 256, 3), dtype=np.uint8)
        hsv[:, :, 0] = np.arange(256)
        hsv[:, :, 1:] = 255
        self.save(Image.frombytes('HSV', (256, 40), hsv.tobytes()).convert('RGB'))
        self.assertLessEqual(len(self.inspect()['candidates']), 5)


if __name__ == '__main__':
    unittest.main()
