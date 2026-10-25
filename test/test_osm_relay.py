"""Synthetic IDs/coordinates only; real loopback service, mock upstream, no keys."""
import copy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from PIL import Image
import osm_relay as relay
import ride_agent as agent
from map_fetch import MapFetchError

TOKEN = 'synthetic-relay-test-token-not-real-12345'
HIERARCHY = [{'name':'合成国','admin_level':2}, {'name':'合成市','admin_level':6}]
ACTION = {'action':'query_map','admin_hierarchy':HIERARCHY,'names':['合成路'], 'evidence':'合成夹具国家与市区文字，层级未验证'}
WAY = {'type':'way','id':42,'nodes':[11,12,13], 'geometry':[{'lat':30.,'lon':120.},{'lat':30.,'lon':120.001},{'lat':30.,'lon':120.002}], 'tags':{'name':'合成路','highway':'residential'}}


def result():
    return {'request':{'admin_hierarchy':HIERARCHY,'road_names':['合成路']}, 'objects':[copy.deepcopy(WAY)],
            'sources':[{'url':'https://overpass-api.de/api/interpreter','response_sha256':'a'*64,'request_sha256':'b'*64,'fetched_at':'2026-01-01T00:00:00Z','source_timestamp':None} for _ in range(3)],
            'coordinate_system':'WGS84','verified':False,'status':'candidates_unverified'}


