"""原图受控补全、网络故障分类及离线恢复的端到端回归。"""
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import json
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw
from mask_cleanup import complete_open_interiors
from ride_agent import start, run, LocalPlanner, ConsentedPlanner, apply_extraction_correction
from ride_web import processing_category, RideServer
from extract_route import extract
from centerline import process


class OpenBandTests(unittest.TestCase):
    def band(self, interior='#98A6BE', connected=True):
        im = Image.new('RGB',(180,70),'#EEF2F8')
        ImageDraw.Draw(im).rectangle((15,24,160,38),fill=interior)
        m=np.zeros((70,180),bool);m[24,15:161]=1;m[38,15:161]=1
        if connected:m[24:39,160]=1
        return im,m

    def test_open_supported_band_not_dependent_on_enclosed_hole(self):
        im,m=self.band();fixed,s=complete_open_interiors(m,im)
        self.assertTrue(fixed[30,90]);self.assertFalse(fixed[10,90])
        self.assertGreater(s['open_added_pixels'],1000)

    def test_no_join_of_separate_parallel_roads_even_gray_gap(self):
        im,m=self.band(connected=False)
        fixed,_=complete_open_interiors(m,im)
        self.assertTrue(np.array_equal(m,fixed))

    def test_background_white_and_overwide_loops_not_filled(self):
        for color in ['#EEF2F8','white']:
            im,m=self.band(color);fixed,_=complete_open_interiors(m,im)
            self.assertTrue(np.array_equal(m,fixed))
        im=Image.new('RGB',(180,90),'#98A6BE')
        m=np.zeros((90,180),bool);m[15,15:160]=1;m[65,15:160]=1;m[15:66,159]=1
        fixed,_=complete_open_interiors(m,im)
        self.assertFalse(fixed[40,90])

    def test_local_pipeline_never_needs_api_to_centerline_or_deliver(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);im=Image.new('RGB',(180,70),'white')
            ImageDraw.Draw(im).line((15,30,160,30),fill='#6A778B',width=15);im.save(p/'image.png')
            state=start(p/'image.png',p/'task')
            apply_extraction_correction(state,'#6A778B',[0,0,180,70])
            with patch('ride_agent.APIPlanner',side_effect=AssertionError('no upload')):
                result=run(state,p/'task',LocalPlanner())
            self.assertEqual(result['status'],'needs_confirmation')
            self.assertTrue(result['centerlined']);self.assertFalse(result['verified'])
            for artifact in result['artifacts']:self.assertTrue((p/'task'/artifact).exists())

    def test_disguised_tiff_reports_actual_format_without_accepting_it(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);Image.new('RGB',(80,60),'white').save(p/'image.png',format='TIFF')
            for call in [lambda:start(p/'image.png',p/'task'),
                         lambda:extract(p/'image.png',p/'out','#6A778B')]:
                with self.assertRaisesRegex(ValueError,'decoded image format: TIFF'):
                    call()

    def test_web_api_task_resumes_locally_without_consent(self):
        import uuid
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);task_id=str(uuid.uuid4());base=root/task_id
            base.mkdir();(base/'inputs').mkdir()
            im=Image.new('RGB',(90,60),'white')
            ImageDraw.Draw(im).line((10,30,80,30),fill='#6A778B',width=7);im.save(base/'inputs/image.png')
            start(base/'inputs/image.png',base/'task')
            server=RideServer(0,output_root=root)
            try:
                result=server.resume(task_id,{'local_only':True,'color':'#6A778B','crop':[0,0,90,60]})
                self.assertEqual(result['status'],'needs_confirmation')
                self.assertTrue(any(a['name']=='centerline/skeleton.png' and a['current'] for a in result['artifacts']))
            finally:server.server_close()

    def test_resume_cli_without_upload_consent_or_api_credentials(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);im=Image.new('RGB',(90,60),'white')
            ImageDraw.Draw(im).line((10,30,80,30),fill='#6A778B',width=7);im.save(p/'im.png')
            start(p/'im.png',p/'task')
            env={k:v for k,v in os.environ.items() if not k.startswith('RIDE_API') and k!='RIDE_MODEL'}
            result=subprocess.run([sys.executable,str(Path(__file__).resolve().parent.parent/'src'/'ride_agent.py'),
                'resume','--task-dir',str(p/'task'),'--local-only','--color','#6A778B',
                '--crop','0','0','90','60'],capture_output=True,text=True,env=env,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['status'],'needs_confirmation')

    def test_network_timeout_not_missing_stage_and_saved_mask_resumes_offline(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);im=Image.new('RGB',(90,60),'white')
            ImageDraw.Draw(im).line((10,30,80,30),fill='#6A778B',width=7);im.save(p/'im.png')
            state=start(p/'im.png',p/'task')
            with patch('ride_agent.APIPlanner') as api:
                api.return_value.decide.side_effect=TimeoutError('write timed out')
                result=run(state,p/'task',ConsentedPlanner())
            self.assertEqual(processing_category(result),'api_timeout')
            apply_extraction_correction(result,'#6A778B',[0,0,90,60])
            result=run(result,p/'task',LocalPlanner())
            self.assertTrue(result['centerlined']);self.assertNotIn('error',result)
            self.assertNotIn('error_category',result)


@unittest.skipUnless(os.environ.get('RIDE_REAL_SCREENSHOT'), '需显式本地原图路径，图片不入 Git')
class RealScreenshotRegression(unittest.TestCase):
    def test_real_crop_band_and_preserved_downward_structure(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)
            # 用户媒体后缀 PNG 实为 TIFF；仅本地无损规范化，不放宽产品输入格式。
            with Image.open(os.environ['RIDE_REAL_SCREENSHOT']) as im:
                self.assertEqual(im.size,(1260,2720));im.convert('RGB').save(p/'original.png')
            r=extract(p/'original.png',p/'extraction','#6A778B',[235,1200,1010,1420])
            m=np.array(Image.open(p/'extraction/mask.png'))>0
            self.assertGreater(r['mask_cleanup']['open_added_pixels'],5000)
            # 中央三个非箭头截线内部应实心；右侧可见向下结构不得被剪枝。
            for x,y in [(200,140),(400,153),(600,166)]:self.assertTrue(m[y-3:y+4,x].all())
            self.assertTrue(m[187,559])
            c=process(p/'extraction/mask.png',p/'centerline')
            self.assertEqual(c['component_count'],1)
            self.assertEqual(c['cycle_rank'],0)
            # 图标遮挡下不伪造两端；保留真实可见支叉，端点并非自动起终点。
            self.assertGreaterEqual(len(c['endpoints_xy']),3)

if __name__=='__main__':unittest.main()
