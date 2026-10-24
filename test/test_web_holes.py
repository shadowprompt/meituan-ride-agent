"""任务内逐孔复核的真实 HTTP、审计和失效/安全回归。"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from PIL import Image
from extract_route import extract
from centerline import process
import test_ride_web as fixtures


class WebHoleTests(unittest.TestCase):
    setUp = fixtures.WebTests.setUp
    tearDown = fixtures.WebTests.tearDown
    request = fixtures.WebTests.request
    state = fixtures.WebTests.state

    def prepared(self, real=False):
        task = self.state(extracted=True, centerlined=True, mode='api')
        folder = self.root/task/'task'
        if real:
            extract(os.environ['RIDE_TURNBACK_SCREENSHOT'],folder/'extraction','#6A778B',[240,1140,1000,1580])
        else:
            mask = np.zeros((70,100),np.uint8);mask[10:60,10:90]=255
            mask[20:24,20:24]=0;mask[30:34,40:44]=0;mask[40:60,60:63]=0
            (folder/'extraction').mkdir()
            Image.new('RGB',(100,70),'gray').save(folder/'extraction/source-crop.png')
            Image.fromarray(mask).save(folder/'extraction/mask.png')
            Image.new('RGB',(100,70),'gray').save(folder/'extraction/overlay.png')
            (folder/'extraction/extraction.json').write_text('{}')
        process(folder/'extraction/mask.png',folder/'centerline')
        return task,folder

    def preview(self, task):
        code,_,body=self.request('/api/tasks/'+task+'/holes/preview',{})
        self.assertEqual(code,200,body)
        return json.loads(body)['hole_review']

    def selection(self, r, ids):
        return {k:r[k] for k in ('review_id','fingerprint')} | {'fill_hole_ids':ids,'confirm_source_reviewed':True}

    def test_default_preserves_mask_apply_exact_and_audits(self):
        task,p=self.prepared(); original=(p/'extraction/mask.png').read_bytes()
        graph=(p/'centerline/pixel-graph.json').read_bytes()
        r=self.preview(task)
        self.assertEqual((p/'extraction/mask.png').read_bytes(),original)
        base='/artifacts/'+task+'/hole-reviews/'+r['review_id']+'/'
        self.assertEqual(self.request(base+'hole-1.png')[0],200)
        self.assertEqual(self.request(base+'review.html')[0],404)
        endpoint='/api/tasks/'+task+'/holes/apply'
        for ids in ([],[True],[1,1],[99]):
            self.assertEqual(self.request(endpoint,self.selection(r,ids))[0],409)
        code,_,body=self.request(endpoint,self.selection(r,[1]));self.assertEqual(code,200,body)
        state=json.loads(body); result=state['hole_correction']
        self.assertEqual(result['added_pixels'],16);self.assertFalse(state['verified'])
        audit=p/'hole-audit'/result['audit_id']
        self.assertEqual((audit/'mask-before.png').read_bytes(),original)
        self.assertEqual((audit/'graph-before.json').read_bytes(),graph)
        self.assertEqual((audit/'graph-after.json').read_bytes(),(p/'centerline/pixel-graph.json').read_bytes())
        self.assertNotEqual(graph,(audit/'graph-after.json').read_bytes())
        self.assertEqual(self.request(endpoint,self.selection(r,[1]))[0],409)
        self.assertEqual(self.request('/artifacts/'+task+'/hole-audit/'+result['audit_id']+'/selection.json')[0],200)
        self.assertFalse(json.loads(self.request('/api/tasks/'+task)[2])['hole_review']['current'])
        a=np.array(Image.open(p/'extraction/mask.png'))
        self.assertEqual(a[50,61],0);self.assertEqual(a[31,41],0)
        self.assertFalse(json.loads((p/'task.json').read_text())['registered'])

    def test_stale_source_mask_and_replaced_preview(self):
        task,p=self.prepared();r=self.preview(task)
        self.preview(task)
        ep='/api/tasks/'+task+'/holes/apply'
        self.assertEqual(self.request(ep,self.selection(r,[1]))[0],409)
        for name in ('source-crop.png','mask.png'):
            r=self.preview(task)
            path=p/'extraction'/name
            im=Image.open(path);a=np.array(im).copy();a[0,0]=0 if name.startswith('source') else 255
            Image.fromarray(a).save(path)
            self.assertEqual(self.request(ep,self.selection(r,[1]))[0],409)
            self.assertFalse(json.loads(self.request('/api/tasks/'+task)[2])['hole_review']['current'])

    def test_guards_symlink_whitelist_and_no_external_calls(self):
        task,p=self.prepared();ep='/api/tasks/'+task+'/holes/preview'
        with patch('ride_web.subprocess.run') as cli:
            for h in ({'X-CSRF-Token':'bad'},{'Host':'evil.test'},{'Origin':'https://evil.test'}):
                self.assertEqual(self.request(ep,{},headers=h)[0],403)
            self.assertEqual(self.request(ep,{'output_dir':'/tmp/foo'})[0],400)
            r=self.preview(task)
            cli.assert_not_called()
        (p/'extraction/source-crop.png').unlink()
        (p/'extraction/source-crop.png').symlink_to(p/'extraction/overlay.png')
        self.assertEqual(self.request(ep,{})[0],403)
        base='/artifacts/'+task+'/hole-reviews/'+r['review_id']+'/'
        (p/'hole-reviews'/r['review_id']/'hole-1.png').unlink()
        (p/'hole-reviews'/r['review_id']/'hole-1.png').symlink_to(p/'extraction/mask.png')
        self.assertEqual(self.request(base+'hole-1.png')[0],403)
        for name in ('hole-129.png','source.png','../task.json'):
            self.assertEqual(self.request(base+name)[0],404)

    def test_concurrent_duplicate_apply_is_serialized(self):
        import threading
        task,p=self.prepared();r=self.preview(task);results=[]
        def call():
            results.append(self.request('/api/tasks/'+task+'/holes/apply',self.selection(r,[1]))[0])
        threads=[threading.Thread(target=call) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(sorted(results),[200,409])
        self.assertEqual(len(list((p/'hole-audit').iterdir())),1)

    def test_invalidation_and_preview_limits(self):
        task,p=self.prepared();r=self.preview(task)
        data=json.loads((p/'task.json').read_text());data['extracted']=False
        (p/'task.json').write_text(json.dumps(data))
        self.assertEqual(self.request('/api/tasks/'+task+'/holes/apply',self.selection(r,[1]))[0],409)
        self.assertEqual(self.request('/api/tasks/'+task+'/holes/preview',{})[0],409)
        data.update(extracted=True,hole_review_count=32);(p/'task.json').write_text(json.dumps(data))
        self.assertEqual(self.request('/api/tasks/'+task+'/holes/preview',{})[0],409)

    @unittest.skipUnless(os.environ.get('RIDE_TURNBACK_SCREENSHOT'),'需本地折返原图')
    def test_real_2_4_http_correction(self):
        task,p=self.prepared(real=True);r=self.preview(task)
        self.assertEqual([h['area'] for h in r['holes']],[61,197,198,103,2,2,5])
        before=np.array(Image.open(p/'extraction/mask.png'))>0
        code,_,body=self.request('/api/tasks/'+task+'/holes/apply',self.selection(r,[2,4]))
        self.assertEqual(code,200,body);s=json.loads(body);c=s['hole_correction']
        self.assertEqual(c['added_pixels'],300)
        self.assertEqual(c['before']['cycle_rank'],7);self.assertEqual(c['after']['cycle_rank'],5)
        self.assertEqual(c['after']['component_count'],2)
        a=np.array(Image.open(p/'extraction/mask.png'))>0
        self.assertTrue(a[before].all())
        for x,y in [(714,40),(717,70),(642,270),(638,280),(639,280)]:self.assertFalse(a[y,x])
        self.assertFalse(s['verified']);self.assertIn('GPS',s['summary'])


if __name__=='__main__':unittest.main()
