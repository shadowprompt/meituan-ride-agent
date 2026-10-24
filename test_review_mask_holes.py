"""逐孔人工补全的边界、绑定及真实图回归。"""
import json
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from review_mask_holes import review, enclosed_holes


class HoleReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.mask = np.zeros((70,100),bool); self.mask[10:60,10:90] = True
        self.mask[20:24,20:24] = False; self.mask[30:34,40:44] = False
        self.mask[40:60,60:63] = False  # 开放缺口不能成为候选。
        Image.new('RGB',(100,70),'gray').save(self.p/'source.png')
        self.save_mask()

    def save_mask(self):
        Image.fromarray(self.mask.astype('uint8')*255).save(self.p/'mask.png')

    def run_review(self, selection=None):
        return review(self.p/'source.png',self.p/'mask.png',self.p/'out',selection)

    def select(self,ids):
        self.run_review()
        report = json.loads((self.p/'out/review.json').read_text())
        f = self.p/'selection.json'
        f.write_text(json.dumps({'fingerprint':report['fingerprint'],
                                'confirm_source_reviewed':True,'fill_hole_ids':ids}))
        return f

    def test_preview_does_not_modify_and_no_network_or_default_selection(self):
        r = self.run_review()
        self.assertEqual(r['added_pixels'],0)
        self.assertEqual(len(enclosed_holes(self.mask)),2)
        page = (self.p/'out/review.html').read_text()
        self.assertNotIn('fetch(',page)
        self.assertNotIn(' checked',page)
        self.assertTrue((self.p/'out/hole-1.png').exists())

    def test_explicit_single_hole_only(self):
        r = self.run_review(self.select([1]))
        m = np.array(Image.open(self.p/'out/mask-corrected.png'))>0
        self.assertTrue(m[20:24,20:24].all())
        self.assertFalse(m[31,41]); self.assertFalse(m[50,61])
        self.assertEqual(r['added_pixels'],16)
        self.assertFalse(r['verified'])
        self.assertTrue(np.all(m[self.mask]))

    def test_stale_source_or_mask_rejected(self):
        f = self.select([1]); self.mask[12,12] = False; self.save_mask()
        with self.assertRaisesRegex(ValueError,'改变'): self.run_review(f)
        self.mask[12,12] = True; self.save_mask()
        Image.new('RGB',(100,70),'white').save(self.p/'source.png')
        with self.assertRaisesRegex(ValueError,'改变'): self.run_review(f)

    def test_unknown_duplicate_boolean_and_missing_confirmation(self):
        for ids in ([3],[1,1],[True], '1'):
            with self.assertRaises(ValueError): self.run_review(self.select(ids))
        f = self.select([1]); d = json.loads(f.read_text()); d['confirm_source_reviewed'] = False
        f.write_text(json.dumps(d))
        with self.assertRaises(ValueError): self.run_review(f)

    def test_large_real_loop_is_ineligible(self):
        self.mask[12:40,12:40] = False; self.save_mask()
        holes = enclosed_holes(self.mask)
        self.assertFalse(holes[0]['eligible'])
        with self.assertRaises(ValueError): self.run_review(self.select([1]))

    def test_alias_shape_and_binary_fail_closed(self):
        with self.assertRaises(ValueError):
            review(self.p/'source.png',self.p/'mask.png',self.p)
        Image.new('L',(100,70),12).save(self.p/'mask.png')
        with self.assertRaises(ValueError): self.run_review()
        Image.new('L',(10,10),0).save(self.p/'mask.png')
        with self.assertRaises(ValueError): self.run_review()


@unittest.skipUnless(os.environ.get('RIDE_TURNBACK_SCREENSHOT'),'需本地折返原图')
class RealHoleReviewTests(unittest.TestCase):
    def test_explicit_review_preserves_real_gap_and_icon_break(self):
        from extract_route import extract
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            extract(os.environ['RIDE_TURNBACK_SCREENSHOT'],p/'e','#6A778B',[240,1140,1000,1580])
            review(p/'e/source-crop.png',p/'e/mask.png',p/'r')
            manifest = json.loads((p/'r/review.json').read_text())
            self.assertEqual([h['area'] for h in manifest['holes']],[61,197,198,103,2,2,5])
            # 工程可视审阅的保守选择，仅独立白箭头2与灰带漏选4；混合孔3不整孔猜填。
            f = p/'selection.json'; f.write_text(json.dumps({'fingerprint':manifest['fingerprint'],
                'confirm_source_reviewed':True,'fill_hole_ids':[2,4]}))
            r = review(p/'e/source-crop.png',p/'e/mask.png',p/'r',f)
            self.assertEqual(r['added_pixels'],300)
            self.assertEqual(r['before']['cycle_rank'],7)
            self.assertEqual(r['after']['component_count'],2)
            self.assertLess(r['after']['cycle_rank'],7)
            m = np.array(Image.open(p/'r/mask-corrected.png'))>0
            for x,y in [(714,40),(717,70),(642,270),(638,280),(639,280)]:
                self.assertFalse(m[y,x],(x,y))
            self.assertFalse(r['verified'])


if __name__ == '__main__':
    unittest.main()
