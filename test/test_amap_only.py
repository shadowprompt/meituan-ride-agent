"""Provider retirement/migration regressions; synthetic images and no external network."""
import itertools
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image
import ride_agent as agent
import map_fetch
import test_amap_provider as fixtures
from test_amap_provider import Planner, ACTION


class OnlyAMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        image = self.root/'synthetic.png'
        Image.new('RGB',(20,20),'white').save(image)
        self.folder = self.root/'task'
        self.state = agent.start(image,self.folder,True,'corridor')

    def test_new_task_defaults_to_research(self):
        self.assertEqual(self.state['map_provider'],'research')

    def test_all_retired_network_helpers_refuse_even_with_consent_and_mock_transport(self):
        target=self.folder/'map.osm'; target.write_text('<osm/>')
        with patch('urllib.request.build_opener') as net, patch('map_fetch._download') as transport:
            for call in (lambda:map_fetch.fetch_roads([1,2,3,4],target,consent_download=True),
                         lambda:map_fetch.fetch_named_roads('合成区',['路'],target,consent_download=True),
                         lambda:map_fetch._fetch_named_roads('合成区',['路'],target,consent_download=True)):
                with self.assertRaises(map_fetch.MapFetchError) as caught: call()
                self.assertEqual(caught.exception.code,'osm_network_disabled')
            net.assert_not_called();transport.assert_not_called()
        with patch('urllib.request.build_opener') as net:
            with self.assertRaises(map_fetch.MapFetchError): map_fetch._download('anything')
            net.assert_not_called()
        self.assertEqual(target.read_text(),'<osm/>')
        with patch('ride_agent.fetch_roads') as net:
            with self.assertRaises(ValueError):agent.download_task_map(self.state,self.folder)
            net.assert_not_called()

    def test_legacy_run_refuses_before_model_and_query(self):
        for provider in ('osm',None):
            state=dict(self.state)
            if provider:state['map_provider']=provider
            else:state.pop('map_provider')
            with patch.object(Planner,'decide') as model,patch('amap_fetch._request') as amap,patch('ride_agent.fetch_named_roads') as osm:
                with self.assertRaisesRegex(ValueError,'本次'):agent.run(state,self.folder,Planner(),consent_map=True)
                model.assert_not_called();amap.assert_not_called();osm.assert_not_called()

    def test_explicit_migration_requires_both_fresh_consents(self):
        for map_ok,upload_ok in itertools.product((False,True), repeat=2):
            if map_ok and upload_ok:
                agent.validate_map_invocation('amap',previous='osm',consent_map=True,consent_upload=True)
            else:
                with self.assertRaises(ValueError):
                    agent.validate_map_invocation('amap',previous='osm',consent_map=map_ok,consent_upload=upload_ok)
        with self.assertRaises(ValueError):agent.validate_map_invocation('osm',consent_map=True,consent_upload=True)
        agent.validate_map_invocation('osm',local_only=True)
        for provider in ('amap','osm'):
            with self.assertRaises(ValueError):agent.validate_map_invocation(provider,local_only=True,fetch_map=True)

    def test_amap_failure_never_falls_back(self):
        self.state['map_provider']='amap'
        with patch('ride_agent.fetch_location_clues',side_effect=map_fetch.MapFetchError('fixed',code='network_error')),patch('ride_agent.fetch_named_roads') as osm,patch('ride_agent.fetch_roads') as bbox:
            result=agent.run(self.state,self.folder,Planner(),consent_map=True)
            self.assertEqual(result['map_query_diagnostic']['reason_code'],'network_error')
            osm.assert_not_called();bbox.assert_not_called()