class RelayTests(unittest.TestCase):
    def test_configuration_policy_and_redaction(self):
        for url in ('https://relay.example', 'http://localhost:8765', 'http://127.0.0.1:1', 'http://[::1]:8765'):
            with patch.dict(os.environ, {'OSM_SERVICE_URL':url, 'OSM_SERVICE_TOKEN':TOKEN}):
                relay.require_configuration()
                self.assertEqual(relay.configuration_status(), 'configured_not_verified')
                self.assertNotIn(TOKEN, agent.redact_credentials(TOKEN))
        for url in ('', 'http://example.com','http://127.0.0.1.evil','https://user:pass@host','https://host/path','https://host?token=x','https://host#x','ftp://host','http://0.0.0.0'):
            with patch.dict(os.environ, {'OSM_SERVICE_URL':url, 'OSM_SERVICE_TOKEN':TOKEN}):
                with self.assertRaises(MapFetchError) as caught: relay.require_configuration()
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_strict_requests_and_no_consent_no_transport(self):
        request = result()['request']
        relay.validate('/v1/named-roads', request)
        relay.validate('/v1/way', {'way_id':42})
        bads = [dict(request, url='https://evil'), dict(request,admin_hierarchy=HIERARCHY[1:]),
                dict(request, admin_hierarchy=[{'name':'国','admin_level':2},{'name':'省','admin_level':4}]),
                dict(request, road_names=['x;out;']), dict(request,road_names=['x','x'])]
        for bad in bads:
            with self.assertRaises(MapFetchError): relay.validate('/v1/named-roads',bad)
        for way in (True, 0, -1, '42', 10**15):
            with self.assertRaises(MapFetchError): relay.validate('/v1/way',{'way_id':way})
        with patch('osm_relay._request') as net:
            with self.assertRaises(MapFetchError): relay.query(admin_hierarchy=HIERARCHY,road_names=['合成路'])
            net.assert_not_called()

    def test_ids_geometry_hash_and_malformed_sources(self):
        with patch('osm_relay._request',return_value=result()):
            xml,evidence=relay.query(admin_hierarchy=HIERARCHY,road_names=['合成路'],consent_download=True)
        self.assertIn(b'id="42"',xml); self.assertIn(b'ref="11"',xml)
        self.assertEqual(evidence['osm_sha256'],hashlib.sha256(xml).hexdigest())
        for mutate in (lambda r:r['sources'][0].update(response_sha256='bad'),
                       lambda r:r.update(coordinate_system='GCJ02'),
                       lambda r:r['objects'][0].update(nodes=[11,12]),
                       lambda r:r['objects'][0]['geometry'][0].update(lat=float('nan')),
                       lambda r:r['objects'].append(copy.deepcopy(WAY)),
                       lambda r:r['sources'][0].update(url='https://evil')):
            r=result();mutate(r)
            with self.assertRaises(MapFetchError):relay.to_osm(r,result()['request'])

    def test_transport_does_not_redirect_retry_or_echo(self):
        class Response:
            status=302
            def getheader(self,*args):return 'https://evil/'+TOKEN
        with patch.dict(os.environ,{'OSM_SERVICE_URL':'https://relay.example','OSM_SERVICE_TOKEN':TOKEN}),patch('osm_relay.http.client.HTTPSConnection') as connection:
            connection.return_value.getresponse.return_value=Response()
            with self.assertRaises(MapFetchError) as caught:relay.query(way_id=42,consent_download=True)
            self.assertEqual(caught.exception.code,'redirect_refused')
            connection.assert_called_once();connection.return_value.request.assert_called_once()
            self.assertNotIn(TOKEN,str(caught.exception))
            connection.return_value.getresponse.side_effect=OSError(TOKEN)
            with self.assertRaises(MapFetchError) as caught:relay.query(way_id=42,consent_download=True)
            self.assertNotIn(TOKEN,str(caught.exception))

    def test_no_configuration_web_and_cli_stop_before_upload_or_mutation(self):
        import ride_web
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'OSM_SERVICE_URL':'', 'OSM_SERVICE_TOKEN':''}):
            root=Path(tmp); server=ride_web.RideServer(0,output_root=root/'web')
            try:
                with patch.object(server,'run_cli') as cli, patch('ride_agent.APIPlanner') as model:
                    with self.assertRaises(ride_web.WebError):
                        server.create({'mode':'api','consent_upload':True,'image_base64':'AA=='})
                    cli.assert_not_called();model.assert_not_called()
                    self.assertEqual(list(server.root.iterdir()),[])
                target=root/'new-task'
                with patch.object(sys,'argv',['ride_agent.py','start','--image',str(root/'missing.png'),'--task-dir',str(target),'--consent-upload']),patch('ride_agent.ConsentedPlanner') as model:
                    with self.assertRaises(SystemExit):agent.main()
                    model.assert_not_called();self.assertFalse(target.exists())
            finally:server.server_close()

    def test_transport_limits_errors_and_credential_safe_projection(self):
        from unittest.mock import Mock
        config={'OSM_SERVICE_URL':'https://relay.example','OSM_SERVICE_TOKEN':TOKEN}
        for status,code in ((401,'http_error'),(429,'http_429'),(503,'http_error')):
            connection=Mock();connection.getresponse.return_value.status=status
            with patch.dict(os.environ,config),patch('osm_relay.http.client.HTTPSConnection',return_value=connection):
                with self.assertRaises(MapFetchError) as caught:relay.query(way_id=42,consent_download=True)
                self.assertEqual(caught.exception.code,code);connection.request.assert_called_once()
        for length,encoding in (('4000001','identity'),('bad','identity'),('1','gzip')):
            connection=Mock();response=connection.getresponse.return_value;response.status=200
            response.getheader.side_effect=lambda key,default=None: {'Content-Type':'application/json','Content-Length':length,'Content-Encoding':encoding}.get(key,default)
            with patch.dict(os.environ,config),patch('osm_relay.http.client.HTTPSConnection',return_value=connection):
                with self.assertRaises(MapFetchError):relay.query(way_id=42,consent_download=True)
                response.read1.assert_not_called()
        r=result();r['sources'][0]['private_secret']=TOKEN;r['extra_secret']=TOKEN
        with patch('osm_relay._request',return_value=r):
            xml,evidence=relay.query(admin_hierarchy=HIERARCHY,road_names=['合成路'],consent_download=True)
        self.assertNotIn(TOKEN,json.dumps(evidence))
        with self.assertRaises(MapFetchError):relay._pairs([('a',1),('a',2)])

    def test_combined_source_requires_complete_bound_admin_chain(self):
        combined = result()
        combined['sources'] = combined['sources'][:1]
        with self.assertRaises(MapFetchError):
            relay.to_osm(combined, combined['request'])
        parent = None
        combined['admin_chain'] = []
        for entry in HIERARCHY:
            ident = 100 + entry['admin_level']
            combined['admin_chain'].append({'object':{'type':'relation','id':ident,
                'tags':{'name':entry['name'],'admin_level':str(entry['admin_level']),'boundary':'administrative'}},
                'parent_relation_id':parent,'membership':'global_exact' if parent is None else 'overpass_parent_area',
                'source_index':0})
            parent = ident
        self.assertIn(b'<osm',relay.to_osm(combined,combined['request']))
        for change in ({'parent_relation_id':999},{'source_index':1},{'membership':'guessed'}):
            bad=copy.deepcopy(combined);bad['admin_chain'][1].update(change)
            with self.assertRaises(MapFetchError):
                relay.to_osm(bad,bad['request'])

    def test_closed_connection_response_keeps_read_deadline(self):
        from unittest.mock import Mock
        connection = Mock()
        connection.sock = None  # HTTPResponse owns the Connection: close socket
        response = connection.getresponse.return_value
        response.status = 200
        response.getheader.side_effect = lambda key, default=None: {'Content-Type':'application/json'}.get(key, default)
        response.read1.side_effect = [json.dumps(result()).encode(), b'']
        with patch.dict(os.environ, {'OSM_SERVICE_URL':'https://relay.example', 'OSM_SERVICE_TOKEN':TOKEN}), patch('osm_relay.http.client.HTTPSConnection', return_value=connection):
            relay.query(admin_hierarchy=HIERARCHY, road_names=['合成路'], consent_download=True)
        timeout = response.fp.raw._sock.settimeout.call_args.args[0]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, relay.TIMEOUT)
        connection.close.assert_called_once()

    def test_web_hash_invalidation_is_read_only_and_hides_current_summary(self):
        import uuid
        import ride_web
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); task_id = str(uuid.uuid4()); folder = root/task_id/'task'
            image = root/'synthetic.png'; Image.new('RGB',(20,20),'white').save(image)
            state = agent.start(image, folder, True, 'corridor')
            with patch('osm_relay._request', return_value=result()):
                xml, evidence = relay.query(admin_hierarchy=HIERARCHY, road_names=['合成路'], consent_download=True)
            relay.save_evidence(folder, xml, evidence)
            state.update(map_query=evidence, corridor_complete=True, summary='stale corridor', artifacts=['old'])
            agent.save(state, folder)
            before = (folder/'task.json').read_bytes()
            (folder/'map.osm').write_bytes(xml+b' ')
            server = ride_web.RideServer(0, output_root=root)
            try:
                status = server.status(task_id)
                self.assertFalse(status['map_available'])
                self.assertFalse(status['corridor_complete'])
                self.assertNotIn('summary', status)
                self.assertEqual(before, (folder/'task.json').read_bytes())
            finally:
                server.server_close()

    def test_defaults_switch_consent_and_hash_invalidation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);image=root/'synthetic.png';Image.new('RGB',(30,20),'white').save(image)
            folder=root/'task';state=agent.start(image,folder,True,'corridor')
            self.assertEqual(state['map_provider'],'osm_relay')
            for old in ('amap','osm'):
                with self.assertRaises(ValueError):agent.validate_map_invocation('osm_relay',previous=old,consent_map=True)
                agent.validate_map_invocation('osm_relay',previous=old,consent_map=True,consent_upload=True)
            with patch('osm_relay._request',return_value=result()):
                xml,evidence=relay.query(admin_hierarchy=HIERARCHY,road_names=['合成路'],consent_download=True)
            relay.save_evidence(folder,xml,evidence);state.update(map_query=evidence,corridor_complete=True,corridor_artifacts=['old'])
            self.assertTrue(relay.evidence_current(folder,evidence))
            (folder/'map.osm').write_bytes(xml+b' ')
            state=agent.run(state,folder,agent.LocalPlanner())
            self.assertFalse(state['corridor_complete']);self.assertNotIn('map_query',state)
            self.assertNotIn('corridor_artifacts',state)
            agent.select_map_provider(state,folder,'amap')
            self.assertFalse((folder/'map.osm').exists());self.assertTrue(list(folder.glob('map-audit-*.osm')))


