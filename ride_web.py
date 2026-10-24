"""Loopback-only Chinese UI. Run: python ride_web.py --port 8765.

POST /api/tasks: {image_base64, mode: demo|api, goal: pixels|registration|roads,
consent_upload: bool}. POST /api/tasks/<uuid>/resume: {answer?, color?, crop?,
controls_json?, osm_xml?, fetch_map?, consent_map?, consent_upload?}.
Every POST requires X-CSRF-Token from GET /api/config (or the root UI).
GET /api/tasks/<uuid> returns a filtered status; GET /artifacts/<uuid>/<name>
serves only deterministic allowlisted outputs. Nothing exposes credentials/state/input.
"""
import argparse
import base64
import binascii
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from urllib.parse import urlsplit
import uuid

BASE = Path(__file__).resolve().parent
MAX_IMAGE = 20_000_000
MAX_BODY = 44_000_000  # 30 MB OSM XML plus JSON overhead
ARTIFACTS = frozenset((
    'inspection/colors.json', 'inspection/color-preview.png',
    'extraction/mask.png', 'extraction/overlay.png', 'extraction/extraction.json',
    'centerline/skeleton.png', 'centerline/graph-overlay.png', 'centerline/pixel-graph.json',
    'registration/registration.json', 'registration/registered-segments.geojson',
    'roads/road-match.json', 'roads/road-candidates.geojson', 'roads/road-overlay.png',
))
UUID_RE = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


def artifact_current(state, name):
    """旧文件可留作审计，但网页必须逐项标记失效，不能只靠统一免责声明。"""
    prefix = name.split('/')[0]
    if prefix == 'inspection':
        return bool(state.get('colors_inspected'))
    valid = bool(state.get('extracted'))
    if prefix == 'extraction':
        return valid
    valid = valid and bool(state.get('centerlined'))
    if prefix == 'centerline':
        return valid
    valid = valid and bool(state.get('registered'))
    if prefix == 'registration':
        return valid
    return valid and bool(state.get('registration_gate_passed')) and bool(state.get('match_attempted'))


# Internal failures expose only fixed categories and recovery tips, never
# exception text, subprocess output, remote responses or local paths.
GUIDANCE = {
    'extraction_required': '尚无有效轨迹提取；请先提供或修正颜色和裁剪范围，完成提取后再上传控制点或继续。',
    'centerline_required': '尚无有效中心线；请检查提取预览、修正颜色或裁剪，再继续生成中心线。',
    'registration_required': '尚未完成当前裁剪的配准；请上传与当前裁剪对应的控制点 JSON，完成配准后再下载地图或匹配。',
    'registration_gate_failed': '配准尚未通过独立检查；请核对控制点坐标及独立检查点，重新配准后再下载地图或匹配。',
    'road_processing_failed': '道路处理未完成；请检查配准范围及本地 OSM 数据，或重新授权地图下载后继续。结果不可用于导航。',
    'task_processing_failed': '任务处理未完成；请刷新状态、核对输入和服务端模型配置，再重新授权继续。',
    'cli_failed': '执行未完成；请刷新任务状态，核对截图格式、输入文件和服务端模型配置后重试。',
    'timeout': '运行超时；任务可能已保存，请先刷新状态，避免重复提交，再重新授权继续。',
    'request_rejected': '请求未接受；请按提示修正输入或刷新网页后重试。',
    'local_state_failed': '本地状态不可用；请刷新状态或创建新任务，必要时由服务端管理员检查。',
}


def processing_category(state):
    """Report observed prerequisites, not a guessed cause from raw error text."""
    if not state.get('extracted'):
        return 'extraction_required'
    if not state.get('centerlined'):
        return 'centerline_required'
    if state.get('goal') in ('registration', 'roads'):
        if not state.get('registered'):
            return 'registration_required'
        if state.get('goal') == 'roads':
            if not state.get('registration_gate_passed'):
                return 'registration_gate_failed'
            return 'road_processing_failed'
    return 'task_processing_failed'


