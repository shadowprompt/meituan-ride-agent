"""固定公共入口的实时调研；不接受模型 URL，不调用 Overpass，不上传图像。"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import time
import unicodedata
import socket
import threading
from contextlib import contextmanager
from urllib.parse import urlencode

import map_research
from map_fetch import MapFetchError

MAX_REQUESTS = 8
MAX_SEARCHES = 3
MAX_BYTES = map_research.MAX_BYTES
TIMEOUT = 25
RATE_FILE = Path(__file__).resolve().parents[1] / 'outputs' / '.research-public-rate'


def fail(code):
    raise MapFetchError('Public research request failed; remote content withheld', code=code)


# Only local schema omissions/type errors may be repaired. Unsafe text, extra parameters,
# provenance failures and exhausted budgets retain terminal invalid_input semantics.
MAX_SCHEMA_CORRECTIONS = 2
TOOL_SCHEMA = {
    'search_map_evidence': {'required': ['action', 'names', 'evidence'],
        'optional': ['region'], 'names': 'exactly one public place-name string, 1..100 characters',
        'region': 'optional public place-name string, 1..100 characters; evidenced, not invented',
        'evidence': 'required nonblank image/object rationale, 1..2000 characters',
        'query': 'one q = region + space + names[0], or names[0]; no URLs or extra parameters'},
    'inspect_map_object': {'required': ['action', 'object_type', 'object_id', 'evidence'],
        'object_type': 'node|way', 'object_id': 'integer ID from returned evidence',
        'evidence': 'required nonblank rationale, 1..2000 characters'},
}


class ModelShapeError(MapFetchError):
    def __init__(self):
        super().__init__('Model tool shape does not match schema', code='invalid_input')


def _place_text(value):
    if not isinstance(value, str):
        raise ModelShapeError()
    # Conservative public-name alphabet: no URL, query syntax, controls or format chars.
    if not 1 <= len(value.strip()) <= 100 or any(
            not (unicodedata.category(c)[0] in 'LMN' or c in " -'’.,·()（）") for c in value):
        fail('invalid_input')
    return ' '.join(value.split())


def search_query(action):
    name = _place_text(action['names'][0])
    return (_place_text(action['region']) + ' ' + name) if 'region' in action else name


def _successful_query(record):
    request = record.get('request', {})
    # Old audits have names only. New audits retain the exact q used on the wire.
    if isinstance(request.get('q'), str):
        return request['q']
    # Historical local audits may contain punctuation no longer accepted for new input.
    # Normalize for comparison only; never re-send or revalidate old successful searches.
    names = request.get('names', [])
    name = names[0] if isinstance(names, list) and names and isinstance(names[0], str) else ''
    region = request.get('region', '')
    return ' '.join(((region + ' ') if isinstance(region, str) and region else '').split() + name.split())


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        fail('timeout')
    return remaining


@contextmanager
def _rate_lock(deadline):
    # 非阻塞锁重探不是网络重试；排队和限频等待均计入同一请求deadline。
    RATE_FILE.parent.mkdir(exist_ok=True)
    fd = os.open(RATE_FILE, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as rate:
        if os.name == 'nt':
            import msvcrt
            if not rate.read():
                rate.write('0')
                rate.flush()
        else:
            import fcntl
        while True:
            _remaining(deadline)
            try:
                if os.name == 'nt':
                    rate.seek(0)
                    msvcrt.locking(rate.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(rate, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                time.sleep(min(0.05, _remaining(deadline)))
        try:
            rate.seek(0)
            yield rate
        finally:
            if os.name == 'nt':
                rate.seek(0)
                msvcrt.locking(rate.fileno(), msvcrt.LK_UNLCK, 1)


def _get(host, path):
    # 跨网页 CLI 子进程串行限频；固定可信 HTTPS，不读代理、不跟重定向、不重试。
    if host not in ('nominatim.openstreetmap.org', 'api.openstreetmap.org'):
        fail('invalid_input')
    deadline = time.monotonic() + TIMEOUT
    with _rate_lock(deadline) as rate:
        try:
            previous = float(rate.read() or '0')
        except ValueError:
            fail('invalid_input')
        delay = max(0, 1.1 - (time.time() - previous))
        if delay >= _remaining(deadline):
            fail('timeout')
        time.sleep(delay)
        rate.seek(0)
        rate.truncate()
        rate.write(str(time.time()))
        rate.flush()
        connection = http.client.HTTPSConnection(host, timeout=_remaining(deadline))
        response = None
        def expire():
            # socket timeout仅限制单次读；主动shutdown还限制慢速响应头/正文总时长。
            sock = connection.sock
            if response is not None and response.fp is not None and response.fp.raw is not None:
                sock = response.fp.raw._sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        timer = threading.Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
        try:
            connection.request('GET', path, headers={'User-Agent':'MeituanRideResearch/1.0 (interactive route evidence; https://github.com/shadowprompt/meituan-ride-agent)', 'Accept':'application/json', 'Accept-Encoding':'identity'})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail('timeout')
            if connection.sock:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            _remaining(deadline)
            if response.status != 200:
                fail('http_429' if response.status == 429 else 'redirect_refused' if 300 <= response.status < 400 else 'http_error')
            if response.getheader('Content-Encoding', 'identity') != 'identity' or response.getheader('Content-Type','').split(';')[0].strip() != 'application/json':
                fail('response_invalid')
            length = response.getheader('Content-Length')
            if length is not None and (not length.isdecimal() or int(length) > MAX_BYTES):
                fail('response_limit')
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail('timeout')
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
            return b''.join(chunks)
        except MapFetchError:
            raise
        except TimeoutError:
            fail('timeout')
        except (OSError, http.client.HTTPException):
            _remaining(deadline)
            fail('network_error')
        finally:
            timer.cancel()
            connection.close()


def initialize(state, folder):
    folder = Path(folder)
    (folder/'research').mkdir(exist_ok=True)
    (folder/'research-manifest.json').write_text('{"sources":[]}', encoding='utf-8')
    refresh(state, folder)


def archive(folder):
    return map_research.load(Path(folder)/'research-manifest.json', folder, allow_empty=True)


def refresh(state, folder):
    data = archive(folder)
    map_research.export_osm(data, Path(folder)/'map.osm')
    for key in ('review_region','research_presented_version','review_presented_version','review_presented_regions'):
        state.pop(key, None)
    state['research_object_count'] = len(data['objects'])
    state['research_version'] = data['version']
    state['research_osm_sha256'] = hashlib.sha256((Path(folder)/'map.osm').read_bytes()).hexdigest()
    return data


def execute(state, folder, action, budget, consent):
    """失败也消耗本轮预算；查询正文/令牌不进入异常；仅成功真实响应成为证据。"""
    name = action.get('action')
    if consent is not True:
        fail('consent_required')
    if budget['requests'] >= MAX_REQUESTS:
        fail('response_limit')
    required = ({'action', 'names', 'evidence'} if name == 'search_map_evidence'
                else {'action', 'object_type', 'object_id', 'evidence'})
    allowed = required | ({'region'} if name == 'search_map_evidence' else set())
    if name not in TOOL_SCHEMA or set(action) - allowed:
        fail('invalid_input')
    # Security/provenance checks precede shape recovery even when evidence is missing.
    if name == 'search_map_evidence':
        if budget['searches'] >= MAX_SEARCHES:
            fail('invalid_input')
        if 'region' in action and isinstance(action['region'], str):
            _place_text(action['region'])
        if isinstance(action.get('names'), str):
            _place_text(action['names'])
        if isinstance(action.get('names'), list):
            for value in action['names']:
                if isinstance(value, str):
                    _place_text(value)
            if (len(action['names']) == 1 and isinstance(action['names'][0], str)
                    and ('region' not in action or isinstance(action['region'], str))):
                candidate = search_query(action).casefold()
                previous = ([_successful_query(item).casefold() for item in state['research_requests']
                             if item.get('tool') == 'search_map_evidence' and item.get('outcome') == 'succeeded']
                            if 'research_requests' in state else state.get('research_search_queries', []))
                if candidate in previous or candidate in budget.get('failed_queries', []):
                    fail('invalid_input')
    elif isinstance(action.get('object_type'), str) and type(action.get('object_id')) is int:
        identity = [action['object_type'], action['object_id']]
        if identity not in state.get('research_seen_ids', []) or identity in budget.get('failed_objects', []):
            fail('invalid_input')
    if not required <= set(action):
        raise ModelShapeError()
    evidence = action['evidence']
    if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 2000:
        raise ModelShapeError()
    if name == 'search_map_evidence':
        if not isinstance(action['names'], list) or len(action['names']) != 1:
            raise ModelShapeError()
        query = search_query(action)
        # 兼容旧任务：旧版可能把失败尝试写进此列表，按成功审计重建。
        if 'research_requests' in state:
            state['research_search_queries'] = list(dict.fromkeys(
                _successful_query(item).casefold() for item in state['research_requests']
                if item.get('tool') == 'search_map_evidence' and item.get('outcome') == 'succeeded'))
        if budget['searches'] >= MAX_SEARCHES or query.casefold() in state.get('research_search_queries',[]) or query.casefold() in budget.get('failed_queries',[]):
            fail('invalid_input')
        budget['searches'] += 1
        host = 'nominatim.openstreetmap.org'
        path = '/search?' + urlencode({'q':query,'format':'jsonv2','limit':5,'addressdetails':0})
    elif name == 'inspect_map_object':
        kind, ident = action['object_type'], action['object_id']
        if not isinstance(kind, str) or type(ident) is not int:
            raise ModelShapeError()
        if [kind,ident] in budget.get('failed_objects',[]) or kind not in ('node','way') or not 1 <= ident <= 999999999999999 or [kind,ident] not in state.get('research_seen_ids',[]):
            fail('invalid_input')
        host = 'api.openstreetmap.org'
        # 节点直接核查父 ways；坐标来自已返回 way/full 中的原始节点，不能伪造。
        path = f'/api/0.6/{kind}/{ident}/' + ('ways.json' if kind == 'node' else 'full.json')
    else:
        fail('invalid_input')
    budget['requests'] += 1
    record = {'tool':name, 'request':{**{k:action[k] for k in ('names','region') if k in action},'q':query} if name == 'search_map_evidence' else {'object_type':kind,'object_id':ident}, 'outcome':'attempted', 'request_index':budget['requests'], 'request_limit':MAX_REQUESTS}
    state.setdefault('research_requests',[]).append(record)
    try:
        raw = _get(host, path)
        body = json.loads(raw, parse_constant=lambda _: fail('response_invalid'))
        source = {'url':'https://' + host + path,'sha256':hashlib.sha256(raw).hexdigest()}
        if name == 'search_map_evidence':
            if not isinstance(body,list) or len(body)>5:
                fail('response_invalid')
            hits = []
            for item in body:
                if not isinstance(item,dict) or item.get('osm_type') not in ('node','way','relation') or type(item.get('osm_id')) is not int or not 1 <= item['osm_id'] <= 999999999999999:
                    fail('response_invalid')
                label = item.get('display_name','')
                if not isinstance(label,str) or len(label)>4096:
                    fail('response_invalid')
                hits.append({'object':{'type':item['osm_type'],'id':item['osm_id'],'tags':{'name':label}},'sources':[source], 'discovery_only':True})
            # 原始搜索响应仅本地留证；不把地点中心当作道路节点坐标。
            target = Path(folder)/'research'/('search-'+hashlib.sha256((source['url'] + source['sha256']).encode()).hexdigest()+'.json')
            target.write_bytes(raw)
            state.setdefault('research_search_queries',[]).append(query.casefold())
        else:
            manifest = json.loads((Path(folder)/'research-manifest.json').read_text())
            saved = 'research/object-'+source['sha256']+'.json'
            (Path(folder)/saved).write_bytes(raw)
            entry = {**source,'saved_as':saved}
            sources = manifest['sources']
            if entry not in sources:
                sources.append(entry)
            candidate = Path(folder)/'research-candidate.json'
            candidate.write_text(json.dumps({'sources':sources}),encoding='utf-8')
            try:
                # 复用原始对象校验：请求对象、节点父关系、ID、坐标、hash与冲突版本。
                map_research.load(candidate, folder, allow_empty=True)
            finally:
                candidate.unlink(missing_ok=True)
            (Path(folder)/'research-manifest.json').write_text(json.dumps({'sources':sources}),encoding='utf-8')
            data = refresh(state, folder)
            requested = (kind,ident)
            if kind == 'node':
                hits = [item for (k,_),item in data['objects'].items() if k == 'way' and ident in item['object']['nodes']]
            else:
                hits = [data['objects'][requested]] + [data['objects'][('node',n)] for n in data['objects'][requested]['object']['nodes'] if ('node',n) in data['objects']]
        record.update(outcome='succeeded', source=source, object_count=len(hits))
        return {'tool':name,'status':'found' if hits else 'no_results','objects':hits,'version':state['research_version'], 'coordinate_system':'WGS84','verified':False,'original_gps':False,'source':source,'budget':dict(budget), 'notice':'Live public evidence. Search is discovery only; relations cannot be inspected. Missing coordinates require way/full. Screenshot identity/traversal remains unverified. © OpenStreetMap contributors https://www.openstreetmap.org/copyright'}
    except MapFetchError as exc:
        record.update(outcome='failed',reason_code=exc.code)
        if name == 'search_map_evidence':
            budget.setdefault('failed_queries', []).append(query.casefold())
        else:
            budget.setdefault('failed_objects', []).append([kind,ident])
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        record.update(outcome='failed',reason_code='response_invalid')
        if name == 'search_map_evidence':
            budget.setdefault('failed_queries', []).append(query.casefold())
        else:
            budget.setdefault('failed_objects', []).append([kind,ident])
        fail('response_invalid')
