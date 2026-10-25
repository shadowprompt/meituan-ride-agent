"""高德 Web 服务地理编码：仅 GCJ-02 定位线索，不生成道路网络或轨迹。
No import-time I/O, no retries, no redirects/proxies, no remote text in errors.
"""
import http.client
import json
import math
import os
import socket
import time
import urllib.parse

from map_fetch import MapFetchError, _exact_text

ENDPOINT = 'https://restapi.amap.com/v3/geocode/geo'
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 512_000
MAX_CANDIDATES = 10


def _request(params, key, deadline):
    """Fixed HTTPS host/path. Never expose a credential-bearing URL or exception."""
    connection = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MapFetchError('Map deadline exceeded', code='timeout')
        connection = http.client.HTTPSConnection('restapi.amap.com', timeout=remaining)
        connection.connect()
        def tighten():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MapFetchError('Map deadline exceeded', code='timeout')
            connection.sock.settimeout(remaining)
        tighten()
        path = '/v3/geocode/geo?' + urllib.parse.urlencode(dict(params, key=key, output='JSON'))
        connection.request('GET', path, headers={'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        tighten()
        response = connection.getresponse()
        if 300 <= response.status < 400:
            raise MapFetchError('Redirect refused', code='redirect_refused')
        if response.status != 200:
            raise MapFetchError('Map HTTP failure', code='http_429' if response.status == 429 else 'http_error', http_status=response.status)
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise MapFetchError('Unexpected encoding')
        length = response.getheader('Content-Length')
        if length is not None:
            try:
                length = int(length)
            except (ValueError, TypeError):
                raise MapFetchError('Invalid response length') from None
            if length < 0 or length > MAX_RESPONSE_BYTES:
                raise MapFetchError('Response limit', code='response_limit')
        chunks, size = [], 0
        while True:
            # HTTPResponse may retain a socket after HTTPSConnection releases it.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MapFetchError('Map deadline exceeded', code='timeout')
            if response.fp is not None and response.fp.raw is not None:
                response.fp.raw._sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise MapFetchError('Response limit', code='response_limit')
            chunks.append(chunk)
        if time.monotonic() > deadline:
            raise MapFetchError('Map deadline exceeded', code='timeout')
        return b''.join(chunks)
    except MapFetchError:
        raise
    except (TimeoutError, socket.timeout):
        raise MapFetchError('Map timeout', code='timeout') from None
    except (OSError, http.client.HTTPException):
        raise MapFetchError('Map network failure', code='network_error') from None
    finally:
        if connection is not None:
            connection.close()


def _candidates(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise MapFetchError('Response limit', code='response_limit')
    try:
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or data.get('status') not in ('0', '1'):
            raise ValueError()
        if data['status'] != '1':
            # Never copy info/infocode/body/URL to diagnostics; fixed classifications.
            code = data.get('infocode')
            reason = ('amap_credentials_rejected' if code in ('10001', '10005', '10006', '10007', '10008', '10009', '10012', '10013')
                      else 'amap_quota' if code in ('10003', '10004', '10010', '10014', '10019', '10020', '10021', '10044')
                      else 'amap_service_error')
            raise MapFetchError('AMap service rejected request', code=reason)
        entries = data.get('geocodes')
        if not isinstance(entries, list):
            raise ValueError()
        if len(entries) > MAX_CANDIDATES:
            raise MapFetchError('Candidate limit', code='response_limit')
        candidates = []
        for entry in entries:
            location = entry.get('location') if isinstance(entry, dict) else None
            if not isinstance(location, str) or len(location) > 64:
                raise ValueError()
            pair = location.split(',')
            if len(pair) != 2:
                raise ValueError()
            lon, lat = map(float, pair)
            if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError()
            # Only numeric coordinates are adopted; no arbitrary server strings.
            candidates.append({'location': [lon, lat], 'coordinate_system': 'GCJ02',
                               'verified': False, 'administrative_membership_verified': False})
        return candidates
    except MapFetchError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise MapFetchError('Invalid AMap response', code='response_invalid') from None


def configuration_status():
    """Local-only preflight; never contacts AMap or returns credential values."""
    key = os.environ.get('AMAP_API_KEY', '').strip()
    if not key:
        return 'amap_key_missing'
    if len(key) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        return 'amap_credentials_rejected'
    return 'configured_not_verified'


def require_configuration():
    status = configuration_status()
    if status != 'configured_not_verified':
        raise MapFetchError('Server AMap configuration unavailable', code=status)


def fetch_location_clues(area_name, names, *, consent_download=False, area_hierarchy=None):
    """At most 3 geocodes, shared 30s budget; no fallback/search expansion/retry.

    The structured address is a query hint, NOT verified admin membership. Keep
    all candidates; even one point is not proof of an exact road or route.
    """
    if consent_download is not True:
        raise MapFetchError('Fresh AMap consent required', code='consent_required')
    area_name = _exact_text(area_name)
    if not isinstance(names, list) or not 1 <= len(names) <= 3:
        raise MapFetchError('Expected 1..3 names', code='invalid_input')
    names = [_exact_text(n) for n in names]
    if len(set(names)) != len(names):
        raise MapFetchError('Duplicate names', code='invalid_input')
    if area_hierarchy is not None:
        if (not isinstance(area_hierarchy, list) or not 2 <= len(area_hierarchy) <= 3
                or ''.join(_exact_text(n) for n in area_hierarchy) != area_name):
            raise MapFetchError('Invalid hierarchy', code='invalid_input')
    require_configuration()
    key = os.environ['AMAP_API_KEY'].strip()
    deadline = time.monotonic() + TIMEOUT_SECONDS
    clues, response_bytes = [], 0
    for name in names:
        if time.monotonic() >= deadline:
            raise MapFetchError('Map deadline exceeded', code='timeout')
        raw = _request({'address': area_name + name}, key, deadline)
        response_bytes += len(raw)
        candidates = _candidates(raw)
        clues.append({'query_name': name, 'candidates': candidates,
                      'status': 'ambiguous' if len(candidates) > 1 else 'unverified_location' if candidates else 'not_found'})
    return {'provider': 'amap', 'endpoint': ENDPOINT, 'area_name': area_name,
            'names': names, 'clues': clues, 'response_bytes': response_bytes,
            'coordinate_system': 'GCJ02', 'supports_corridor': False,
            'verified': False, 'original_gps': False, 'status': 'location_clues_only',
            'warning': '高德地理编码仅返回 GCJ-02 位置线索，可能歧义或无结果；未验证行政区归属。不是道路几何、连通节点、WGS84轨迹或历史路线，不支持构建道路走廊，不可导航。'}
