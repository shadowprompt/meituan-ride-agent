"""固定公共入口的实时调研；不接受模型 URL，不调用 Overpass，不上传图像。"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import time
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


@contextmanager
def _rate_lock():
    # 跨平台进程锁；锁文件在本应用输出目录，不接受模型路径。
    RATE_FILE.parent.mkdir(exist_ok=True)
    fd = os.open(RATE_FILE, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as rate:
        if os.name == 'nt':
            import msvcrt
            if not rate.read():
                rate.write('0')
                rate.flush()
            rate.seek(0)
            msvcrt.locking(rate.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(rate, fcntl.LOCK_EX)
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
    with _rate_lock() as rate:
        try:
            previous = float(rate.read() or '0')
        except ValueError:
            fail('invalid_input')
        time.sleep(max(0, 1.1 - (time.time() - previous)))
        rate.seek(0)
        rate.truncate()
        rate.write(str(time.time()))
        rate.flush()
        connection = http.client.HTTPSConnection(host, timeout=TIMEOUT)
        deadline = time.monotonic() + TIMEOUT
        try:
            connection.request('GET', path, headers={'User-Agent':'MeituanRideResearch/1.0 (interactive route evidence; https://github.com/shadowprompt/meituan-ride-agent)', 'Accept':'application/json', 'Accept-Encoding':'identity'})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                fail('timeout')
            if connection.sock:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
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
            fail('network_error')
        finally:
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
    state['research_object_count'] = len(data['objects'])
    state['research_version'] = data['version']
    state['research_osm_sha256'] = hashlib.sha256((Path(folder)/'map.osm').read_bytes()).hexdigest()
    return data


def execute(state, folder, action, budget, consent):
    """失败也消耗本轮预算；查询正文/令牌不进入异常；仅成功真实响应成为证据。"""
    name = action.get('action')
    if consent is not True:
        fail('consent_required')
    evidence = action.get('evidence')
    if not isinstance(evidence, str) or not 1 <= len(evidence.strip()) <= 2000:
        fail('invalid_input')
    if budget['requests'] >= MAX_REQUESTS:
        fail('response_limit')
    if name == 'search_map_evidence':
        if set(action) != {'action','names','evidence'} or not isinstance(action['names'],list) or len(action['names']) != 1:
            fail('invalid_input')
        query = action['names'][0]
        if not isinstance(query,str) or not 1 <= len(query.strip()) <= 100 or any(ord(c)<32 for c in query):
            fail('invalid_input')
        query = query.strip()
        if budget['searches'] >= MAX_SEARCHES or query.casefold() in state.get('research_search_queries',[]):
            fail('invalid_input')
        budget['searches'] += 1
        state.setdefault('research_search_queries',[]).append(query.casefold())
        host = 'nominatim.openstreetmap.org'
        path = '/search?' + urlencode({'q':query,'format':'jsonv2','limit':5,'addressdetails':0})
    elif name == 'inspect_map_object':
        if set(action) != {'action','object_type','object_id','evidence'}:
            fail('invalid_input')
        kind, ident = action['object_type'], action['object_id']
        if kind not in ('node','way') or type(ident) is not int or not 1 <= ident <= 999999999999999 or [kind,ident] not in state.get('research_seen_ids',[]):
            fail('invalid_input')
        host = 'api.openstreetmap.org'
        # 节点直接核查父 ways；坐标来自已返回 way/full 中的原始节点，不能伪造。
        path = f'/api/0.6/{kind}/{ident}/' + ('ways.json' if kind == 'node' else 'full.json')
    else:
        fail('invalid_input')
    budget['requests'] += 1
    record = {'tool':name, 'request':{'names':action['names']} if name == 'search_map_evidence' else {'object_type':kind,'object_id':ident}, 'outcome':'attempted', 'request_index':budget['requests'], 'request_limit':MAX_REQUESTS}
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
            target = Path(folder)/'research'/('search-'+source['sha256']+'.json')
            target.write_bytes(raw)
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
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        record.update(outcome='failed',reason_code='response_invalid')
        fail('response_invalid')
