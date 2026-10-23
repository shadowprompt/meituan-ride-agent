"""Explicit-consent, bounded Overpass download; no retries or import-time I/O.

fetch_roads([west, south, east, north], output_path, consent_download=True)
transmits ONLY the bounding box to the fixed HTTPS endpoint. It does not upload
screenshots, logs, routes or credentials. Consent is per invocation (not stored).
The response is validated as Overpass JSON and saved atomically as OSM XML,
which is the format currently accepted by road_match._load_osm (not JSON).
Data is unverified OSM road data, not navigation guidance or original GPS.
"""
import http.client
import json
import math
import os
from pathlib import Path
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

ENDPOINT = 'https://overpass-api.de/api/interpreter'
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 10_000_000
MAX_OUTPUT_BYTES = 30_000_000
MAX_SPAN_DEGREES = 0.1
MAX_AREA_KM2 = 25.0
MAX_NODES = 100_000
MAX_WAYS = 50_000
MAX_REFERENCES = 400_000


class MapFetchError(ValueError):
    """Invalid input, remote failure, unsafe data, or output failure."""


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _bounds(bounds):
    if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or not all(_number(v) for v in bounds)):
        raise MapFetchError('Bounds must be four finite numbers [west,south,east,north]')
    west, south, east, north = bounds
    if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
        raise MapFetchError('Bounds require west < east, south < north, longitude ±180 and latitude ±85; no antimeridian')
    width, height = east - west, north - south
    # Conservative local planar upper bound (cosine nearest the equator).
    latitude = 0 if south <= 0 <= north else min(abs(south), abs(north))
    area = width * height * 111.32 ** 2 * math.cos(math.radians(latitude))
    if width > MAX_SPAN_DEGREES or height > MAX_SPAN_DEGREES or area > MAX_AREA_KM2:
        raise MapFetchError('Bounding box too large: max span 0.1 degrees and area 25 km²; request a smaller local box')
    return [float(v) for v in bounds], area


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MapFetchError('Overpass redirect refused; only the fixed HTTPS endpoint is permitted')


def _download(query):
    request = urllib.request.Request(
        ENDPOINT, data=urllib.parse.urlencode({'data': query}).encode('ascii'),
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json', 'Accept-Encoding': 'identity',
                 'User-Agent': 'meituan-ride-agent-map-fetch/1.0'}, method='POST')
    # Do not forward bounding boxes to environment-configured HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    start = time.monotonic()
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise MapFetchError(f'Overpass HTTP {response.status}; no automatic retry')
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise MapFetchError('Unexpected compressed Overpass response')
            length = response.headers.get('Content-Length')
            if length is not None:
                try:
                    length = int(length)
                except (ValueError, TypeError) as exc:
                    raise MapFetchError('Invalid Overpass Content-Length') from exc
                if length < 0 or length > MAX_RESPONSE_BYTES:
                    raise MapFetchError('Overpass response exceeds byte limit')
            chunks, size = [], 0
            while True:
                if time.monotonic() - start > TIMEOUT_SECONDS:
                    raise MapFetchError('Overpass download deadline exceeded; no automatic retry')
                chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise MapFetchError('Overpass response exceeds byte limit')
                chunks.append(chunk)
            if time.monotonic() - start > TIMEOUT_SECONDS:
                raise MapFetchError('Overpass download deadline exceeded; no automatic retry')
            return b''.join(chunks)
    except urllib.error.HTTPError as exc:
        # Deliberately exclude URLs, server body and query from error messages.
        exc.close()
        raise MapFetchError(f'Overpass HTTP {exc.code}; no automatic retry') from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError,
            http.client.HTTPException) as exc:
        raise MapFetchError('Overpass network/timeout failure; no automatic retry') from exc