# The independent service is deliberately not a runtime dependency. This optional
# integration imports its actual implementation when the sibling checkout exists.
SERVICE = Path(__file__).resolve().parents[2]/'osm-evidence-service/osm_service/server.py'


@unittest.skipUnless(SERVICE.is_file(), 'independent service checkout unavailable')
class LoopbackIntegration(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('relay_test_service', SERVICE)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
        self.calls=[]
        def upstream(url, body, budget):
            self.calls.append((url,body))
            if body is None:
                objects=[dict(type='node',id=n,**p) for n,p in zip(WAY['nodes'],WAY['geometry'])]+[{k:v for k,v in WAY.items() if k!='geometry'}]
            else:
                from urllib.parse import parse_qs
                q=parse_qs(body.decode())['data'][0]
                if 'rel' in q:
                    import re
                    # Service now emits one guarded combined hierarchy+roads program.
                    # Return every requested admin and the road in the same response,
                    # rather than simulating the retired three-upstream-call protocol.
                    names=re.findall(r'\["name"="([^"]+)"\]',q)
                    levels=re.findall(r'\["admin_level"="([^"]+)"\]',q)
                    objects=[{'type':'relation','id':100+int(level),'tags':{'name':name,'admin_level':level,'boundary':'administrative'}}
                             for name,level in zip(names,levels)] + [copy.deepcopy(WAY)]
                else:objects=[copy.deepcopy(WAY)]
            return objects,{'url':url,'response_sha256':hashlib.sha256(json.dumps(objects).encode()).hexdigest(), 'request_sha256':hashlib.sha256(body or url.encode()).hexdigest(),'fetched_at':'2026-01-01T00:00:00Z','source_timestamp':None}
        self.server=module.Server(('127.0.0.1',0),TOKEN,module.Evidence(fetch=upstream))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.env=patch.dict(os.environ,{'OSM_SERVICE_URL':f'http://127.0.0.1:{self.server.server_port}','OSM_SERVICE_TOKEN':TOKEN});self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_real_http_query_inspect_build_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); image=root/'synthetic.png';Image.new('RGB',(30,20),'white').save(image)
            folder=root/'task';state=agent.start(image,folder,True,'corridor')
            hypothesis={'schema_version':1,'coordinate_system':'WGS84','source':{'kind':'model','evidence':'合成顺序，不是真实截图'},'legs':[{'road':{'name':'合成路'},'via':[{'node_id':11},{'node_id':13}]}]}
            actions=iter([ACTION,{'action':'inspect_roads','names':['合成路']},{'action':'build_corridor','hypothesis':hypothesis},{'action':'deliver','summary':'合成道路走廊，未验证，不可导航'}])
            class Planner:
                semantic_review=True
                def decide(self,*args):return next(actions)
            with patch('map_fetch._download',side_effect=AssertionError('direct OSM forbidden')),patch('amap_fetch._request',side_effect=AssertionError('fallback forbidden')):
                state=agent.run(state,folder,Planner(),consent_map=True)
            self.assertEqual(state['status'],'needs_confirmation',state.get('error'))
            self.assertTrue(state['corridor_complete']);self.assertEqual(len(self.calls),1)
            self.assertEqual(state['map_query']['coordinate_system'],'WGS84')
            self.assertFalse(state['verified']);self.assertFalse(state['original_gps'])
            self.assertNotIn(TOKEN,(folder/'task.json').read_text())
            for name in state['corridor_artifacts']:self.assertTrue((folder/name).is_file())

    def test_real_http_way_and_auth_and_no_automatic_retry(self):
        xml,evidence=relay.query(way_id=42,consent_download=True)
        self.assertIn(b'id="42"',xml);self.assertEqual(len(self.calls),1)
        with patch.dict(os.environ,{'OSM_SERVICE_TOKEN':'wrong-synthetic-token-1234567890123456'}):
            with self.assertRaises(MapFetchError) as caught:relay.query(way_id=42,consent_download=True)
            self.assertEqual(caught.exception.http_status,401)
        self.assertEqual(len(self.calls),1)

    def test_agent_budget_and_authorization(self):
        for consent in (False,True):
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);image=root/'synthetic.png';Image.new('RGB',(20,20),'white').save(image)
                folder=root/'task';state=agent.start(image,folder,True,'corridor')
                class Planner:
                    semantic_review=True
                    def decide(self,*args):return ACTION
                before=len(self.calls)
                state=agent.run(state,folder,Planner(),consent_map=consent)
                self.assertEqual(len(self.calls)-before,1 if consent else 0)
                self.assertEqual(state['status'],'waiting_for_user')


if __name__ == '__main__':unittest.main()