class WebError(Exception):
    def __init__(self, message, code=400, category='request_rejected'):
        super().__init__(message)
        self.code = code
        self.category = category if category in GUIDANCE else 'request_rejected'

    def public(self):
        return {'error': str(self), 'error_category': self.category,
                'recovery_hint': GUIDANCE[self.category]}


def safe_path(root, *parts):
    """拒绝符号链接（含目录），而非只检查最终文件名。"""
    root = Path(root)
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise WebError('路径不安全', 403)
    for item in (path, *path.parents):
        if item.is_symlink():
            raise WebError('禁止符号链接', 403)
    return path


def strict_object(data):
    if not isinstance(data, dict):
        raise WebError('请求必须是 JSON 对象')
    return data


def flag(data, name):
    value = data.get(name, False)
    if type(value) is not bool:
        raise WebError(name + ' 必须是布尔值')
    return value


def text(value, name, limit):
    if not isinstance(value, str) or len(value.encode('utf-8')) > limit or '\x00' in value:
        raise WebError(name + ' 格式或长度不正确')
    return value


class RideServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=8765, *, output_root=None, cli_timeout=180):
        # 绑定地址不能由浏览器、环境变量或命令行改成公网地址。
        self.root = Path(output_root) if output_root is not None else BASE/'outputs'/'web'
        safe_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.token = secrets.token_urlsafe(32)
        self.change_lock = threading.RLock()
        self.cli_timeout = max(1, min(300, cli_timeout))
        super().__init__(('127.0.0.1', port), Handler)

    def folder(self, task_id):
        if not UUID_RE.fullmatch(task_id):
            raise WebError('任务编号无效', 404)
        folder = safe_path(self.root, task_id)
        if not folder.is_dir():
            raise WebError('任务不存在', 404)
        return folder

    def tree_safe(self, folder):
        # CLI 会覆盖固定输出；调用前拒绝任务树内的链接，防止跨任务写入。
        safe_path(self.root, folder.relative_to(self.root))
        for parent, dirs, files in os.walk(folder, followlinks=False):
            for name in dirs + files:
                safe_path(self.root, Path(parent).relative_to(self.root), name)

    def run_cli(self, args):
        try:
            result = subprocess.run([sys.executable, str(BASE/'ride_agent.py'), *args],
                                    cwd=str(BASE), shell=False, capture_output=True,
                                    timeout=self.cli_timeout, check=False)
        except subprocess.TimeoutExpired:
            raise WebError('运行超时，任务可能已保存；请刷新状态后再继续', 504, 'timeout') from None
        # 不向页面返回 stdout/stderr：异常中可能包含密钥、远端响应或本地路径。
        if result.returncode:
            category = 'cli_failed'
            # A prerequisite may be rejected before task.json is updated.
            # Inspect local stage flags only, never stdout/stderr.
            try:
                from ride_agent import load
                state = load(Path(args[args.index('--task-dir')+1]))
                if '--controls-file' in args and not state.get('extracted'):
                    category = 'extraction_required'
                elif '--fetch-map' in args and not state.get('registered'):
                    category = 'registration_required'
                elif '--fetch-map' in args and not state.get('registration_gate_passed'):
                    category = 'registration_gate_failed'
                elif state.get('status') == 'error':
                    category = processing_category(state)
            except (OSError, ValueError, TypeError, KeyError, IndexError):
                pass
            raise WebError('CLI 运行失败（详细错误不在网页公开）', 422, category)

    def status(self, task_id):
        from ride_agent import load
        folder = self.folder(task_id)
        safe_path(self.root, task_id, 'task', 'task.json')
        try:
            state = load(folder/'task')
        except (ValueError, OSError, TypeError, KeyError):
            raise WebError('任务状态尚未生成或无法读取', 409) from None
        fields = ('mode', 'goal', 'status', 'question', 'summary', 'warnings',
                  'registration_notice', 'road_notice', 'verified', 'original_gps')
        result = {key: state[key] for key in fields if key in state}
        if state.get('error') or state.get('status') == 'error':
            category = processing_category(state)
            result.update(error='任务处理错误（详细错误不在网页公开）',
                          error_category=category, recovery_hint=GUIDANCE[category])
        result['task_id'] = task_id
        available = []
        for name in sorted(ARTIFACTS):
            if safe_path(self.root, task_id, 'task', name).is_file():
                available.append({'name': name, 'url': '/artifacts/'+task_id+'/'+name,
                                  'current': artifact_current(state,name)})
        result['artifacts'] = available
        result['artifact_notice'] = '产物可能来自先前步骤，修改颜色/裁剪后旧文件可能失效；仅为未验证候选，不可导航。'
        # 纵使模型意外复述了环境密钥，也不能显示给浏览器。
        key = os.environ.get('RIDE_API_KEY')
        def redact(value):
            if isinstance(value, str):
                return value.replace(key, '[已隐藏]') if key else value
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {name: redact(item) for name, item in value.items()}
            return value
        return redact(result)

    def create(self, data):
        allowed = {'image_base64', 'mode', 'goal', 'consent_upload'}
        if data.keys() - allowed:
            raise WebError('未知创建参数（不接受文件路径）')
        mode = data.get('mode', 'demo')
        goal = data.get('goal', 'pixels')
        if mode not in ('demo', 'api') or goal not in ('pixels', 'registration', 'roads'):
            raise WebError('模式或目标无效')
        consent = flag(data, 'consent_upload')
        if mode == 'api' and not consent:
            raise WebError('真实 API 每次调用均需明确同意上传截图、预览及对话', 403)
        encoded = text(data.get('image_base64'), '截图 base64', ((MAX_IMAGE+2)//3)*4)
        try:
            image = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise WebError('截图 base64 无效') from None
        if not image or len(image) > MAX_IMAGE:
            raise WebError('截图须为 1–20 MB 内的 PNG/JPEG/WebP')
        task_id = str(uuid.uuid4())
        folder = safe_path(self.root, task_id)
        folder.mkdir(mode=0o700)
        inputs = folder/'inputs'
        inputs.mkdir(mode=0o700)
        source = inputs/'source-image'
        source.write_bytes(image)
        args = ['start', '--image', str(source), '--task-dir', str(folder/'task'), '--goal', goal]
        if mode == 'demo':
            args.append('--demo')
        if consent:
            args.append('--consent-upload')
        try:
            self.run_cli(args)
        except WebError as exc:
            # 创建失败仍提供编号，以便用户查看已落盘的任务状态。
            raise WebError(str(exc)+'；任务编号：'+task_id, exc.code, exc.category) from None
        return self.status(task_id)

    def resume(self, task_id, data):
        allowed = {'answer', 'color', 'crop', 'controls_json', 'osm_xml',
                   'fetch_map', 'consent_map', 'consent_upload'}
        if data.keys() - allowed:
            raise WebError('未知继续参数（不接受文件路径）')
        folder = self.folder(task_id)
        self.tree_safe(folder)
        state = self.status(task_id)
        upload = flag(data, 'consent_upload')
        consent_map = flag(data, 'consent_map')
        fetch_map = flag(data, 'fetch_map')
        if state['mode'] == 'api' and not upload:
            raise WebError('真实 API 每次继续均需重新同意上传', 403)
        if fetch_map and not consent_map:
            raise WebError('下载地图须明确同意发送范围坐标', 403)
        if fetch_map and 'osm_xml' in data:
            raise WebError('地图下载与本地 OSM 不能同时选择')
        args = ['resume', '--task-dir', str(folder/'task')]
        pending = {}
        if 'answer' in data:
            answer = text(data['answer'], '回答', 32000)
            if not answer or len(answer) > 8000:
                raise WebError('回答应为 1–8000 字符')
            pending['answer.txt'] = answer.encode('utf-8')
            args += ['--answer-file', str(folder/'inputs'/'answer.txt')]
        if 'color' in data:
            color = text(data['color'], '颜色', 7)
            if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                raise WebError('颜色须为 #RRGGBB')
            args += ['--color', color]
        if 'crop' in data:
            crop = data['crop']
            if (not isinstance(crop, list) or len(crop) != 4
                    or any(type(n) is not int or not 0 <= n <= 100000 for n in crop)
                    or crop[2] <= crop[0] or crop[3] <= crop[1]):
                raise WebError('裁剪须为 [左,上,右,下] 整数且宽高大于零')
            args += ['--crop', *map(str, crop)]
        if 'controls_json' in data:
            controls = text(data['controls_json'], '控制点 JSON', 1_000_000)
            try:
                parsed = json.loads(controls, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if not isinstance(parsed, dict):
                    raise ValueError()
            except (ValueError, RecursionError):
                raise WebError('控制点必须是有效 JSON 对象') from None
            pending['controls-upload.json'] = controls.encode('utf-8')
            args += ['--controls-file', str(folder/'inputs'/'controls-upload.json')]
        if 'osm_xml' in data:
            osm = text(data['osm_xml'], 'OSM XML', 30_000_000)
            # XML 的结构与地理约束继续由离线 matcher 校验，禁止 DTD/实体。
            if not osm.strip() or re.search(r'<!\s*(?:DOCTYPE|ENTITY)', osm, re.I):
                raise WebError('OSM XML 为空或含禁止的 DTD/实体')
            pending['osm-upload.xml'] = osm.encode('utf-8')
            args += ['--osm-file', str(folder/'inputs'/'osm-upload.xml')]
        if not any(k in data for k in ('answer', 'color', 'crop', 'controls_json', 'osm_xml')) and not fetch_map:
            raise WebError('请提供回答、修正或证据文件')
        if upload:
            args.append('--consent-upload')
        if fetch_map:
            args += ['--fetch-map', '--consent-map']
        # 所有参数校验通过后才写固定的任务内输入文件。
        for name, content in pending.items():
            target = safe_path(self.root, task_id, 'inputs', name)
            target.write_bytes(content)
        self.run_cli(args)
        return self.status(task_id)


PAGE = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>骑行截图分析（本地）</title>
<style>body{max-width:950px;margin:2em auto;padding:0 1em;font-family:system-ui;line-height:1.6}fieldset{margin:1em 0}label{display:block;margin:.5em 0}textarea{width:95%;height:6em}button{padding:.5em 1em}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eee;padding:1em}img{max-width:100%;max-height:420px}input[type=text]{width:70%}.consent{border:2px solid #855400;background:#fff4cf;padding:1em;font-weight:bold}</style>
<h1>骑行截图分析 · 本地网页</h1><p>结果只是未验证候选，不是真实 GPS，不可用于导航。Demo 为固定离线演示，并非模型识别。截图不会通过本网页向外发送；只有明确同意的 API / 地图步骤会访问外部服务。</p>
<fieldset><legend>1. 创建任务</legend><label>截图（PNG/JPEG/WebP，≤20 MB、≤400万像素） <input id="image" type="file" accept="image/png,image/jpeg,image/webp"></label>
<label>模式 <select id="mode"><option value="demo">离线 Demo</option><option value="api">真实视觉 API</option></select></label>
<label>目标 <select id="goal"><option value="pixels">提取轨迹像素</option><option value="registration">手工控制点配准</option><option value="roads">道路候选匹配</option></select></label>
<label class="consent"><input id="uploadConsent" type="checkbox">仅本次同意向已配置模型 API 上传截图、派生预览、诊断及对话（创建/继续后自动取消；每次继续前须回到此处重新勾选）</label>
<button id="create">创建并开始</button></fieldset>
<fieldset><legend>2. 任务状态</legend><label>任务编号 <input id="task" type="text" placeholder="服务器生成的 UUID，可粘贴恢复"></label><button id="refresh">刷新状态</button><pre id="status">尚未创建任务</pre><h3>当前问题</h3><pre id="question"></pre></fieldset>
<fieldset><legend>3. 回答 / 修正 / 上传证据</legend><label>回答（Demo 首次可填 {"crop":[0,0,80,60],"color":"#00BFA5"}）<textarea id="answer"></textarea></label>
<label>修正颜色 <input id="color" type="text" placeholder="#00BFA5，空白不修改"></label>
<label>修正裁剪 <input id="crop" type="text" placeholder="[左,上,右,下]，空白不修改"></label>
<p>裁剪变更会使旧控制点失效。控制点必须使用当前裁剪坐标，网页不会自动地理定位。</p>
<label>控制点 JSON <input id="controls" type="file" accept=".json,application/json"></label><label>本地 OSM XML <input id="osm" type="file" accept=".osm,.xml"></label>
<p id="roadsNotice">注意：勾选下载地图或上传本地 OSM 后，点击“继续处理”会将任务目标自动切换为 roads（道路候选匹配），即使原目标为像素提取或配准。下载须先完成配准并通过独立检查；下载授权与 API 上传授权相互独立，未勾选不会下载。</p>
<label><input id="fetch" type="checkbox">下载当前配准范围的地图（不可同时上传 OSM）</label><label><input id="mapConsent" type="checkbox">仅本次同意向 Overpass 发送地理范围（不发送截图）</label>
<button id="resume">继续处理</button></fieldset><p id="error" role="alert"></p><h2>产物预览</h2><p>失效产物仅提供审计链接，不显示为当前预览。当前产物也未经路线确认。</p><div id="artifacts"></div>
<script nonce="__TOKEN__">
'use strict';const token='__TOKEN__';const $=id=>document.getElementById(id);
async function request(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':token}:{},body:data?JSON.stringify(data):undefined});const d=await r.json();if(!r.ok)throw Error((d.error||'请求失败')+(d.error_category?' ['+d.error_category+']':'')+(d.recovery_hint?'\n'+d.recovery_hint:''));return d;}
function render(d){$('task').value=d.task_id;$('status').textContent=JSON.stringify(d,null,2);$('question').textContent=typeof d.question==='string'?d.question:JSON.stringify(d.question||'无');$('artifacts').replaceChildren();for(const a of d.artifacts||[]){const div=document.createElement('div'),link=document.createElement('a');link.textContent=(a.current?'[当前未验证] ':'[已失效/仅审计] ')+a.name;link.href=a.url;link.target='_blank';link.rel='noopener';div.append(link);if(a.current&&a.name.endsWith('.png')){const img=document.createElement('img');img.src=a.url;img.alt=a.name;div.append(document.createElement('br'),img);}$('artifacts').append(div);}}
async function action(fn){$('error').textContent='处理中，请稍候（最长约 3 分钟）';for(const b of document.querySelectorAll('button'))b.disabled=true;try{await fn();$('error').textContent='';}catch(e){$('error').textContent=e.message;}finally{for(const b of document.querySelectorAll('button'))b.disabled=false;$('uploadConsent').checked=false;$('mapConsent').checked=false;}}
function taskPath(){const id=$('task').value.trim();if(!/^[0-9a-f-]{36}$/.test(id))throw Error('请先创建任务或填写编号');return '/api/tasks/'+id;}
async function fileText(id,max){const f=$(id).files[0];if(!f)return undefined;if(f.size>max)throw Error('上传文件过大');return await f.text();}
$('create').onclick=()=>action(async()=>{const f=$('image').files[0];if(!f||f.size>20000000)throw Error('请选择 ≤20 MB 截图');const b64=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});render(await request('/api/tasks',{image_base64:b64,mode:$('mode').value,goal:$('goal').value,consent_upload:$('uploadConsent').checked}));});
$('refresh').onclick=()=>action(async()=>render(await request(taskPath())));
$('resume').onclick=()=>action(async()=>{const d={consent_upload:$('uploadConsent').checked,fetch_map:$('fetch').checked,consent_map:$('mapConsent').checked};if($('answer').value)d.answer=$('answer').value;if($('color').value.trim())d.color=$('color').value.trim();if($('crop').value.trim())d.crop=JSON.parse($('crop').value);const controls=await fileText('controls',1000000),osm=await fileText('osm',30000000);if(controls!==undefined)d.controls_json=controls;if(osm!==undefined)d.osm_xml=osm;render(await request(taskPath()+'/resume',d));$('answer').value='';$('color').value='';$('crop').value='';$('controls').value='';$('osm').value='';$('fetch').checked=false;});
</script></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = 'RideLocal/1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, *_):
        pass  # 不记录用户输入、令牌或任务路径。

    def send_content(self, code, content, mime='application/json; charset=utf-8'):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'nonce-"+self.server.token+"'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(content)

    def send_json(self, code, data):
        self.send_content(code, json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8'))

    def guard(self):
        # 防 DNS 重绑定和跨站表单：Host/Origin/来源地址均须是此回环服务。
        if self.client_address[0] != '127.0.0.1':
            raise WebError('只允许本机访问', 403)
        hosts = {'127.0.0.1:'+str(self.server.server_port), 'localhost:'+str(self.server.server_port)}
        host = self.headers.get('Host')
        if len(self.headers.get_all('Host', [])) != 1 or host not in hosts:
            raise WebError('Host 不允许', 403)
        origins = self.headers.get_all('Origin', [])
        if len(origins) > 1 or origins and origins[0] != 'http://'+host:
            raise WebError('Origin 不允许', 403)
        if self.headers.get('Sec-Fetch-Site') not in (None, 'none', 'same-origin'):
            raise WebError('禁止跨站请求', 403)
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment or '%' in self.path:
            raise WebError('无效请求路径', 404)
        return parsed.path

    def do_GET(self):
        try:
            path = self.guard()
            if path == '/':
                self.send_content(200, PAGE.replace('__TOKEN__', html.escape(self.server.token, quote=True)).encode('utf-8'), 'text/html; charset=utf-8')
                return
            if path == '/api/config':
                self.send_json(200, {'csrf_token': self.server.token})
                return
            with self.server.change_lock:
                match = re.fullmatch(r'/api/tasks/([^/]+)', path)
                if match:
                    self.send_json(200, self.server.status(match[1]))
                    return
                match = re.fullmatch(r'/artifacts/([^/]+)/(.+)', path)
                if match:
                    task_id, name = match.groups()
                    self.server.folder(task_id)
                    if name not in ARTIFACTS:
                        raise WebError('产物不在公开列表', 404)
                    target = safe_path(self.server.root, task_id, 'task', name)
                    if not target.is_file():
                        raise WebError('产物不存在', 404)
                    if target.stat().st_size > 30_000_000:
                        raise WebError('产物过大', 413)
                    mime = 'image/png' if name.endswith('.png') else 'application/json; charset=utf-8'
                    self.send_content(200, target.read_bytes(), mime)
                    return
            raise WebError('页面不存在', 404)
        except WebError as exc:
            self.send_json(exc.code, exc.public())
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self.send_json(500, WebError('本地状态读取失败', 500, 'local_state_failed').public())

    def do_POST(self):
        try:
            path = self.guard()
            tokens = self.headers.get_all('X-CSRF-Token', [])
            if len(tokens) != 1 or not secrets.compare_digest(tokens[0], self.server.token):
                raise WebError('CSRF 令牌无效，请刷新网页', 403)
            if self.headers.get('Transfer-Encoding'):
                raise WebError('不支持分块上传', 400)
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,10}', lengths[0]):
                raise WebError('必须提供唯一有效 Content-Length', 411)
            length = int(lengths[0])
            if length > MAX_BODY:
                raise WebError('请求过大', 413)
            if self.headers.get_content_type() != 'application/json':
                raise WebError('仅支持 application/json', 415)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise WebError('请求不完整')
            try:
                data = strict_object(json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
            except (ValueError, UnicodeError, RecursionError):
                raise WebError('JSON 无效') from None
            # 所有任务变更及读取序列化，避免 CLI 与覆盖输入竞争。
            with self.server.change_lock:
                if path == '/api/tasks':
                    result = self.server.create(data)
                    self.send_json(201, result)
                    return
                match = re.fullmatch(r'/api/tasks/([^/]+)/resume', path)
                if not match:
                    raise WebError('接口不存在', 404)
                self.send_json(200, self.server.resume(match[1], data))
        except WebError as exc:
            self.close_connection = True
            self.send_json(exc.code, exc.public())
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            self.close_connection = True
            self.send_json(500, WebError('本地任务处理失败', 500, 'local_state_failed').public())

    def do_OPTIONS(self):
        self.send_json(405, {'error': '不支持跨域访问'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error('port must be 0–65535')
    server = RideServer(args.port)
    print('本地网页：http://127.0.0.1:'+str(server.server_port)+' （Ctrl+C 停止）', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
