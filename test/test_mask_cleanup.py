"""中文回归：修复边缘双线，但不得把真实环路或开放断点补成路线。"""
import unittest
import numpy as np
from PIL import Image, ImageDraw
from mask_cleanup import fill_supported_interiors
from centerline import thin, analyze


class CleanupTests(unittest.TestCase):
    def band(self, interior='#A0A7AF'):
        im = Image.new('RGB',(160,60),'#EEF2F8')
        ImageDraw.Draw(im).rectangle((10,20,150,34),fill=interior)
        mask = np.zeros((60,160),dtype=bool)
        mask[20,10:151] = True; mask[34,10:151] = True
        mask[20:35,10] = True; mask[20:35,150] = True
        return im,mask

    def test_fill_band_not_background_and_thin_once(self):
        im,mask=self.band()
        fixed,stats=fill_supported_interiors(mask,im)
        self.assertTrue(fixed[26,80]); self.assertFalse(fixed[5,80])
        self.assertGreater(stats['added_pixels'],0)
        report=analyze(thin(fixed)[0])
        self.assertEqual(report['cycle_rank'],0)
        self.assertEqual(len(report['endpoints_xy']),2)

    def test_real_loop_with_background_stays(self):
        im,mask=self.band('#EEF2F8')
        fixed,stats=fill_supported_interiors(mask,im)
        self.assertTrue(np.array_equal(fixed,mask))
        self.assertEqual(stats['added_pixels'],0)

    def test_open_band_not_closed(self):
        im,mask=self.band();mask[20:35,10]=False
        fixed,_=fill_supported_interiors(mask,im)
        self.assertTrue(np.array_equal(fixed,mask))

    def test_small_internal_arrow(self):
        im=Image.new('RGB',(60,30),'#87929F')
        mask=np.ones((30,60),dtype=bool); mask[12:16,25:30]=False
        ImageDraw.Draw(im).rectangle((25,12,29,15),fill='white')
        fixed,s=fill_supported_interiors(mask,im)
        self.assertTrue(fixed.all());self.assertEqual(s['added_pixels'],20)

    def test_large_white_loop_never_filled(self):
        im,mask=self.band('white')
        fixed,_=fill_supported_interiors(mask,im)
        self.assertTrue(np.array_equal(fixed,mask))

if __name__=='__main__':unittest.main()
