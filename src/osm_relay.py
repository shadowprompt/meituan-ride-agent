"""Bounded OSM evidence relay, never a general URL/QL proxy.

Only administrator environment config controls transport. No proxies, redirects,
retries, credential-bearing diagnostics, or direct OSM access. Public provenance
hashes are asserted by the trusted relay (not independently re-fetched here).
"""
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from map_fetch import MapFetchError

MAX_BYTES = 4_000_000
TIMEOUT = 25


def fail(code='response_invalid', status=None):
    raise MapFetchError('OSM relay request refused or failed', code=code, http_status=status) from None


def configuration():
    try:
        raw = os.environ.get('OSM_SERVICE_URL', '')
        token = os.environ.get('OSM_SERVICE_TOKEN', '')
        url = urlsplit(raw)
        host = url.hostname
        loopback = host == 'localhost'
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or url.username is not None or url.password is not None
                or url.query or url.fragment or url.path not in ('', '/')
                or url.scheme not in ('http', 'https')
                or (url.scheme == 'http' and not loopback)
                or any(c.isspace() or ord(c) < 32 for c in raw)):
            fail('invalid_input')
        port = url.port
        if not 32 <= len(token) <= 256 or any(not 33 <= ord(c) <= 126 for c in token):
            fail('consent_required')
        return url.scheme, host, port, token
    except MapFetchError:
        raise
    except (ValueError, TypeError):
        fail('invalid_input')


def require_configuration():
    configuration()


def configuration_status():
    try:
        require_configuration()
        return 'configured_not_verified'
    except MapFetchError:
        return 'not_configured'


def _name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 100 or not value.strip()
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF or c in '\"\\;[]{}()' for c in value)):
        fail('invalid_input')


def validate(path, request):
    if not isinstance(request, dict):
        fail('invalid_input')
    if path == '/v1/way':
        if set(request) != {'way_id'} or type(request['way_id']) is not int or not 1 <= request['way_id'] <= 999999999999999:
            fail('invalid_input')
        return
    if path != '/v1/named-roads' or set(request) != {'admin_hierarchy', 'road_names'}:
        fail('invalid_input')
    hierarchy, roads = request['admin_hierarchy'], request['road_names']
    if not isinstance(hierarchy, list) or not 2 <= len(hierarchy) <= 6:
        fail('invalid_input')
    last = 0
    for item in hierarchy:
        if not isinstance(item, dict) or set(item) != {'name', 'admin_level'}:
            fail('invalid_input')
        _name(item['name'])
        level = item['admin_level']
        if type(level) is not int or not last < level <= 10:
            fail('invalid_input')
        last = level
    if hierarchy[0]['admin_level'] != 2 or last < 6:
        fail('invalid_input')
    if not isinstance(roads, list) or not 1 <= len(roads) <= 5:
        fail('invalid_input')
    for road in roads:
        _name(road)
    if len(set(roads)) != len(roads):
        fail('invalid_input')


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            fail()
        result[key] = value
    return result


def _request(path, request):
    validate(path, request)
    body = json.dumps(request, ensure_ascii=False, allow_nan=False).encode()
    if len(body) > 8192:
        fail('invalid_input')
    scheme, host, port, token = configuration()
    conn = None
    deadline = time.monotonic() + TIMEOUT
    try:
        conn = (http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection)(host, port, timeout=TIMEOUT)
        conn.request('POST', path, body, {'Authorization': 'Bearer ' + token,
                     'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail('timeout')
        if conn.sock:
            conn.sock.settimeout(remaining)
        response = conn.getresponse()
        if 300 <= response.status < 400:
            fail('redirect_refused', response.status)
        if response.status != 200:
            fail('http_429' if response.status == 429 else 'http_error', response.status)
        if response.getheader('Content-Encoding', 'identity') != 'identity':
            fail()
        if response.getheader('Content-Type', '').split(';')[0].strip() != 'application/json':
            fail()
        length = response.getheader('Content-Length')
        if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
            fail('response_limit')
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail('timeout')
            # A Connection: close response can own the socket after conn.sock
            # is cleared by getresponse(); keep the wall-clock read budget.
            if response.fp is not None and response.fp.raw is not None:
                response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                fail('response_limit')
            chunks.append(chunk)
        if time.monotonic() > deadline:
            fail('timeout')
        return json.loads(b''.join(chunks), object_pairs_hook=_pairs,
                          parse_constant=lambda _: fail())
    except MapFetchError:
        raise
    except (TimeoutError,):
        fail('timeout')
    except (OSError, http.client.HTTPException):
        fail('network_error')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        fail()
    finally:
        if conn is not None:
            conn.close()


def _id(value):
    return type(value) is int and 0 < value < 2**63


def _coord(obj):
    lat, lon = obj.get('lat'), obj.get('lon')
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (lat, lon))
            or not -90 <= lat <= 90 or not -180 <= lon <= 180):
        fail()
    return lat, lon


