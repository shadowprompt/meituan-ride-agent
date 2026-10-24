"""淡灰折返实图与可解释截线证据回归；私有图片仅由环境变量提供。"""
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from extract_route import extract
from mask_cleanup import complete_open_interiors
from centerline import process


class SourceContrastTests(unittest.TestCase):
    def band(self, exterior=245, interior=190, thick=False):
        a = np.full((65,100,3), exterior, np.uint8)
        m = np.zeros((65,100), bool)
        m[20:22,10:90] = True
        m[35:37,10:90] = True
        if thick:
            m[16:22,10:90] = True
            m[35:41,10:90] = True
        a[22:35,10:90] = interior
        a[m] = 139
        return Image.fromarray(a), m

    def test_separate_thin_edges_need_source_contrast(self):
        im,m = self.band()
        fixed,s = complete_open_interiors(m,im)
        self.assertTrue(fixed[22:35,50].all())
        self.assertGreater(s['cross_component_supported_sections'],0)
        self.assertTrue(np.all(fixed[m]))
        # 同色、无外部对比、白色间隙、厚实臂均不足以跨分量连接。
        for kwargs in ({'interior':139}, {'exterior':190}, {'interior':255}, {'thick':True}):
            im,m = self.band(**kwargs)
            fixed,_ = complete_open_interiors(m,im)
            self.assertFalse(fixed[28,50], kwargs)

    def test_diagonal_edge_tangent_not_solid_arm(self):
        im,m = self.band()
        im = im.rotate(24,expand=True,resample=Image.Resampling.NEAREST,fillcolor='white')
        m = np.array(Image.fromarray(m.astype('uint8')*255).rotate(
            24,expand=True,resample=Image.Resampling.NEAREST,fillcolor=0))>0
        fixed,s = complete_open_interiors(m,im)
        self.assertGreater(s['open_added_pixels'],500)
        self.assertTrue(np.all(fixed[m]))


@unittest.skipUnless(os.environ.get('RIDE_TURNBACK_SCREENSHOT'), '需显式本地折返原图，图片不入 Git')
class RealTurnbackTests(unittest.TestCase):
    def test_non_arrow_sections_solid_bright_gap_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)
            source = os.environ['RIDE_TURNBACK_SCREENSHOT']
            with Image.open(source) as im:
                self.assertEqual(im.size,(1260,2720))
            # 根据本次原图另选解释性参数，不声称重现用户未知的页面参数。
            r = extract(source,p/'extract','#6A778B',[240,1140,1000,1580])
            m = np.array(Image.open(p/'extract/mask.png'))>0
            # 坐标均相对 crop；两臂各三处非白箭头截面必须实心。
            for y,x0,x1 in [(160,668,680),(160,684,690),(200,653,665),
                             (200,668,678),(270,627,639),(270,646,657)]:
                self.assertTrue(m[y,x0:x1].all(),(y,x0,x1))
            # 图上可见的亮间隔及顶部回转环中心保留，不能一把抹平两臂。
            for x,y in [(717,70),(642,270),(638,280),(639,280),(714,40)]:
                self.assertFalse(m[y,x],(x,y))
            self.assertGreater(r['mask_cleanup']['cross_component_supported_sections'],0)
            self.assertFalse(r['verified'])
            self.assertTrue(any('跨原始边缘分量' in w for w in r['warnings']))
            c = process(p/'extract/mask.png',p/'centerline')
            self.assertIsNone(c['ordered_path_xy'])
            self.assertFalse(c['verified'])
            # 图标遮挡与保守箭头残孔未伪造连接；不把两个端点作为验收捷径。
            self.assertGreater(c['component_count'],1)
            self.assertGreater(c['cycle_rank'],0)


if __name__ == '__main__':
    unittest.main()
