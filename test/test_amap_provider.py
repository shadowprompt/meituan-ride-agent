"""Synthetic inputs and mock-only transports; no live AMap/model calls."""
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from PIL import Image
import amap_fetch as amap
import ride_agent as agent
import ride_web as web
from map_fetch import MapFetchError

KEY = 'synthetic-server-key-only'
ACTION = {'action':'query_map', 'area_name':'合成市合成区', 'names':['合成路'], 'evidence':'合成文字，不是真实图文'}


def raw(locations=('116.4,39.9',)):
    return json.dumps({'status':'1', 'geocodes':[{'location':p, 'formatted_address':KEY} for p in locations]}).encode()


class Planner:
    semantic_review = True
    def __init__(self, action=ACTION): self.action = action
    def decide(self, *args): return self.action


class AMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)/'task'
        image = Path(self.temp.name)/'synthetic.png'
        Image.new('RGB', (30,20), 'white').save(image)
        self.state = agent.start(image, self.folder, True, 'corridor')
        agent.select_map_provider(self.state, self.folder, 'amap')

    def test_authorization_before_key_and_no_fallback(self):
        with patch.dict('os.environ', {}, clear=True), patch('amap_fetch._request') as net, patch('ride_agent.fetch_named_roads') as osm:
            with self.assertRaises(MapFetchError) as error:
                amap.fetch_location_clues('合成区', ['合成路'])
            self.assertEqual(error.exception.code, 'consent_required')
            state = agent.run(self.state, self.folder, Planner(), consent_map=False)
            self.assertNotIn('map_query', state)
            self.assertFalse(net.called)
            state = agent.run(state, self.folder, Planner(), consent_map=True)
            self.assertEqual(state['map_query_diagnostic']['reason_code'], 'amap_key_missing')
            self.assertIn('AMAP_API_KEY', state['question'])
            self.assertFalse(net.called)
            self.assertFalse(osm.called)

    def test_gcj02_artifact_and_direct_summary_not_corridor(self):
        with patch.dict('os.environ', {'AMAP_API_KEY':KEY}), patch('amap_fetch._request', return_value=raw()) as net, patch('ride_agent.fetch_named_roads') as osm:
            state = agent.run(self.state, self.folder, Planner(), consent_map=True)
        self.assertEqual(net.call_count, 1)
        self.assertFalse(osm.called)
        self.assertFalse((self.folder/'map.osm').exists())
        result = state['location_clues']
        self.assertEqual(result['coordinate_system'], 'GCJ02')
        self.assertFalse(result['supports_corridor'])
        self.assertEqual(result['clues'][0]['candidates'][0]['location'], [116.4,39.9])
        self.assertFalse(state['corridor_complete'])
        self.assertFalse(state['original_gps'])
        self.assertTrue(web.artifact_current(state, 'amap-location-clues.json'))
        for path in self.folder.glob('*.json'):
            self.assertNotIn(KEY, path.read_text())
        start = web.PAGE.index('function resultSummary(')
        end = web.PAGE.index('\nfunction ', start+10)
        script = web.PAGE[start:end]+'\nconsole.log(resultSummary('+json.dumps(state)+'));'
        summary = subprocess.run(['node','-e',script], capture_output=True,text=True,check=True).stdout
        for text in ('116.4,39.9','GCJ-02','不是WGS84','合成路'):
            self.assertIn(text, summary)

    def test_service_errors_are_fixed_and_no_retry(self):
        for infocode, reason in [('10001','amap_credentials_rejected'), ('10003','amap_quota'), ('999','amap_service_error')]:
            with self.subTest(infocode=infocode), patch.dict('os.environ', {'AMAP_API_KEY':KEY}), patch('amap_fetch._request', return_value=json.dumps({'status':'0','info':'https://evil/?key='+KEY,'infocode':infocode}).encode()) as net:
                with self.assertRaises(MapFetchError) as error:
                    amap.fetch_location_clues('合成区',['合成路'],consent_download=True)
                self.assertEqual(error.exception.code,reason)
                self.assertNotIn(KEY, str(error.exception)+str(error.exception.diagnostic()))
                self.assertEqual(net.call_count,1)

    def test_input_response_and_call_budgets(self):
        with patch.dict('os.environ', {'AMAP_API_KEY':KEY}), patch('amap_fetch._request', return_value=raw()) as net:
            for area,names in [('', ['路']), ('区',['a']*2), ('区',['a','b','c','d']), ('区',['x\n'])]:
                with self.assertRaises(MapFetchError):
                    amap.fetch_location_clues(area,names,consent_download=True)
            self.assertFalse(net.called)
            amap.fetch_location_clues('合成区',['a','b','c'],consent_download=True)
            self.assertEqual(net.call_count,3)
            self.assertEqual(len({call.args[2] for call in net.call_args_list}),1)
        for payload in (raw(('NaN,30',)), raw(('181,30',)), b'[]', b'bad', raw(['1,2']*11), b'x'*(amap.MAX_RESPONSE_BYTES+1)):
            with self.assertRaises(MapFetchError): amap._candidates(payload)
        self.assertEqual(amap._candidates(raw(())),[])
        with patch.dict('os.environ', {'AMAP_API_KEY':KEY}), patch('amap_fetch._request',return_value=raw(('1,2','3,4'))):
            result=amap.fetch_location_clues('区',['路'],consent_download=True)
            self.assertEqual(result['clues'][0]['status'],'ambiguous')

    def test_transport_fixed_endpoint_redaction_and_no_redirect_retry(self):
        for status, reason in [(302,'redirect_refused'), (429,'http_429'), (503,'http_error')]:
            conn=Mock(); conn.getresponse.return_value.status=status
            with self.subTest(status=status), patch('amap_fetch.http.client.HTTPSConnection',return_value=conn) as factory:
                with self.assertRaises(MapFetchError) as error:
                    amap._request({'address':'合成区合成路'},KEY,time.monotonic()+30)
                self.assertEqual(error.exception.code,reason)
                self.assertNotIn(KEY,str(error.exception)+str(error.exception.diagnostic()))
                self.assertEqual(factory.call_args.args, ('restapi.amap.com',))
                self.assertEqual(conn.request.call_count,1)
                self.assertTrue(conn.request.call_args.args[1].startswith('/v3/geocode/geo?'))
                conn.close.assert_called_once()
        for exc,reason in [(OSError('https://bad/?key='+KEY),'network_error'),(socket.timeout(KEY),'timeout')]:
            with patch('amap_fetch.http.client.HTTPSConnection',side_effect=exc):
                with self.assertRaises(MapFetchError) as error: amap._request({},KEY,time.monotonic()+30)
                self.assertEqual(error.exception.code,reason)
                self.assertNotIn(KEY,str(error.exception))
        conn=Mock(); r=conn.getresponse.return_value; r.status=200; r.getheader.side_effect=lambda name, default=None: 'identity' if name=='Content-Encoding' else None
        r.read1.side_effect=io.BytesIO(raw()).read
        with patch('amap_fetch.http.client.HTTPSConnection',return_value=conn):
            self.assertEqual(amap._request({},KEY,time.monotonic()+30),raw())
        r.getheader.side_effect=lambda name, default=None: 'identity' if name=='Content-Encoding' else str(amap.MAX_RESPONSE_BYTES+1)
        with patch('amap_fetch.http.client.HTTPSConnection',return_value=conn):
            with self.assertRaises(MapFetchError) as error: amap._request({},KEY,time.monotonic()+30)
            self.assertEqual(error.exception.code,'response_limit')

    def test_switch_invalidates_and_archives_not_reuses(self):
        agent.select_map_provider(self.state,self.folder,'osm')
        (self.folder/'map.osm').write_text('synthetic old map')
        self.state.update(corridor_complete=True,corridor_inspection={'old':True},map_query={'old':True},messages=[{'role':'user','content':'old node IDs'}],map_query_diagnostic={'old':True})
        agent.select_map_provider(self.state,self.folder,'amap')
        self.assertFalse((self.folder/'map.osm').exists())
        self.assertEqual(len(list(self.folder.glob('map-audit-*.osm'))),1)
        self.assertFalse(self.state['corridor_complete'])
        self.assertNotIn('map_query',self.state)
        self.assertEqual(self.state['messages'],[])
        self.state['location_clues']={'old':True}
        agent.select_map_provider(self.state,self.folder,'osm')
        self.assertNotIn('location_clues',self.state)
        self.assertFalse((self.folder/'map.osm').exists())
        with self.assertRaises(ValueError): agent.select_map_provider(self.state,self.folder,'https://evil')

    def test_amap_cannot_call_osm_tools(self):
        self.state['corridor_complete'] = True  # stale metadata must not bypass provider gate
        self.assertEqual(agent.delivery_requirements(self.state,self.folder)['reason'],'amap_location_clues_only')
        self.state['corridor_complete'] = False
        for action in [{'action':'fetch_map'},{'action':'inspect_roads','names':['路']},{'action':'build_corridor','hypothesis':{}},{'action':'match'}]:
            with self.subTest(action=action),patch('ride_agent.fetch_roads') as net,patch('ride_agent.road_corridor.inspect_roads') as inspect:
                state=agent.run(self.state,self.folder,Planner(action),consent_map=True)
                self.assertEqual(state['status'],'error')
                self.assertFalse(net.called or inspect.called)

    def test_persisted_model_echo_and_url_diagnostics_are_redacted(self):
        with patch.dict('os.environ', {'AMAP_API_KEY':KEY}):
            action=dict(ACTION,evidence='https://restapi.amap.com/v3/geocode/geo?key='+KEY+'&address=x key echo '+KEY)
            agent.map_query_diagnostic(self.state,self.folder,action,'started')
            self.state['messages']=[{'role':'assistant','content':KEY+' https://x/?key=other-secret&foo=1'}]
            agent.save(self.state,self.folder)
            for path in self.folder.glob('*.json'):
                text=path.read_text()
                self.assertNotIn(KEY,text)
                self.assertNotIn('other-secret',text)