def _identifier(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**63 - 1


def _reject_constant(value):
    raise ValueError('Nonfinite JSON constant')


def _convert(raw):
    try:
        data = json.loads(raw.decode('utf-8'), parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise MapFetchError('Invalid UTF-8 Overpass JSON') from exc
    if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
        raise MapFetchError('Expected Overpass object with elements list')
    if data.get('remark'):
        raise MapFetchError('Overpass reported an incomplete/error response')
    elements = data['elements']
    if len(elements) > MAX_NODES + MAX_WAYS:
        raise MapFetchError('Overpass element limit exceeded')
    nodes, ways, references = {}, {}, 0
    for item in elements:
        if not isinstance(item, dict) or not _identifier(item.get('id')):
            raise MapFetchError('Invalid Overpass element/id')
        kind, key = item.get('type'), item['id']
        if kind == 'node':
            lon, lat = item.get('lon'), item.get('lat')
            if (not _number(lon) or not _number(lat)
                    or not -180 <= lon <= 180 or not -85 <= lat <= 85):
                raise MapFetchError('Invalid OSM node coordinate')
            if key in nodes or len(nodes) >= MAX_NODES:
                raise MapFetchError('Duplicate node or node limit exceeded')
            nodes[key] = (lon, lat)
        elif kind == 'way':
            ids, tags = item.get('nodes'), item.get('tags')
            if (not isinstance(ids, list) or len(ids) < 2
                    or not all(_identifier(i) for i in ids)):
                raise MapFetchError('Invalid OSM way references')
            references += len(ids)
            if references > MAX_REFERENCES:
                raise MapFetchError('OSM reference limit exceeded')
            if (not isinstance(tags, dict) or not tags.get('highway')
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items())
                    or any(len(k) > 255 or len(v) > 4096 for k, v in tags.items())
                    or len(tags) > 256):
                raise MapFetchError('Invalid OSM highway way tags')
            if key in ways or len(ways) >= MAX_WAYS:
                raise MapFetchError('Duplicate way or way limit exceeded')
            ways[key] = (ids, tags)
        else:
            raise MapFetchError('Unexpected element type; expected highway ways and nodes only')
    for ids, _ in ways.values():
        if any(i not in nodes for i in ids):
            raise MapFetchError('Incomplete Overpass data: missing referenced node')
    root = ET.Element('osm', version='0.6', generator='meituan-ride-agent-map-fetch')
    for key, (lon, lat) in nodes.items():
        ET.SubElement(root, 'node', id=str(key), lon=str(lon), lat=str(lat))
    for key, (ids, tags) in ways.items():
        way = ET.SubElement(root, 'way', id=str(key))
        for node in ids:
            ET.SubElement(way, 'nd', ref=str(node))
        for k, v in tags.items():
            # Reject XML-illegal controls instead of producing unreadable XML.
            if any(not (c in '\t\n\r' or '\x20' <= c <= '\ud7ff' or '\ue000' <= c <= '\ufffd' or '\U00010000' <= c <= '\U0010ffff') for c in k + v):
                raise MapFetchError('Invalid XML character in OSM tag')
            ET.SubElement(way, 'tag', k=k, v=v)
    xml = ET.tostring(root, encoding='utf-8', xml_declaration=True)
    if len(xml) > MAX_OUTPUT_BYTES:
        raise MapFetchError('Converted OSM exceeds output byte limit')
    return xml, len(nodes), len(ways), references


def fetch_roads(bounds, output_path, *, consent_download=False):
    """Download one local box after explicit consent; return counts/bounds metadata.

    Raises MapFetchError without retry. Keeps existing output unchanged on any
    pre-publication failure. Uses a sibling temporary file and os.replace.
    Whole selected ways may extend beyond the bbox; no clipping or GPS inference.
    Caller must ask permission to disclose the bbox before passing consent=True.
    """
    bounds, area = _bounds(bounds)
    if consent_download is not True:
        raise MapFetchError('Explicit consent_download=True required to send bounding box to Overpass')
    try:
        target = Path(output_path)
        if not target.parent.is_dir() or target.is_dir():
            raise MapFetchError('Output must be a file in an existing directory')
    except TypeError as exc:
        raise MapFetchError('Invalid output path') from exc
    west, south, east, north = bounds
    query = (f'[out:json][timeout:20][maxsize:16777216];'
             f'way["highway"]({south:.12f},{west:.12f},{north:.12f},{east:.12f});'
             '(._;>;);out body;')
    raw = _download(query)
    xml, nodes, ways, references = _convert(raw)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.map-fetch-', suffix='.tmp', delete=False) as stream:
            temporary = stream.name
            stream.write(xml)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise MapFetchError('Could not atomically save OSM output') from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return {'bounds': bounds, 'area_km2_upper_bound': area, 'node_count': nodes,
            'way_count': ways, 'reference_count': references, 'response_bytes': len(raw),
            'output_bytes': len(xml), 'output_path': str(target), 'format': 'osm_xml',
            'endpoint': ENDPOINT, 'verified': False, 'original_gps': False,
            'status': 'needs_confirmation' if ways else 'no_roads'}
