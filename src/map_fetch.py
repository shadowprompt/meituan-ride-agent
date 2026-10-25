"""Explicit-consent, bounded Overpass download; no retries or import-time I/O.

fetch_roads([west, south, east, north], output_path, consent_download=True)
transmits ONLY the bounding box to the fixed HTTPS endpoint. It does not upload
screenshots, logs, routes or credentials. Consent is per invocation (not stored).
The response is validated as Overpass JSON and saved atomically as OSM XML,
which is the format currently accepted by road_match._load_osm (not JSON).
Data is unverified OSM road data, not navigation guidance or original GPS.
"""
import hashlib
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
    """安全结构化错误；诊断不使用远端正文或异常字符串。"""

    def __init__(self, message, *, code='response_invalid', http_status=None):
        super().__init__(message)
        self.code = code if code in REASONS else 'response_invalid'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None

    def diagnostic(self):
        message, retry = REASONS.get(self.code, REASONS['response_invalid'])
        result = {'reason_code': self.code, 'message': message,
                  'retry_advice': retry, 'automatic_retry': False}
        if self.http_status is not None:
            result['http_status'] = self.http_status
        return result


# 固定文案独立于异常/响应正文，避免密钥、URL或服务端内容回显。
REASONS = {
    'area_ambiguous': ('行政区名称匹配多个对象，无法安全选择。', '请补充原图可见的城市/区县上下文；不会自动扩大地域。'),
    'area_not_found': ('未找到该精确行政区。', '请核对行政区全称及 OSM 收录情况；重复授权不会修正名称。'),
    'roads_not_found': ('该行政区内未找到请求的精确道路。', '请核对道路文字和所属行政区；不会模糊搜索或扩大范围。'),
    'roads_partial': ('该行政区仅找到部分请求道路，不能当作完整查询成功。', '请逐项核对道路文字及所属行政区；不会丢弃未找到的道路或扩大范围。'),
    'http_429': ('地图服务限流（HTTP 429）。', '请稍后自行决定是否重新授权；不会自动重试。'),
    'http_error': ('地图服务返回非成功 HTTP 状态。', '请稍后检查服务状态，再决定是否重新授权。'),
    'timeout': ('地图查询超过本次时间预算。', '请稍后手动重新授权，或核对是否可以缩小精确查询；不会自动重试。'),
    'network_error': ('无法完成地图网络连接。', '请检查服务器网络后再决定是否重新授权。'),
    'response_limit': ('地图响应超过安全大小或对象预算。', '请缩小精确道路查询；不会提高预算或自动重试。'),
    'response_incomplete': ('地图服务报告结果不完整，不能作为道路证据。', '请稍后手动查询或缩小精确请求；不会接受截断结果。'),
    'response_invalid': ('地图响应格式或对象校验不通过。', '请检查服务状态；未保存或采用无效地图。'),
    'invalid_input': ('地图查询输入不合法。', '请提供一个短行政区全称和 1–3 个不重复的精确道路名。'),
    'consent_required': ('本次地图查询尚未授权。', '核对待查询对象后再决定是否授权；旧授权不会继承。'),
    'output_error': ('无法安全保存本地地图文件。', '请检查任务目录及磁盘写入权限；不要盲目重复授权。'),
    'redirect_refused': ('地图服务重定向已被安全拒绝。', '仅允许固定 HTTPS 端点；不会跟随重定向。'),
}


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _bounds(bounds):
    if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
            or not all(_number(v) for v in bounds)):
        raise MapFetchError('Bounds must be four finite numbers [west,south,east,north]', code='invalid_input')
    west, south, east, north = bounds
    if not (-180 <= west < east <= 180 and -85 <= south < north <= 85):
        raise MapFetchError('Bounds require west < east, south < north, longitude ±180 and latitude ±85; no antimeridian', code='invalid_input')
    width, height = east - west, north - south
    # Conservative local planar upper bound (cosine nearest the equator).
    latitude = 0 if south <= 0 <= north else min(abs(south), abs(north))
    area = width * height * 111.32 ** 2 * math.cos(math.radians(latitude))
    if width > MAX_SPAN_DEGREES or height > MAX_SPAN_DEGREES or area > MAX_AREA_KM2:
        raise MapFetchError('Bounding box too large: max span 0.1 degrees and area 25 km²; request a smaller local box', code='invalid_input')
    return [float(v) for v in bounds], area


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MapFetchError('Overpass redirect refused; only the fixed HTTPS endpoint is permitted', code='redirect_refused')


