"""折返结构保护：仅使用本地合成证据，不从静态图猜骑行次序。"""
import tempfile
from pathlib import Path
import unittest
import numpy as np
from PIL import Image, ImageDraw
from mask_cleanup import complete_open_interiors, fill_supported_interiors
from centerline import thin, analyze
from extract_route import extract


def hairpin(vertical=False, gap_color='#98A6BE'):
    """两条实心去回程在末端连接，间隙恰有旧算法会接受的灰色。"""
    mask = np.zeros((70,180), bool)
    mask[20:27,15:161] = True
    mask[35:42,15:161] = True
    mask[20:42,154:161] = True
    a = np.full((70,180,3), (238,242,248), np.uint8)
    a[27:35,15:154] = Image.new('RGB',(1,1),gap_color).getpixel((0,0))
    a[mask] = (106,119,139)
    if vertical:
        mask = mask.T.copy(); a = a.transpose(1,0,2).copy()
    return Image.fromarray(a), mask


class TurnbackGuardTests(unittest.TestCase):
    def test_same_component_narrow_u_keeps_gray_corridor(self):
        for vertical in (False, True):
            im, m = hairpin(vertical)
            fixed, s = complete_open_interiors(m, im)
            corridor = np.zeros((70,180),bool); corridor[27:35,20:145] = True
            if vertical: corridor = corridor.T
            self.assertEqual(int((fixed & corridor).sum()),0)
            self.assertTrue(np.all(fixed[m]))
            self.assertGreater(s['protected_cross_sections'],0)
            self.assertEqual(len(analyze(thin(fixed)[0])['endpoints_xy']),2)

    def test_diagonal_hairpin_keeps_gray_corridor(self):
        im,m=hairpin()
        im=im.rotate(35,expand=True,resample=Image.Resampling.NEAREST,fillcolor='#EEF2F8')
        rotated=Image.fromarray(m.astype('uint8')*255).rotate(
            35,expand=True,resample=Image.Resampling.NEAREST,fillcolor=0)
        m=np.array(rotated)>0
        fixed,s=complete_open_interiors(m,im)
        self.assertTrue(np.array_equal(fixed,m))
        self.assertGreater(s['protected_gap_pixels'],1000)

    def test_real_narrow_loop_preserves_gray_hole(self):
        im,m=hairpin();m[20:42,15:22]=True
        a=np.array(im);a[m]=(106,119,139);im=Image.fromarray(a)
        fixed,s=fill_supported_interiors(m,im)
        self.assertTrue(np.array_equal(fixed,m))
        self.assertGreater(s['protected_enclosed_regions'],0)
        fixed,_=complete_open_interiors(fixed,im)
        self.assertFalse(fixed[30,80])
        self.assertGreaterEqual(analyze(thin(fixed)[0])['cycle_rank'],1)

    def test_small_white_true_loop_not_arrow(self):
        m=np.zeros((40,40),bool);m[12:22,12:22]=True;m[14:20,14:20]=False
        a=np.full((40,40,3),255,np.uint8);a[m]=(106,119,139)
        fixed,s=fill_supported_interiors(m,Image.fromarray(a))
        self.assertTrue(np.array_equal(fixed,m));self.assertEqual(s['added_pixels'],0)

    def test_thin_edge_gray_band_still_completes(self):
        m=np.zeros((60,180),bool);m[20:22,15:161]=True;m[35:38,15:161]=True;m[20:38,160]=True
        im=Image.new('RGB',(180,60),'#EEF2F8');ImageDraw.Draw(im).rectangle((15,20,160,37),fill='#98A6BE')
        fixed,s=complete_open_interiors(m,im)
        self.assertTrue(fixed[25:34,80].all());self.assertGreater(s['open_added_pixels'],1000)

    def test_no_skipping_foreground_or_joining_other_components(self):
        im,m=hairpin();m[20:42,154:161]=False
        fixed,_=complete_open_interiors(m,im)
        self.assertTrue(np.array_equal(m,fixed))

    def test_pipeline_exposes_diagnostic_and_order_ambiguity(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);im,m=hairpin(gap_color='white');im.save(p/'im.png')
            r=extract(p/'im.png',p/'out','#6A778B')
            out=np.array(Image.open(p/'out/mask.png'))>0
            self.assertFalse(out[30,80]);self.assertTrue(out[23,80]);self.assertTrue(out[38,80])
            self.assertFalse(r['verified']);self.assertEqual(r['status'],'needs_confirmation')
            self.assertTrue(any('同路往返' in w for w in r['warnings']))
            self.assertTrue((p/'out/structure-diagnostics.png').exists())

    def test_static_retraced_line_never_claims_ride_order(self):
        m=np.zeros((30,90),bool);m[13:18,10:80]=True
        r=analyze(thin(m)[0])
        self.assertEqual(r['path_direction'],'arbitrary_not_ride_direction')
        self.assertFalse(r['verified'])
        self.assertTrue(any('Repeated traversal' in w for w in r['warnings']))

if __name__=='__main__':unittest.main()
