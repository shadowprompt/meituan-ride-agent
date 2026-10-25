"""Retired OSM network helpers plus shared safe map error/local parsing utilities.

All former Overpass entry points unconditionally reject without I/O, including
calls with consent. Network map queries use amap_fetch only; offline OSM parsing
remains available for explicit local debug and existing artifact compatibility.
"""
import hashlib
import http.client
import json
import math
import os
from admin_area import levels_for, resolve as resolve_admin_area
from pathlib import Path
import socket
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Historical identifier only; no OSM transport is implemented.
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

    def __init__(self, message, *, code='response_invalid', http_status=None, area_resolution=None, transport=None):
        super().__init__(message)
        self.transport = transport
        self.area_resolution = area_resolution
        self.code = code if code in REASONS else 'response_invalid'
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None

    def diagnostic(self):
        message, retry = REASONS.get(self.code, REASONS['response_invalid'])
        result = {'reason_code': self.code, 'message': message,
                  'retry_advice': retry, 'automatic_retry': False}
        if self.transport is not None:
            result['transport'] = self.transport
        if self.http_status is not None:
            result['http_status'] = self.http_status
        if self.area_resolution is not None:
            result['area_resolution'] = self.area_resolution
        return result


# 固定文案独立于异常/响应正文，避免密钥、URL或服务端内容回显。
REASONS = {
    'osm_network_disabled': ('OSM/Overpass 联网已停用；本地直连禁止；可显式迁移至受控OSM中转或高德兼容。', '请显式选择 map_provider=osm_relay（或高德兼容 amap），并重新给予本次地图查询及模型上传授权；旧任务不会自动迁移或重放。'),
    'amap_key_missing': ('服务器未配置 AMAP_API_KEY，未发送高德请求。', '请管理员在服务器私密环境配置新的高德 Web服务 Key；不要在网页或群聊粘贴密钥。已公开的 Key 应立即撤销并轮换。'),
    'amap_credentials_rejected': ('高德凭证或服务权限校验未通过。', '请管理员私下检查 Web服务 Key 与服务权限；不要公开密钥，不会自动扩大权限。'),
    'amap_quota': ('高德服务调用配额或频率受限。', '请管理员检查配额，稍后由用户决定是否重新授权；不会自动重试。'),
    'amap_service_error': ('高德未成功处理本次定位线索查询。', '请检查服务配置和查询地址；不回显远端正文，不会自动重试。'),
    'area_ambiguous': ('行政区名称匹配多个对象，无法安全选择。', '请补充原图可见的城市/区县上下文；不会自动扩大地域。'),
    'area_parent_required': ('行政区层级无法保守解析或父级不明确。', '请提供有图文依据的明确父/子行政区结构；不会猜选同名区县。'),
    'area_membership_unverified': ('行政区缺少可验证的父子归属证据。', '需父 relation 的 subarea 成员及父范围内匹配；不会扩大范围或猜选。'),
    'area_not_found': ('未找到该精确行政区。', '请核对行政区全称及 OSM 收录情况；重复授权不会修正名称。'),
    'roads_not_found': ('该行政区内未找到请求的精确道路。', '请核对道路文字和所属行政区；不会模糊搜索或扩大范围。'),
    'roads_partial': ('该行政区仅找到部分请求道路，不能当作完整查询成功。', '请逐项核对道路文字及所属行政区；不会丢弃未找到的道路或扩大范围。'),
    'http_429': ('地图服务限流（HTTP 429）。', '请稍后自行决定是否重新授权；不会自动重试。'),
    'http_error': ('地图服务返回非成功 HTTP 状态。', '请稍后检查服务状态，再决定是否重新授权。'),
    'timeout': ('地图查询超过本次时间预算。', '请稍后手动重新授权，或核对是否可以缩小精确查询；不会自动重试。'),
    'server_timeout': ('地图服务器报告查询执行超时，结果不能作为证据。', '请稍后手动查询；不会扩大范围或自动重试。'),
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
    """Retired network entry point: reject even with consent or mocked transport."""
    raise MapFetchError(REASONS['osm_network_disabled'][0], code='osm_network_disabled')


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
    """Retired network entry point: reject even with consent or mocked transport."""
    raise MapFetchError(REASONS['osm_network_disabled'][0], code='osm_network_disabled')


# 只允许精确行政区和道路对象查询，不接受模型URL、坐标、QL或输出路径。
MAX_QUERY_WAYS = 32
MAX_QUERY_NODES = 10000


def _exact_text(value):
    if (not isinstance(value, str) or not 1 <= len(value.strip()) <= 80
            or any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
            or len(value.encode('utf-8')) > 240):
        raise MapFetchError('Expected a short exact map name', code='invalid_input')
    return value.strip()


def _fetch_named_roads(area_name, names, output_path, *, consent_download=False, area_hierarchy=None, resolution=None):
    """Retired network entry point: reject even with consent or mocked transport."""
    raise MapFetchError(REASONS['osm_network_disabled'][0], code='osm_network_disabled')


def fetch_named_roads(area_name, names, output_path, *, consent_download=False, area_hierarchy=None):
    """Retired network entry point: reject even with consent or mocked transport."""
    raise MapFetchError(REASONS['osm_network_disabled'][0], code='osm_network_disabled')
