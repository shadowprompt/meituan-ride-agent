"""Synthetic POIs/pixels only. No external transport or model."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
import amap_fetch as amap
import ride_agent as agent
import place_review
from map_fetch import MapFetchError


def poi(id='branch1',city='杭州市',**extra):
    return dict(id=id,name='合成咖啡分店',pname='浙江省',cityname=city,adname='合成区',adcode='330100',address='合成地址',type='餐饮',location='120,30',**extra)


def raw(*items):
    return json.dumps({'status':'1','pois':items}).encode()


class POITests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict('os.environ',{'AMAP_API_KEY':'synthetic-poi-key-only'});self.env.start();self.addCleanup(self.env.stop)

    def test_branches_cross_city_wrong_city_type_and_bounded_fields(self):
        with patch('amap_fetch._request',return_value=raw(poi(navi={'entr_location':'120.1,30.1'}),poi('branch2','宁波市'),poi('branch3',[]),poi('branch4',name_unused='ignored'))) as net:
            result=amap.fetch_places('合成咖啡','杭州市',consent_download=True)
        self.assertEqual(net.call_count,1)
        self.assertEqual(net.call_args.kwargs,{'endpoint_path':'/v5/place/text'})
        params=net.call_args.args[0]
        self.assertEqual(params['city_limit'],'true');self.assertEqual(params['page_size'],'10');self.assertEqual(params['page_num'],'1')
        self.assertEqual([c['region_anomaly'] for c in result['candidates']],[False,True,True,False])
        self.assertEqual(result['candidates'][0]['positions'][1]['kind'],'entrance')
        self.assertTrue(all(not c['verified'] and not c['anchor_eligible'] for c in result['candidates']))
        self.assertEqual(amap._poi_text('x'*1000), 'x'*160)
        self.assertEqual(amap._poi_text('https://bad/?key=secret'),'[redacted]')

    def test_validation_consent_and_no_retry(self):
        with patch('amap_fetch._request') as net:
            for args,kw in [(('coffee','杭州市'),{}),(('x'*81,'杭州市'),{'consent_download':True}),(('a|b','杭州市'),{'consent_download':True}),(('x','普陀区'),{'consent_download':True}),(('x','330100'),{'consent_download':True})]:
                with self.assertRaises(MapFetchError):amap.fetch_places(*args,**kw)
            net.assert_not_called()
        with patch('amap_fetch._request',side_effect=MapFetchError('safe',code='timeout')) as net:
            with self.assertRaises(MapFetchError):amap.fetch_places('合成','杭州市',consent_download=True)
            self.assertEqual(net.call_count,1)
        with patch('amap_fetch._request',return_value=raw()):
            result=amap.fetch_places('合成','330100',consent_download=True,verified_adcodes=('330100',))
            self.assertEqual(result['status'],'ambiguous')

    def test_invalid_response_and_safe_service_error(self):
        for response in [raw(*[poi(str(i)) for i in range(11)]),raw(poi(),poi()),b'{',json.dumps({'status':'0','infocode':'10001','info':'https://bad/key=secret'}).encode()]:
            with patch('amap_fetch._request',return_value=response):
                with self.assertRaises(MapFetchError) as caught:amap.fetch_places('合成','杭州市',consent_download=True)
                self.assertNotIn('secret',str(caught.exception.diagnostic()))


class LocalReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.folder=Path(self.tmp.name)
        Image.new('RGB',(30,20),'white').save(self.folder/'input.png')
        self.places={'version':'v1','image_version':place_review.image_version(self.folder),'candidates':[dict(id='branch1',region_anomaly=False),dict(id='branch2',region_anomaly=True)]}
        self.action=dict(action='review_places',version='v1',image_version=self.places['image_version'],candidate_ids=['branch1'],pixel_bbox=[1,2,10,12],evidence='synthetic visible label',ambiguous=False)

    def test_single_never_verified_cross_city_ambiguous(self):
        r=place_review.review_places(self.places,self.action,self.folder,[30,20])
        self.assertEqual(r['status'],'anchors_proposed');self.assertFalse(r['verified']);self.assertFalse(r['usable_for_registration'])
        self.assertEqual(place_review.review_places(self.places,dict(self.action,candidate_ids=['branch2']),self.folder,[30,20])['status'],'ambiguous')

    def test_stale_ids_versions_free_coordinates_and_bad_pixel_evidence(self):
        for changes in [dict(version='old'),dict(image_version='old'),dict(candidate_ids=['old']),dict(location=[120,30]),dict(pixel_bbox=[1,2,99,99])]:
            with self.assertRaises(ValueError):place_review.review_places(self.places,dict(self.action,**changes),self.folder,[30,20])

    def test_run_query_permission_gate_no_supplier_history_and_no_second_model_call(self):
        class Planner:
            semantic_review=True
            def __init__(self):self.calls=0
            def decide(self,state,folder):
                self.calls+=1
                return dict(action='search_places',keywords='合成咖啡',region='杭州市',evidence='合成图中文字')
        state=agent.start(self.folder/'input.png', self.folder/'task',True,'corridor')
        state['map_provider']='amap'
        planner=Planner()
        with patch('amap_fetch._request',return_value=raw(poi())),patch.dict('os.environ',{'AMAP_API_KEY':'synthetic-poi-key-only'}):
            agent.run(state,self.folder/'task',planner,consent_map=True)
        self.assertEqual(planner.calls,1)
        self.assertEqual(state['place_stage'],'place_review_pending')
        self.assertNotIn('branch1',json.dumps(state['messages']))
        self.assertFalse((self.folder/'task'/'amap-places.json').exists())
        # APIPlanner must refuse before any upload, even on a later invocation.
        model=object.__new__(agent.APIPlanner)
        with self.assertRaises(ValueError):model.decide(state,self.folder/'task')

    def test_no_query_consent_zero_transport(self):
        class Planner:
            semantic_review=True
            def decide(self,*args):return dict(action='search_places',keywords='合成',region='杭州市',evidence='合成图')
        state=agent.start(self.folder/'input.png',self.folder/'task',True,'corridor')
        state['map_provider']='amap'
        with patch('amap_fetch._request') as net:
            agent.run(state,self.folder/'task',Planner(),consent_map=False)
            net.assert_not_called()

    def test_poi_without_fresh_consent_never_uses_transport(self):
        class Planner:
            semantic_review=True
            def decide(self,*args):return dict(action='search_places',keywords='合成',region='杭州市',evidence='合成文字')
        # Both query actions share the invocation consent/budget gate;
        # verify POI refusal with the correct explicitly selected provider.
        state=agent.start(self.folder/'input.png',self.folder/'task',True,'corridor')
        state['map_provider']='amap'
        with patch('ride_agent.fetch_places') as net:
            agent.run(state,self.folder/'task',Planner(),budget=1,consent_map=False)
            net.assert_not_called()


class POIHTTPTests(unittest.TestCase):
    def test_local_review_endpoint_and_status_do_not_invoke_model_or_map(self):
        import uuid
        import ride_web as web
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);task_id=str(uuid.uuid4());folder=root/task_id/'task'
            image=root/'synthetic.png';Image.new('RGB',(30,20),'white').save(image)
            state=agent.start(image,folder,True,'corridor')
            state['places']={'version':'v1','image_version':place_review.image_version(folder),'candidates':[dict(id='branch1',region_anomaly=False)]}
            agent.save(state,folder)
            server=web.RideServer(0,output_root=root)
            try:
                action=dict(action='review_places',version='v1',image_version=state['places']['image_version'],candidate_ids=['branch1'],pixel_bbox=[1,2,10,12],evidence='synthetic label',ambiguous=False)
                with patch.object(server,'run_cli') as cli,patch('amap_fetch._request') as net:
                    result=server.resume(task_id,{'review_places':action,'local_only':True})
                    self.assertEqual(result['place_review']['status'],'anchors_proposed')
                    self.assertTrue(any(a['name']=='input.png' for a in result['artifacts']))
                    cli.assert_not_called();net.assert_not_called()
                    with self.assertRaises(web.WebError):server.resume(task_id,{'review_places':dict(action,location=[1,2]),'local_only':True})
            finally:server.server_close()