class AMapWebTests(unittest.TestCase):
    from test_ride_web import WebTests as _Base
    setUp = _Base.setUp
    tearDown = _Base.tearDown
    request = _Base.request
    state = _Base.state
    mock_cli = _Base.mock_cli

    def test_web_rejects_keys_and_untrusted_provider(self):
        for payload in ({'map_provider':'evil'}, {'amap_key':KEY}, {'AMAP_API_KEY':KEY}, {'key':KEY}):
            code,_,body=self.request('/api/tasks',dict(image_base64='AA==',mode='demo',**payload))
            self.assertEqual(code,400)
            self.assertNotIn(KEY,body.decode())
        task=self.state(map_provider='amap')
        for payload in ({'fetch_map':True,'consent_map':True},{'osm_xml':'<osm/>'},{'map_provider':'evil'},{'key':KEY}):
            code,_,body=self.request('/api/tasks/'+task+'/resume',payload)
            self.assertEqual(code,400)
            self.assertNotIn(KEY,body.decode())

    def test_web_passes_selected_provider_and_redacts_status(self):
        task=self.state(map_provider='amap',question='https://x/?key=unknown-secret '+KEY,
                        location_clues={'coordinate_system':'GCJ02','supports_corridor':False})
        with patch.dict('os.environ',{'AMAP_API_KEY':KEY}), patch('ride_web.subprocess.run',side_effect=self.mock_cli) as cli:
            code,_,body=self.request('/api/tasks/'+task)
            self.assertEqual(code,200)
            self.assertNotIn(KEY,body.decode())
            self.assertNotIn('unknown-secret',body.decode())
            code,_,body=self.request('/api/tasks/'+task+'/resume',{'map_provider':'osm','consent_map':True})
            self.assertEqual(code,200)
            argv=cli.call_args.args[0]
            self.assertEqual(argv[argv.index('--map-provider')+1],'osm')
            self.assertIn('--consent-map',argv)
            self.assertNotIn(KEY,str(argv))

    def test_artifact_download_redacts_credential_query_parameters(self):
        task=self.state(map_provider='amap')
        path=self.root/task/'task'/'amap-location-clues.json'
        path.write_text(json.dumps({'warning':'https://x/?key=unknown-secret&x=1 '+KEY}))
        with patch.dict('os.environ', {'AMAP_API_KEY':KEY}):
            code,_,body=self.request('/artifacts/'+task+'/amap-location-clues.json')
            self.assertEqual(code,200)
            self.assertNotIn(KEY,body.decode())
            self.assertNotIn('unknown-secret',body.decode())

    def test_page_explicit_provider_independent_consent_and_rotation_notice(self):
        self.assertIn('id="mapProvider"',web.PAGE)
        for text in ('AMAP_API_KEY','撤销并轮换','GCJ-02','不支持道路走廊','queryMapConsent','uploadConsent'):
            self.assertIn(text,web.PAGE)
        self.assertNotIn('id="amapKey"',web.PAGE)
        script=web.PAGE.split('<script nonce="__TOKEN__">')[1].split('</script>')[0]
        syntax=subprocess.run(['node','--check'],input=script,text=True,capture_output=True)
        self.assertEqual(syntax.returncode,0,syntax.stderr)


if __name__ == '__main__': unittest.main()
