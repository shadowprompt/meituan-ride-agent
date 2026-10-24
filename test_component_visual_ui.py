"""精确编号产物、重叠框反例与前端即时绘制回归；只用合成图和本地 HTTP。"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image, ImageDraw
from extract_route import extract
from ride_web import PAGE
import test_ride_web as fixtures


class PixelMappingTests(unittest.TestCase):
    def test_nested_bbox_components_have_exact_disjoint_pixels(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t);im=Image.new('RGB',(100,100),'white');d=ImageDraw.Draw(im)
            d.rectangle((10,10,89,89),outline='#00BFA5',width=3)
            d.line((30,50,70,50),fill='#00BFA5',width=3);im.save(p/'input.png')
            r=extract(p/'input.png',p/'out','#00BFA5',min_component=1)
            a=np.array(Image.open(p/'out/component-ids.png')).astype(np.uint32)
            ids=a[:,:,0]+256*a[:,:,1]+65536*a[:,:,2]
            candidates=r['component_selection']['candidates']
            self.assertEqual(len(candidates),2)
            self.assertEqual(ids[10,10],1);self.assertEqual(ids[50,50],2)
            self.assertEqual(ids[40,40],0)  # 在两个框内仍不能冒充候选像素
            for c in candidates:self.assertEqual(int((ids==c['id']).sum()),c['pixels'])
            self.assertTrue(np.array_equal(ids>0,np.array(Image.open(p/'out/candidates-mask.png'))>0))
            self.assertTrue(np.array_equal(np.array(Image.open(p/'out/source-crop.png')),np.array(im)))
            extract(p/'input.png',p/'out','#00BFA5',component_ids=[2],min_component=1)
            self.assertTrue(np.array_equal(np.array(Image.open(p/'out/mask.png'))>0,ids==2))
            self.assertTrue(np.array_equal(a,np.array(Image.open(p/'out/component-ids.png'))))

    @unittest.skipUnless(shutil.which('node'),'需要本地 Node 执行前端绘制回归')
    def test_frontend_exact_pixel_focus_and_live_mask_colors(self):
        script=PAGE.split('<script nonce="__TOKEN__">')[1].split('</script>')[0]
        harness="""
const assert=require('assert');const nodes={};
global.document={getElementById:id=>nodes[id]||(nodes[id]={}),querySelectorAll:()=>[]};
"""+script+"""
const src=new Uint8ClampedArray([100,100,100,255,100,100,100,255,100,100,100,255]);
const labels=new Uint32Array([1,2,0]);
let out=paintComponentPixels(src,labels,[1],0);
assert(out[1]>out[0]);assert(out[4]>out[5]);assert.equal(out[8],100);
let changed=paintComponentPixels(src,labels,[2],0);
assert(changed[0]>changed[1]);assert(changed[5]>changed[4]);
let focus=paintComponentPixels(src,labels,[1],2);
assert.equal(focus[0],224);assert.equal(focus[8],224);assert.equal(focus[4],out[4]);
assert.equal(src[0],100);
"""
        subprocess.run(['node','-e',harness],check=True,capture_output=True)
        self.assertNotIn('chooseComponents',PAGE)
        self.assertIn('{local_only:true,component_ids:ids}',PAGE)
        self.assertIn('box.onchange=paintComponents',PAGE)
        self.assertIn('应用选择并重新生成',PAGE)


class VisualArtifactHTTPTests(unittest.TestCase):
    setUp=fixtures.WebTests.setUp
    tearDown=fixtures.WebTests.tearDown
    request=fixtures.WebTests.request
    state=fixtures.WebTests.state

    def test_fixed_artifacts_only_and_symlink_rejected(self):
        task=self.state(extracted=True)
        out=self.root/task/'task/extraction';out.mkdir()
        for name in ('source-crop.png','component-ids.png'):
            Image.new('RGB',(2,2),'white').save(out/name)
            code,_,body=self.request('/artifacts/'+task+'/extraction/'+name)
            self.assertEqual(code,200);self.assertTrue(body.startswith(b'\x89PNG'))
        data=json.loads(self.request('/api/tasks/'+task)[2])
        self.assertTrue(all(a['current'] for a in data['artifacts']))
        for name in ('../task.json','source-crop.png/../task.json','%2e%2e/task.json'):
            self.assertNotEqual(self.request('/artifacts/'+task+'/extraction/'+name)[0],200)
        (out/'source-crop.png').unlink();(out/'source-crop.png').symlink_to(out/'component-ids.png')
        self.assertEqual(self.request('/artifacts/'+task+'/extraction/source-crop.png')[0],403)

    def test_selection_post_still_needs_csrf(self):
        task=self.state(extracted=True)
        with patch('ride_web.subprocess.run') as cli:
            code,_,_=self.request('/api/tasks/'+task+'/resume',{'local_only':True,'component_ids':[1]},headers={'X-CSRF-Token':'invalid'})
        self.assertEqual(code,403);cli.assert_not_called()


if __name__=='__main__':unittest.main()
