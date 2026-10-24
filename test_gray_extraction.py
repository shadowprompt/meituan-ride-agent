"""Deterministic synthetic acceptance: segmentation, not route/GPS reconstruction."""
from pathlib import Path
import tempfile
import unittest
import numpy as np
from PIL import Image, ImageDraw
from extract_route import segment, extract
from inspect_colors import inspect_colors


class GrayExtractionTests(unittest.TestCase):
    def fixture(self):
        im = Image.new('RGB', (240, 160), '#DFE5EF')
        d = ImageDraw.Draw(im)
        d.line([(20,120),(100,120),(100,40),(210,40)], fill='#87929F', width=7)
        d.rectangle((30,20,34,26), fill='#87929F')  # small label-like distractor
        d.rectangle((150,100,200,130), fill='#00BFA5')
        d.line([(60,117),(65,120),(60,123)], fill='white', width=2)  # real occlusion
        return im

    def test_tinted_gray_recall_and_distractors(self):
        im = self.fixture()
        mask, stats = segment(im, (135,146,159))
        expected = np.all(np.asarray(im) == (135,146,159), axis=2)
        expected[20:27,30:35] = False
        self.assertGreater((mask & expected).sum()/expected.sum(), .99)
        self.assertFalse(mask[23,32])
        self.assertFalse(mask[110,180])
        self.assertFalse(mask[10,10])
        self.assertEqual(stats['segmentation_mode'], 'neutral_brightness')
        self.assertFalse(mask[120,65])  # no fabricated gap filling

    def test_gray_proposals_and_metadata(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'input.png'; self.fixture().save(p)
            proposals = inspect_colors(p,Path(t)/'inspect')
            self.assertTrue(proposals['neutral_candidates'])
            report = extract(p,Path(t)/'extract','#87929F')
            self.assertFalse(report['verified'])
            self.assertEqual(report['segmentation_mode'],'neutral_brightness')
            self.assertIn('gray_value_tolerance',report['parameters'])

    def test_black_white_and_invalid_gray_thresholds(self):
        for level in (0, 128, 255):
            im = Image.new('RGB',(60,30),'#4488ff')
            ImageDraw.Draw(im).line((5,15,50,15),fill=(level,)*3,width=3)
            mask,_ = segment(im,(level,)*3)
            self.assertTrue(mask[15,25]); self.assertFalse(mask[0,0])
        for kw in ({'gray_value_tolerance':float('nan')},
                   {'gray_max_saturation':2},{'gray_min_span':True}):
            with self.assertRaises(ValueError):
                segment(self.fixture(),(128,)*3,**kw)

if __name__ == '__main__': unittest.main()