class MigrationHTTPTests(unittest.TestCase):
    setUp=fixtures.AMapWebTests.setUp
    tearDown=fixtures.AMapWebTests.tearDown
    request=fixtures.AMapWebTests.request
    state=fixtures.AMapWebTests.state
    mock_cli=fixtures.AMapWebTests.mock_cli

    def test_legacy_no_silent_migration_or_replay_and_files_preserved(self):
        task=self.state(map_provider='osm',mode='api')
        folder=self.root/task/'task'; old=folder/'task.json'; before=old.read_bytes()
        (folder/'map.osm').write_text('<osm/>')
        with patch('ride_web.subprocess.run',side_effect=self.mock_cli) as cli,patch('amap_fetch._request') as net:
            for payload in ({'consent_upload':True}, {'map_provider':'osm','consent_map':True,'consent_upload':True},
                            {'map_provider':'amap','consent_upload':True}, {'map_provider':'amap','consent_map':True},
                            {'map_provider':'amap','consent_upload':True,'consent_map':True,'fetch_map':True}):
                code,_,body=self.request('/api/tasks/'+task+'/resume',payload)
                self.assertIn(code,(403,409),body)
                self.assertEqual(old.read_bytes(),before)
                self.assertEqual((folder/'map.osm').read_text(),'<osm/>')
            cli.assert_not_called();net.assert_not_called()
            code,_,body=self.request('/api/tasks/'+task+'/resume',{'map_provider':'amap','consent_map':True,'consent_upload':True})
            self.assertEqual(code,200,body)
            argv=cli.call_args.args[0]
            for flag in ('--consent-map','--consent-upload'):self.assertIn(flag,argv)
            self.assertEqual(argv[argv.index('--map-provider')+1],'amap')
            net.assert_not_called()

    def test_create_default_amap_and_explicit_osm_refused(self):
        with patch.object(self.server,'preflight_map') as preflight,patch('ride_web.subprocess.run',side_effect=self.mock_cli) as cli:
            code,_,body=self.request('/api/tasks',{'mode':'demo','image_base64':'AA==','map_provider':'osm'})
            self.assertEqual(code,409);cli.assert_not_called();preflight.assert_not_called()
            code,_,body=self.request('/api/tasks',{'mode':'demo','image_base64':'AA=='})
            self.assertEqual(code,201,body)
            preflight.assert_called_with('research')
            argv=cli.call_args.args[0]
            self.assertEqual(argv[argv.index('--map-provider')+1],'research')

class MigrationCLITests(OnlyAMapTests):
    def test_cli_legacy_rejection_before_preflight_planner_or_task_mutation(self):
        import sys
        state=dict(self.state,map_provider='osm',mode='api')
        agent.save(state,self.folder)
        before=(self.folder/'task.json').read_bytes()
        cases=([],['--map-provider','osm','--consent-map'],['--map-provider','amap'],['--map-provider','amap','--fetch-map','--consent-map'])
        with patch('ride_agent.ConsentedPlanner') as model,patch('amap_fetch.require_configuration') as config,patch('amap_fetch._request') as net:
            for options in cases:
                with self.subTest(options=options),patch.object(sys,'argv',['ride_agent.py','resume','--task-dir',str(self.folder),'--consent-upload',*options]):
                    with self.assertRaises(SystemExit):agent.main()
                    self.assertEqual((self.folder/'task.json').read_bytes(),before)
            model.assert_not_called();config.assert_not_called();net.assert_not_called()

    def test_authorized_cli_migration_preserves_old_files_and_queries_amap_only(self):
        import sys
        from test_amap_provider import raw
        agent.save(dict(self.state,map_provider='osm',mode='api',messages=[{'role':'user','content':'old map context'}]),self.folder)
        (self.folder/'map.osm').write_text('<osm/>')
        product=self.folder/'old-product.json';product.write_text('{"unverified":true}')
        with patch.object(sys,'argv',['ride_agent.py','resume','--task-dir',str(self.folder),'--map-provider','amap','--consent-upload','--consent-map']),patch.dict('os.environ',{'AMAP_API_KEY':'synthetic-server-key-only'}),patch('ride_agent.ConsentedPlanner',side_effect=lambda:Planner()),patch('amap_fetch._request',return_value=raw()) as net,patch('ride_agent.fetch_named_roads') as osm:
            agent.main()
        state=agent.load(self.folder)
        self.assertEqual(state['map_provider'],'amap')
        self.assertEqual(state['location_clues']['coordinate_system'],'GCJ02')
        self.assertFalse((self.folder/'map.osm').exists())
        self.assertEqual(next(self.folder.glob('map-audit-*.osm')).read_text(),'<osm/>')
        self.assertTrue(list(self.folder.glob('map-history-audit-*.json')))
        self.assertEqual(product.read_text(),'{"unverified":true}')
        self.assertEqual(net.call_count,1);osm.assert_not_called()

    def test_local_osm_debug_never_calls_map_or_model_and_preserves_source(self):
        import sys
        source=self.root/'source.osm'
        source.write_text('<osm version="0.6"/>')
        with patch.object(sys,'argv',['ride_agent.py','resume','--task-dir',str(self.folder),'--map-provider','osm','--local-only','--osm-file',str(source)]),patch('ride_agent.ConsentedPlanner') as model,patch('amap_fetch._request') as amap,patch('map_fetch._download') as osm:
            agent.main()
            model.assert_not_called();amap.assert_not_called();osm.assert_not_called()
        self.assertEqual(source.read_text(),'<osm version="0.6"/>')
        self.assertEqual(agent.load(self.folder)['map_provider'],'osm')