def _download(query, *, deadline=None, max_bytes=None):
    max_bytes = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
    remaining = min(TIMEOUT_SECONDS, deadline - time.monotonic()) if deadline else TIMEOUT_SECONDS
    if remaining <= 0:
        raise MapFetchError("Map query deadline exceeded; no automatic retry", code='timeout')
    request = urllib.request.Request(
        ENDPOINT, data=urllib.parse.urlencode({'data': query}).encode('ascii'),
        headers={'Content-Type': 'application/x-www-form-urlencoded',
                 'Accept': 'application/json', 'Accept-Encoding': 'identity',
                 'User-Agent': 'meituan-ride-agent-map-fetch/1.0'}, method='POST')
    # Do not forward bounding boxes to environment-configured HTTP proxies.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    start = time.monotonic()
    try:
        with opener.open(request, timeout=remaining) as response:
            if response.status != 200:
                raise MapFetchError(f'Overpass HTTP {response.status}; no automatic retry', code='http_429' if response.status == 429 else 'http_error', http_status=response.status)
            if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
                raise MapFetchError('Unexpected compressed Overpass response')
            length = response.headers.get('Content-Length')
            if length is not None:
                try:
                    length = int(length)
                except (ValueError, TypeError) as exc:
                    raise MapFetchError('Invalid Overpass Content-Length') from exc
                if length < 0 or length > max_bytes:
                    raise MapFetchError('Overpass response exceeds byte limit', code='response_limit')
            chunks, size = [], 0
            while True:
                if time.monotonic() - start > remaining:
                    raise MapFetchError('Overpass download deadline exceeded; no automatic retry', code='timeout')
                chunk = response.read(min(65536, max_bytes + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > max_bytes:
                    raise MapFetchError('Overpass response exceeds byte limit', code='response_limit')
                chunks.append(chunk)
            if time.monotonic() - start > remaining:
                raise MapFetchError('Overpass download deadline exceeded; no automatic retry', code='timeout')
            return b''.join(chunks)
    except urllib.error.HTTPError as exc:
        # Deliberately exclude URLs, server body and query from error messages.
        exc.close()
        raise MapFetchError(f'Overpass HTTP {exc.code}; no automatic retry', code='http_429' if exc.code == 429 else 'http_error', http_status=exc.code) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise MapFetchError('Overpass timeout; no automatic retry', code='timeout') from exc
    except (urllib.error.URLError, OSError,
            http.client.HTTPException) as exc:
        raise MapFetchError('Overpass network failure; no automatic retry',
                            code='timeout' if isinstance(getattr(exc, 'reason', None), TimeoutError) else 'network_error') from exc


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
        raise MapFetchError('Overpass reported an incomplete/error response', code='response_incomplete')
    elements = data['elements']
    if len(elements) > MAX_NODES + MAX_WAYS:
        raise MapFetchError('Overpass element limit exceeded', code='response_limit')
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
                raise MapFetchError('Duplicate node or node limit exceeded', code='response_limit')
            nodes[key] = (lon, lat)
        elif kind == 'way':
            ids, tags = item.get('nodes'), item.get('tags')
            if (not isinstance(ids, list) or len(ids) < 2
                    or not all(_identifier(i) for i in ids)):
                raise MapFetchError('Invalid OSM way references')
            references += len(ids)
            if references > MAX_REFERENCES:
                raise MapFetchError('OSM reference limit exceeded', code='response_limit')
            if (not isinstance(tags, dict) or not tags.get('highway')
                    or not all(isinstance(k, str) and isinstance(v, str) for k, v in tags.items())
                    or any(len(k) > 255 or len(v) > 4096 for k, v in tags.items())
                    or len(tags) > 256):
                raise MapFetchError('Invalid OSM highway way tags')
            if key in ways or len(ways) >= MAX_WAYS:
                raise MapFetchError('Duplicate way or way limit exceeded', code='response_limit')
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
        raise MapFetchError('Converted OSM exceeds output byte limit', code='response_limit')
    return xml, len(nodes), len(ways), references


def fetch_roads(bounds, output_path, *, consent_download=False):
    """Download one local box after explicit consent; return counts/bounds metadata.

    Raises MapFetchError without retry. Keeps existing output unchanged on any
    pre-publication failure. Uses a sibling temporary file and os.replace.
    Whole selected ways may extend beyond the bbox; no clipping or GPS inference.
    Caller must ask permission to disclose the bbox before passing consent=True.
    """
    bounds, area = _bounds(bounds)
    # 地理范围也是敏感信息：同意仅针对本次调用，不能从旧任务状态继承。
    if consent_download is not True:
        raise MapFetchError('Explicit consent_download=True required to send bounding box to Overpass', code='consent_required')
    try:
        target = Path(output_path)
        if not target.parent.is_dir() or target.is_dir():
            raise MapFetchError('Output must be a file in an existing directory', code='output_error')
    except TypeError as exc:
        raise MapFetchError('Invalid output path', code='output_error') from exc
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
        raise MapFetchError('Could not atomically save OSM output', code='output_error') from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return {'bounds': bounds, 'area_km2_upper_bound': area, 'node_count': nodes,
            'way_count': ways, 'reference_count': references, 'response_bytes': len(raw),
            'output_bytes': len(xml), 'output_path': str(target), 'format': 'osm_xml',
            'endpoint': ENDPOINT, 'verified': False, 'original_gps': False,
            'status': 'needs_confirmation' if ways else 'no_roads'}


# 只允许精确行政区和道路对象查询，不接受模型URL、坐标、QL或输出路径。
MAX_QUERY_WAYS = 32
MAX_QUERY_NODES = 10000


def _exact_text(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or len(value.encode('utf-8')) > 240):
        raise MapFetchError('Expected a short exact map name', code='invalid_input')
    return value.strip()


def fetch_named_roads(area_name, names, output_path, *, consent_download=False):
    """本次授权下查询具体行政区内最多3个精确道路名；最多两次请求，无重试。"""
    if consent_download is not True:
        raise MapFetchError('Fresh explicit map query consent required', code='consent_required')
    area_name = _exact_text(area_name)
    if not isinstance(names, list) or not 1 <= len(names) <= 3:
        raise MapFetchError('Expected 1..3 exact road names', code='invalid_input')
    names = [_exact_text(n) for n in names]
    if len(set(names)) != len(names):
        raise MapFetchError('Duplicate road names', code='invalid_input')
    try:
        target = Path(output_path)
    except TypeError as exc:
        raise MapFetchError('Invalid output path', code='output_error') from exc
    if not target.parent.is_dir() or target.is_dir():
        raise MapFetchError('Output must be a file in an existing directory', code='output_error')
    quote = lambda v: json.dumps(v, ensure_ascii=False)
    deadline = time.monotonic() + TIMEOUT_SECONDS
    area_query = ('[out:json][timeout:10][maxsize:2097152];'
                  f'area["name"={quote(area_name)}]["boundary"="administrative"];out tags 3;')
    area_raw = _download(area_query, deadline=deadline, max_bytes=2000000)
    try:
        data = json.loads(area_raw.decode('utf-8'), parse_constant=_reject_constant)
        areas = data['elements']
        if not isinstance(areas, list):
            raise ValueError()
        if data.get('remark'):
            raise MapFetchError('Incomplete area response', code='response_incomplete')
        if not areas:
            raise MapFetchError('No administrative area', code='area_not_found')
        # 校验全部返回对象后再判断唯一性；非法/重复对象不是地域歧义证据。
        if any(not isinstance(a, dict) or a.get('type') != 'area'
               or not _identifier(a.get('id')) or not isinstance(a.get('tags'), dict)
               or a['tags'].get('name') != area_name
               or a['tags'].get('boundary') != 'administrative' for a in areas):
            raise ValueError()
        if len({a['id'] for a in areas}) != len(areas):
            raise ValueError()
        if len(areas) > 1:
            raise MapFetchError('Ambiguous administrative area', code='area_ambiguous')
    except MapFetchError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError, RecursionError) as exc:
        raise MapFetchError('Invalid administrative area response') from exc
    area_id = areas[0]['id']
    clauses = ''.join(f'way(area.scope)["highway"]["name"={quote(n)}];' for n in names)
    query = (f'[out:json][timeout:15][maxsize:2097152];area({area_id})->.scope;'
             f'({clauses})->.roads;.roads out body 33;node(w.roads);out skel;')
    raw = _download(query, deadline=deadline, max_bytes=2000000)
    if len(raw) > 2_000_000:
        raise MapFetchError('Named map response exceeds 2 MB', code='response_limit')
    xml, nodes, ways, references = _convert(raw)
    if ways > MAX_QUERY_WAYS or nodes > MAX_QUERY_NODES or references > 20000:
        raise MapFetchError('Named map object budget exceeded; narrow exact names', code='response_limit')
    root = ET.fromstring(xml)
    if any(not any(t.get('k') == 'name' and t.get('v') in names for t in w.findall('tag'))
           for w in root.findall('way')):
        raise MapFetchError('Unexpected road outside requested exact names')
    if not ways:
        raise MapFetchError('No exact highway objects found; clarify visible road name', code='roads_not_found')
    returned_names = {tag.get('v') for way in root.findall('way')
                      for tag in way.findall('tag') if tag.get('k') == 'name'}
    if not set(names).issubset(returned_names):
        raise MapFetchError('Only some requested exact roads found', code='roads_partial')
    # 有界查询得到完整way节点，原子发布；失败时旧地图不受影响。
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.named-map-', delete=False) as stream:
            temporary = stream.name
            stream.write(xml)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        temporary = None
    except OSError as exc:
        raise MapFetchError('Could not save named map evidence', code='output_error') from exc
    finally:
        if temporary is not None:
            os.unlink(temporary)
    return {'endpoint': ENDPOINT, 'source_urls': [ENDPOINT] +
            [f'https://www.openstreetmap.org/way/{w.get("id")}' for w in root.findall('way')],
            'area_name': area_name, 'names': names, 'area_id': area_id,
            'query_sha256': hashlib.sha256(query.encode()).hexdigest(),
            'area_response_sha256': hashlib.sha256(area_raw).hexdigest(),
            'response_sha256': hashlib.sha256(raw).hexdigest(),
            'osm_sha256': hashlib.sha256(xml).hexdigest(),
            'node_count': nodes, 'way_count': ways, 'response_bytes': len(raw),
            'verified': False, 'original_gps': False, 'status': 'needs_confirmation'}
