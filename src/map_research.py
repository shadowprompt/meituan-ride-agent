"""Offline replay of hash-bound public single-object research, not a map download.

An administrator/CLI selects a manifest; the model can only search names or inspect
IDs within it. No URL, filesystem path, network request or generated coordinate is
accepted from the model. Missing objects remain missing, not Overpass prerequisites.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

MAX_BYTES = 4_000_000
URL = re.compile(r'https://api\.openstreetmap\.org/api/0\.6/(node|way)/([1-9][0-9]{0,14})(?:/(ways|full))?\.json\Z')


def _read(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Research evidence exceeds byte budget')
    raw = path.read_bytes()
    return raw, hashlib.sha256(raw).hexdigest()


def load(manifest, root, allow_empty=False):
    """Validate saved responses, not documentary claims that an endpoint worked."""
    root = Path(root).resolve()
    path = Path(manifest).resolve()
    raw, digest = _read(path)
    sources = json.loads(raw)['sources']
    if not isinstance(sources, list) or not (0 if allow_empty else 1) <= len(sources) <= (128 if allow_empty else 32):
        raise ValueError('Research manifest requires 1..32 snapshots')
    objects, provenance = {}, []
    for source in sources:
        match = URL.fullmatch(source['url'])
        saved = (root / source['saved_as']).resolve()
        if not match or not saved.is_relative_to(root):
            raise ValueError('Only local public OSM single-object snapshots accepted')
        body, actual = _read(saved)
        if actual != source['sha256']:
            raise ValueError('Research snapshot hash mismatch')
        elements = json.loads(body)['elements']
        if not isinstance(elements, list) or len(elements) > 10000 or any(not isinstance(e,dict) for e in elements):
            raise ValueError('Research object budget exceeded')
        requested = (match[1], int(match[2]))
        if (match[3] == 'full' and match[1] != 'way') or (match[3] == 'ways' and match[1] != 'node'):
            raise ValueError('Unsupported public object endpoint')
        if match[3] == 'ways' and any(e.get('type') != 'way' or requested[1] not in e.get('nodes',[]) for e in elements):
            raise ValueError('Membership response does not prove requested node membership')
        if match[3] != 'ways' and not any((e.get('type'), e.get('id')) == requested for e in elements):
            raise ValueError('Research response does not contain requested object')
        if match[3] == 'full':
            ways = [e for e in elements if e.get('type') == 'way']
            if len(ways) != 1 or ways[0].get('id') != requested[1]:
                raise ValueError('Full response must contain only requested way')
            refs = ways[0].get('nodes', [])
            if any(e.get('type') != 'way' and (e.get('type') != 'node' or e.get('id') not in refs) for e in elements):
                raise ValueError('Unrelated objects in full response')
        provenance.append({'url':source['url'], 'sha256':actual})
        for element in elements:
            kind, key = element.get('type'), element.get('id')
            if kind not in ('node', 'way') or type(key) is not int or key <= 0:
                raise ValueError('Invalid research object')
            tags = element.get('tags', {})
            if not isinstance(tags, dict) or any(not isinstance(k,str) or not isinstance(v,str) or len(v)>4096 for k,v in tags.items()):
                raise ValueError('Invalid object tags')
            if kind == 'node':
                lon, lat = element.get('lon'), element.get('lat')
                if any(type(v) not in (int,float) or not math.isfinite(v) for v in (lon,lat)) or not -180<=lon<=180 or not -85<=lat<=85:
                    raise ValueError('Invalid public WGS84 coordinate')
            else:
                ids = element.get('nodes')
                if not isinstance(ids,list) or len(ids)>10000 or any(type(n) is not int or n<=0 for n in ids):
                    raise ValueError('Invalid way references')
            identity = (kind,key)
            if identity in objects and objects[identity]['object'] != element:
                raise ValueError('Conflicting snapshot versions; do not silently merge')
            item = objects.setdefault(identity, {'object':element, 'sources':[]})
            item['sources'].append({'url':source['url'], 'sha256':actual})
    if len(objects)>20000:
        raise ValueError('Research total object budget exceeded')
    version = hashlib.sha256(json.dumps([digest,provenance],sort_keys=True).encode()).hexdigest()
    return {'version':version, 'manifest_sha256':digest, 'objects':objects}


def export_osm(archive, path):
    """Retain original IDs, even when member coordinates are unavailable."""
    root = ET.Element('osm',version='0.6')
    for (kind,key), item in sorted(archive['objects'].items()):
        obj = item['object']
        attrs = {'id':str(key)}
        if kind == 'node':
            attrs.update(lon=str(obj['lon']),lat=str(obj['lat']))
        el = ET.SubElement(root,kind,attrs)
        for ref in obj.get('nodes',[]):
            ET.SubElement(el,'nd',ref=str(ref))
        for k,v in obj.get('tags',{}).items():
            ET.SubElement(el,'tag',k=k,v=v)
    Path(path).write_bytes(ET.tostring(root,encoding='utf-8',xml_declaration=True))


def execute(archive, action):
    """Search hypotheses, inspect concrete objects, feed results back for revision."""
    if action.get('action') == 'search_map_evidence' and set(action)=={'action','names','evidence'}:
        names = action['names']
        if not isinstance(names,list) or not 1<=len(names)<=5 or any(not isinstance(n,str) or not 1<=len(n)<=100 for n in names):
            raise ValueError('Search requires bounded visible name hypotheses')
        hits = [item for item in archive['objects'].values() if item['object'].get('tags',{}).get('name') in names]
    elif action.get('action') == 'inspect_map_object' and set(action)=={'action','object_type','object_id','evidence'}:
        key = (action['object_type'],action['object_id'])
        if key[0] not in ('node','way') or type(key[1]) is not int or key[1]<=0:
            raise ValueError('Specific public object identity required')
        target = archive['objects'].get(key)
        hits = [] if target is None else [target]
        if target and key[0]=='node':
            hits += [item for (kind,_),item in archive['objects'].items() if kind=='way' and key[1] in item['object']['nodes']]
        if target and key[0]=='way':
            hits += [archive['objects'][('node',n)] for n in target['object']['nodes'] if ('node',n) in archive['objects']]
    else:
        raise ValueError('Research accepts bounded name search or single-object inspection only')
    if not isinstance(action['evidence'],str) or not 1<=len(action['evidence'].strip())<=2000:
        raise ValueError('Visible hypothesis evidence and uncertainty required')
    if len(hits)>256:
        raise ValueError('Narrow research query; response object budget exceeded')
    return {'tool':action['action'], 'version':archive['version'], 'status':'found' if hits else 'not_in_archive',
            'objects':hits, 'verified':False, 'original_gps':False,
            'notice':'Saved public object facts only; screenshot correspondence and traversal are unverified. Missing archive object does not mean absent road. No network was used.'}
