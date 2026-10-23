"""No live API calls: inspect outgoing current-stage previews and diagnostics."""
import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
from ride_agent import visual_feedback, APIPlanner, start, run, load
from road_match import match


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        for relative in ('extraction/overlay.png','centerline/graph-overlay.png','roads/road-overlay.png'):
            p=self.root/relative
            p.parent.mkdir(exist_ok=True)
            Image.new('RGB',(1600,800),'white').save(p)
        self.state=dict(extracted=True,centerlined=True,registered=True,
                        registration_gate_passed=True,match_attempted=True)

    def test_current_previews_bounded(self):
        content=visual_feedback(self.state,self.root)
        self.assertEqual(len(content),6)
        for item in content[1::2]:
            url=item['image_url']['url']
            self.assertTrue(url.startswith('data:image/jpeg;base64,'))
            image=Image.open(io.BytesIO(base64.b64decode(url.split(',')[1])))
            self.assertLessEqual(max(image.size),1024)

    def test_invalidated_stages_never_upload_stale_files(self):
        self.state['extracted']=False
        self.assertEqual(visual_feedback(self.state,self.root),[])
        self.state['extracted']=True
        self.state['centerlined']=False
        self.assertEqual(len(visual_feedback(self.state,self.root)),2)
        self.state['centerlined']=True
        self.state['registered']=False
        self.assertEqual(len(visual_feedback(self.state,self.root)),4)
        self.state['registered']=True
        self.state['registration_gate_passed']=False
        self.assertEqual(len(visual_feedback(self.state,self.root)),4)

    def test_optional_old_task_files_absent(self):
        (self.root/'roads/road-overlay.png').unlink()
        self.assertEqual(len(visual_feedback(self.state,self.root)),4)

    def test_model_artifact_paths_ignored(self):
        self.state['artifacts']=['/etc/passwd','https://example.com/image.png']
        self.assertEqual(len(visual_feedback(self.state,self.root)),6)

    def test_symlink_escape_refused(self):
        outside=self.root.parent/(self.root.name+'-outside.png')
        Image.new('RGB',(10,10)).save(outside)
        self.addCleanup(outside.unlink)
        target=self.root/'extraction/overlay.png'
        target.unlink()
        target.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'inside task'):
            visual_feedback(self.state,self.root)

    def test_oversize_refused(self):
        (self.root/'extraction/overlay.png').write_bytes(b'0'*5_000_001)
        with self.assertRaisesRegex(ValueError,'5 MB'):
            visual_feedback(self.state,self.root)

    def test_excessive_pixel_dimensions_refused(self):
        Image.new('RGB',(2001,2000)).save(self.root/'extraction/overlay.png')
        with self.assertRaisesRegex(ValueError,'pixel limit'):
            visual_feedback(self.state,self.root)

    def test_api_current_state_appended_after_history(self):
        source=self.root/'source.png'
        Image.new('RGB',(20,20)).save(source)
        folder=self.root/'task'
        state=start(source,folder,True)
        state['messages']=[{'role':'user','content':'Historical extraction succeeded, now invalidated'}]
        # A leftover image must not be sent for a false stage.
        (folder/'extraction').mkdir()
        Image.new('RGB',(20,20)).save(folder/'extraction/overlay.png')
        class Response:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):
                return json.dumps({'choices':[{'message':{'content':json.dumps({'action':'ask','question':'crop?'})}}]}).encode()
        with patch.dict(os.environ,{'RIDE_API_BASE':'https://example.com/v1','RIDE_API_KEY':'fake','RIDE_MODEL':'fake'}):
            with patch('ride_agent.urllib.request.build_opener') as opener:
                opener.return_value.open.return_value=Response()
                APIPlanner().decide(state,folder)
                body=json.loads(opener.return_value.open.call_args.args[0].data)
        latest=body['messages'][-1]['content']
        self.assertEqual(len(latest),1)
        self.assertIn('"extracted": false',latest[0]['text'])
        self.assertIn('Historical',body['messages'][-2]['content'])

    def test_failed_rerun_invalidates_prior_evidence(self):
        class Retry:
            def decide(self,*args):return {'action':'extract','crop':[0,0,20,20],'color':'#00BFA5'}
        source=self.root/'source.png'
        Image.new('RGB',(20,20)).save(source)
        folder=self.root/'task'
        state=start(source,folder,True)
        state.update(self.state)
        with patch('ride_agent.extract',side_effect=ValueError('partial write failure')):
            result=run(state,folder,Retry())
        self.assertEqual(result['status'],'error')
        persisted=load(folder)
        for flag in ('extracted','centerlined','registered','match_attempted'):
            self.assertFalse(persisted[flag])

    def test_no_match_diagnostics(self):
        segments=self.root/'segments.json'
        segments.write_text(json.dumps({'type':'FeatureCollection','features':[{'type':'Feature',
            'properties':{'coordinate_system':'WGS84'},'geometry':{'type':'LineString','coordinates':[[0,0],[.001,0]]}}]}))
        osm=self.root/'map.osm'
        osm.write_text('<osm version="0.6"/>')
        report=match(segments,osm,self.root/'result')
        self.assertEqual(report['matched_segment_indices'],[])
        self.assertEqual(report['missing_segment_indices'],[0])
        self.assertEqual(report['coverage_fraction'],0)
        self.assertFalse(report['verified'])


if __name__=='__main__':unittest.main()
