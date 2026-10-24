"""分量选择的断裂、多路线、大图标反例及真实原图回归（全本地）。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw
from route_components import select_components
from extract_route import extract
from centerline import process
from ride_agent import (start, run, LocalPlanner, apply_extraction_correction,
                        apply_component_selection, load)
from ride_web import RideServer, WebError


class SelectionTests(unittest.TestCase):
    def test_isolated_compact_only_with_dominant_long_band(self):
        m = np.zeros((300,840),bool)
        m[180:194,80:750] = True
        m[10:28,400:418] = True
        selected, s = select_components(m)
        self.assertEqual(s['status'],'auto_isolated_compact')
        self.assertFalse(selected[15,405])
        self.assertEqual(s['excluded_ids'],[2])
        self.assertEqual(sum(c['pixels'] for c in s['candidates']),int(m.sum()))
        restored, explicit = select_components(m,[1,2])
        self.assertTrue(np.array_equal(m,restored))
        self.assertEqual(explicit['status'],'explicit')

    def test_fragments_multiple_routes_and_large_icon_are_not_forced(self):
        cases = []
        # 同向断裂、两条平行路线、小片段近长带：都不能强制只选最大。
        for boxes in [[(20,150,300,164),(325,150,750,164)],
                      [(20,130,750,144),(20,180,700,194)],
                      [(20,150,750,164),(760,150,778,164)],
                      [(20,150,750,164),(350,10,450,110)],
                      [(20,150,300,164),(500,10,700,210)]]:
            m=np.zeros((300,840),bool)
            for x0,y0,x1,y1 in boxes:m[y0:y1,x0:x1]=True
            cases.append(m)
        for m in cases:
            with self.subTest(pixels=int(m.sum())):
                selected,s=select_components(m)
                self.assertEqual(s['status'],'ambiguous')
                self.assertTrue(s['requires_selection'])
                self.assertTrue(np.array_equal(m,selected))

    def test_invalid_explicit_selection(self):
        m=np.zeros((60,80),bool);m[20:25,10:70]=True
        for ids in [[],[True],[0],[2],[1,1],'1']:
            with self.assertRaises(ValueError):select_components(m,ids)

    def test_ambiguous_agent_pauses_then_explicit_multi_selection_is_local(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);im=Image.new('RGB',(200,90),'white');d=ImageDraw.Draw(im)
            d.line((10,30,190,30),fill='#6B798C',width=7)
            d.line((10,65,190,65),fill='#6B798C',width=7);im.save(p/'im.png')
            state=start(p/'im.png',p/'task')
            apply_extraction_correction(state,'#6B798C',[0,0,200,90])
            with patch('ride_agent.APIPlanner',side_effect=AssertionError('不能上传')):
                state=run(state,p/'task',LocalPlanner())
                self.assertEqual(state['status'],'waiting_for_user')
                self.assertFalse(state['centerlined'])
                self.assertEqual(state['component_selection']['selected_ids'],[1,2])
                apply_component_selection(state,p/'task',[1,2])
                state=run(state,p/'task',LocalPlanner())
            self.assertTrue(state['centerlined'])
            self.assertEqual(state['component_selection']['status'],'explicit')
            apply_extraction_correction(state,crop=[0,10,200,90])
            self.assertNotIn('component_ids',state['extraction_config'])
            with self.assertRaises(ValueError):apply_component_selection(state,p/'task',[1])

    def test_web_candidates_and_checkbox_choice_and_validation(self):
        import uuid
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);tid=str(uuid.uuid4());base=root/tid
            (base/'inputs').mkdir(parents=True)
            im=Image.new('RGB',(200,90),'white');d=ImageDraw.Draw(im)
            for y in (30,65):d.line((10,y,190,y),fill='#6B798C',width=7)
            im.save(base/'inputs/im.png');start(base/'inputs/im.png',base/'task')
            server=RideServer(0,output_root=root)
            try:
                r=server.resume(tid,{'local_only':True,'color':'#6B798C','crop':[0,0,200,90]})
                self.assertTrue(r['component_selection']['requires_selection'])
                self.assertTrue(any(a['name']=='extraction/components-overlay.png' and a['current'] for a in r['artifacts']))
                for ids in [[True],[0],[1,1],[],[999]]:
                    with self.assertRaises(WebError):server.resume(tid,{'local_only':True,'component_ids':ids})
                r=server.resume(tid,{'local_only':True,'component_ids':[1,2]})
                self.assertEqual(r['status'],'needs_confirmation')
                self.assertEqual(r['component_selection']['selected_ids'],[1,2])
                self.assertTrue(load(base/'task')['centerlined'])
            finally:server.server_close()


@unittest.skipUnless(os.environ.get('RIDE_REAL_SCREENSHOT'),'仅显式本地原图，不提交图片')
class RealComponentTests(unittest.TestCase):
    def test_large_crop_icons_excluded_and_old_crop_kept_end_to_end(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)
            for name,crop,color,expected in [('large',[200,1160,1040,1460],'#6B798C',10366),
                                             ('old',[235,1200,1010,1420],'#6A778B',10363)]:
                with self.subTest(name=name):
                    folder=p/name
                    state=start(os.environ['RIDE_REAL_SCREENSHOT'],folder)
                    apply_extraction_correction(state,color,crop)
                    with patch('ride_agent.APIPlanner',side_effect=AssertionError('禁止原图上传')):
                        state=run(state,folder,LocalPlanner())
                    self.assertTrue(state['centerlined'])
                    self.assertFalse(state['verified'])
                    r=json.loads((folder/'extraction/extraction.json').read_text())
                    # 折返保护改为原始薄边截线后，旧输出像素数不再作为真值；
                    # 实心截线、图标排除及拓扑断言仍必须同时通过。
                    self.assertEqual(r['selected_pixels'],expected)
                    mask=np.array(Image.open(folder/'extraction/mask.png'))>0
                    graph=json.loads((folder/'centerline/pixel-graph.json').read_text())
                    self.assertEqual(graph['component_count'],1)
                    self.assertEqual(graph['cycle_rank'],0)
                    if name=='large':
                        self.assertEqual(r['raw_pixels'],4248)
                        self.assertEqual(r['color_candidate_pixels'],10905)
                        self.assertEqual(r['component_selection']['excluded_ids'],[2,3,4])
                        self.assertEqual([c['pixels'] for c in r['component_selection']['candidates']],[10366,216,172,151])
                        self.assertFalse(mask[:100].any())
                        self.assertFalse((np.array(Image.open(folder/'centerline/skeleton.png'))[:100]>0).any())
                        for xy in [(235,180),(435,193),(635,206)]:
                            x,y=xy;self.assertTrue(mask[y-3:y+4,x].all())


if __name__=='__main__':unittest.main()