def to_osm(result, request):
    """Validate projection and retain original IDs; never fabricate node IDs."""
    if (not isinstance(result, dict) or result.get('request') != request
            or result.get('coordinate_system') != 'WGS84' or result.get('verified') is not False
            or result.get('status') not in ('candidates_unverified', 'no_roads_found')):
        fail()
    sources = result.get('sources')
    expected = len(request['admin_hierarchy']) + 1 if 'admin_hierarchy' in request else 1
    if not isinstance(sources, list) or len(sources) not in (1, expected):
        fail()
    if 'admin_hierarchy' in request and len(sources) == 1:
        # Current service uses a single guarded combined hierarchy/road query.
        # Accept it only with a complete explicit chain tied to that response;
        # retain strict legacy source-count validation for older responses.
        chain = result.get('admin_chain')
        if not isinstance(chain,list) or len(chain) != len(request['admin_hierarchy']):
            fail()
        parent = None
        seen_admins = set()
        for entry, step in zip(request['admin_hierarchy'],chain):
            if not isinstance(step,dict):
                fail()
            obj = step.get('object',{})
            if not isinstance(obj,dict) or not isinstance(obj.get('tags'),dict):
                fail()
            tags = obj['tags']
            if (obj.get('type') != 'relation' or not _id(obj.get('id'))
                    or obj['id'] in seen_admins or tags.get('name') != entry['name'] or tags.get('admin_level') != str(entry['admin_level'])
                    or tags.get('boundary') != 'administrative' or step.get('parent_relation_id') != parent
                    or step.get('source_index') != 0
                    or step.get('membership') != ('global_exact' if parent is None else 'overpass_parent_area')):
                fail()
            seen_admins.add(obj['id'])
            parent = obj['id']
    for source in sources:
        expected_url = ('https://overpass-api.de/api/interpreter' if 'admin_hierarchy' in request
                        else f"https://api.openstreetmap.org/api/0.6/way/{request['way_id']}/full.json")
        if (not isinstance(source, dict) or source.get('url') != expected_url
                or any(not isinstance(source.get(k), str) or not re.fullmatch('[0-9a-f]{64}', source[k])
                       for k in ('response_sha256', 'request_sha256'))
                or not isinstance(source.get('fetched_at'), str) or len(source['fetched_at']) > 80
                or (source.get('source_timestamp') is not None and
                    (not isinstance(source['source_timestamp'], str) or len(source['source_timestamp']) > 80))):
            fail()
    objects = result.get('objects')
    if not isinstance(objects, list) or len(objects) > 5000:
        fail()
    nodes, ways, ids = {}, [], set()
    for obj in objects:
        if not isinstance(obj, dict) or obj.get('type') not in ('node', 'way') or not _id(obj.get('id')):
            fail()
        identity = (obj['type'], obj['id'])
        if identity in ids:
            fail()
        ids.add(identity)
        if obj['type'] == 'node':
            nodes[obj['id']] = _coord(obj)
        else:
            ways.append(obj)
    if len(ways) > 100 or ('way_id' in request and (len(ways) != 1 or ways[0]['id'] != request['way_id'])):
        fail()
    refs_total = 0
    for way in ways:
        refs, geometry, tags = way.get('nodes'), way.get('geometry'), way.get('tags')
        if (not isinstance(refs, list) or not 2 <= len(refs) <= 5000 or not all(_id(n) for n in refs)
                or not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 256 for k, v in tags.items())):
            fail()
        refs_total += len(refs)
        if refs_total > 5000:
            fail('response_limit')
        if 'road_names' in request and (tags.get('name') not in request['road_names'] or not tags.get('highway')):
            fail()
        if geometry is not None:
            if not isinstance(geometry, list) or len(geometry) != len(refs):
                fail()
            for ident, point in zip(refs, geometry):
                if not isinstance(point, dict):
                    fail()
                coord = _coord(point)
                if ident in nodes and nodes[ident] != coord:
                    fail()
                nodes[ident] = coord
        if not set(refs) <= nodes.keys():
            fail()
    if len(nodes) > 5000 or bool(ways) != (result['status'] == 'candidates_unverified'):
        fail()
    root = ET.Element('osm', version='0.6', generator='controlled-osm-relay')
    for ident, (lat, lon) in sorted(nodes.items()):
        ET.SubElement(root, 'node', id=str(ident), lat=str(lat), lon=str(lon))
    for way in ways:
        element = ET.SubElement(root, 'way', id=str(way['id']))
        for ident in way['nodes']:
            ET.SubElement(element, 'nd', ref=str(ident))
        for key, value in way['tags'].items():
            if key in ('name', 'highway', 'boundary', 'admin_level', 'type'):
                ET.SubElement(element, 'tag', k=key, v=value)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def query(*, admin_hierarchy=None, road_names=None, way_id=None, consent_download=False):
    if consent_download is not True:
        fail('consent_required')
    if way_id is not None:
        if admin_hierarchy is not None or road_names is not None:
            fail('invalid_input')
        path, request = '/v1/way', {'way_id': way_id}
    else:
        path, request = '/v1/named-roads', {'admin_hierarchy': admin_hierarchy, 'road_names': road_names}
    validate(path, request)
    result = _request(path, request)
    xml = to_osm(result, request)
    # Project only public contract fields; never persist arbitrary server extras.
    evidence = {k: result[k] for k in ('request', 'sources', 'coordinate_system', 'status', 'verified')}
    if result.get('admin_chain') is not None:
        evidence['admin_chain'] = [{'object':{'type':'relation','id':step['object']['id'],
            'tags':{k:step['object']['tags'][k] for k in ('name','admin_level','boundary')}},
            **{k:step[k] for k in ('parent_relation_id','membership','source_index')}} for step in result['admin_chain']] if len(result['sources']) == 1 else []
    evidence['sources'] = [{k: source.get(k) for k in ('url', 'response_sha256', 'request_sha256', 'fetched_at', 'source_timestamp')} for source in result['sources']]
    evidence.update(provider='osm_relay', original_gps=False, osm_sha256=hashlib.sha256(xml).hexdigest(),
                    attribution='© OpenStreetMap contributors https://www.openstreetmap.org/copyright')
    return xml, evidence


def save_evidence(folder, xml, evidence):
    folder = Path(folder)
    # Caller has already invalidated old current evidence. Atomic per-file writes.
    for filename, data in [('map.osm', xml), ('map-query.json', json.dumps(evidence, ensure_ascii=False).encode())]:
        target = folder / filename
        temp = target.with_suffix(target.suffix + '.tmp')
        temp.write_bytes(data)
        temp.replace(target)


def evidence_current(folder, evidence):
    try:
        return (evidence.get('provider') == 'osm_relay' and
                hashlib.sha256((Path(folder)/'map.osm').read_bytes()).hexdigest() == evidence.get('osm_sha256') and
                json.loads((Path(folder)/'map-query.json').read_bytes()) == evidence)
    except (OSError, ValueError, TypeError):
        return False
