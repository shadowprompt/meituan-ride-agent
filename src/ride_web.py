"""Loopback-only Chinese UI. Run: python src/ride_web.py --port 8765.

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

from ride_errors import GUIDANCE as STRUCTURED_GUIDANCE, safe_diagnostic

SOURCE_DIR = Path(__file__).resolve().parent
# 源码与数据根目录分开，迁移后继续读取原有 outputs/web 任务。
BASE = SOURCE_DIR.parent
MAX_IMAGE = 20_000_000
MAX_BODY = 44_000_000  # 30 MB OSM XML plus JSON overhead
ARTIFACTS = frozenset((
    'inspection/colors.json', 'inspection/color-preview.png',
    'extraction/question-source.png', 'extraction/question-overlay.png',
    'extraction/mask.png', 'extraction/overlay.png', 'extraction/extraction.json',
    'extraction/candidates-mask.png', 'extraction/components-overlay.png',
    'extraction/source-crop.png', 'extraction/component-ids.png',
    'extraction/structure-diagnostics.png',
    'centerline/skeleton.png', 'centerline/graph-overlay.png', 'centerline/pixel-graph.json',
    'registration/registration.json', 'registration/registered-segments.geojson',
    'roads/road-match.json', 'roads/road-candidates.geojson', 'roads/road-overlay.png',
))
UUID_RE = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')


def allowed_artifact(name):
    """动态目录只允许服务端 UUID 与固定文件名/有界孔编号。"""
    from web_hole_review import AUDIT_NAMES
    if name in ARTIFACTS:
        return True
    parts = name.split('/')
    if len(parts) != 3 or not UUID_RE.fullmatch(parts[1]):
        return False
    if parts[0] == 'hole-reviews':
        return parts[2] in ('numbered.png','review.json') or bool(re.fullmatch(r'hole-(?:[1-9]|[1-9][0-9]|1[01][0-9]|12[0-8])\.png', parts[2]))
    return parts[0] == 'hole-audit' and parts[2] in AUDIT_NAMES


def artifact_current(state, name):
    """旧文件可留作审计，但网页必须逐项标记失效，不能只靠统一免责声明。"""
    prefix = name.split('/')[0]
    if prefix in ('hole-reviews', 'hole-audit'):
        return False  # 独立审计，不冒充当前提取产物
    if prefix == 'inspection':
        return bool(state.get('colors_inspected'))
    valid = bool(state.get('extracted'))
    if name in ('extraction/question-source.png', 'extraction/question-overlay.png'):
        return bool(valid and state.get('question_evidence'))
    if prefix == 'extraction':
        if state.get('hole_correction') and name in ('extraction/component-ids.png', 'extraction/candidates-mask.png', 'extraction/components-overlay.png', 'extraction/structure-diagnostics.png'):
            return False
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
    'task_processing_failed': '任务处理未完成，当前证据不足以确定原因；不要仅凭无产物反复修改颜色/裁剪。请先刷新状态，必要时提供脱敏 error_category、error_stage 与错误类型，不要发送密钥、环境文件或完整任务。',
    'cli_failed': '执行未完成；请刷新任务状态，核对截图格式、输入文件和服务端模型配置后重试。',
    'api_timeout': '模型 API 连接、写入或读取超时；已有提取不会失效。可勾选仅本地继续生成中心线，无需重新上传；需模型时再授权重试。',
    'api_network': '模型 API 网络连接失败；已有结果已保存。可仅本地继续；需要视觉判断时检查服务配置后重新授权。',
    'timeout': '运行超时；任务可能已保存，请先刷新状态，避免重复提交，再重新授权继续。',
    'request_rejected': '请求未接受；请按提示修正输入或刷新网页后重试。',
    'local_state_failed': '本地状态不可用；请刷新状态或创建新任务，必要时由服务端管理员检查。',
}

GUIDANCE.update(STRUCTURED_GUIDANCE)


def processing_category(state):
    """优先使用固定错误代码；旧任务缺少结构化证据时不按产物缺失猜原因。"""
    category = state.get('error_category')
    return category if isinstance(category, str) and category in GUIDANCE else 'task_processing_failed'


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

    def __init__(self, port=8765, *, output_root=None, cli_timeout=300):
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
            result = subprocess.run([sys.executable, str(SOURCE_DIR/'ride_agent.py'), *args],
                                    cwd=str(BASE), shell=False, capture_output=True,
                                    timeout=self.cli_timeout, check=False)
        except subprocess.TimeoutExpired:
            raise WebError('运行超时，任务可能已保存；请刷新状态后再继续', 504, 'timeout') from None
        # 不向页面返回 stdout/stderr：异常中可能包含密钥、远端响应或本地路径。
        if result.returncode:
            category = 'cli_failed'
            # 仅解析 CLI 自产的固定代码，不公开或匹配 stderr 原文。
            try:
                if len(result.stderr or b'') <= 4096:
                    report = json.loads(result.stderr)
                    code = report.get('error_category')
                    if isinstance(code, str) and code in GUIDANCE:
                        category = code
            except (ValueError, TypeError, AttributeError):
                pass
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
                elif category == 'cli_failed' and state.get('status') == 'error':
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
        fields = ('candidate_decision', 'question_evidence', 'route_review', 'component_selection', 'mode', 'goal', 'status', 'question', 'summary', 'warnings',
                  'registration_notice', 'road_notice', 'verified', 'original_gps', 'hole_correction', 'hole_proposal')
        result = {key: state[key] for key in fields if key in state}
        if state.get('error') or state.get('status') == 'error':
            category = processing_category(state)
            result.update(error='任务处理错误（详细错误不在网页公开）',
                          error_category=category, recovery_hint=GUIDANCE[category])
            stage = state.get('error_stage')
            if isinstance(stage, str) and stage in ('planner','action','inspect_colors','extract','centerline','register','fetch_map','match'):
                result['error_stage'] = stage
            diagnostic = safe_diagnostic(state.get('error_diagnostic'))
            if diagnostic:
                result['error_diagnostic'] = diagnostic
        evidence = result.get('question_evidence')
        if evidence:
            import candidate_review
            import route_review
            try:
                valid = (evidence.get('candidate_version') == candidate_review.version(state, folder/'task')
                         and evidence.get('review_version') == route_review.version(state, folder/'task'))
            except (ValueError, OSError):
                valid = False
            if not valid:
                result.pop('question_evidence', None)
                state = dict(state, question_evidence=None)
        # 旧任务没有execution也能安全显示；只按当前证据版本认可复核，不按mode猜测。
        import route_review
        execution = state.get('execution', {})
        try:
            current_version = route_review.version(state, folder/'task')
        except (ValueError, OSError, KeyError, TypeError):
            current_version = None
        review = state.get('route_review', {})
        result['execution'] = {'source':execution.get('source', 'local' if state.get('semantic_review_required') is False else 'unknown'),
                               'api_responses':execution.get('api_responses', 0)}
        result['review_status'] = (review.get('result', 'not_reviewed') if current_version and review.get('version') == current_version else 'not_reviewed')
        result['centerlined'] = bool(state.get('centerlined'))
        result['user_components_applied'] = 'component_ids' in state.get('user_extraction_constraints', {})
        result['task_id'] = task_id
        available = []
        for name in sorted(ARTIFACTS):
            if safe_path(self.root, task_id, 'task', name).is_file():
                available.append({'name': name, 'url': '/artifacts/'+task_id+'/'+name,
                                  'current': artifact_current(state,name)})
        candidate = state.get('hole_review')
        if candidate:
            from review_mask_holes import load_inputs
            valid = False
            if state.get('extracted') and not candidate.get('applied'):
                try:
                    source = safe_path(self.root, task_id, 'task', 'extraction/source-crop.png')
                    mask = safe_path(self.root, task_id, 'task', 'extraction/mask.png')
                    valid = load_inputs(source, mask)[2] == candidate['fingerprint']
                except (ValueError, OSError):
                    pass
            result['hole_review'] = dict(candidate, current=valid)
        for prefix in ('hole-reviews', 'hole-audit'):
            directory = safe_path(self.root, task_id, 'task', prefix)
            if directory.is_dir():
                for sub in sorted(directory.iterdir()):
                    safe_path(self.root, task_id, 'task', prefix, sub.name)
                    if not UUID_RE.fullmatch(sub.name) or not sub.is_dir():
                        continue
                    for item in sorted(sub.iterdir()):
                        name = prefix+'/'+sub.name+'/'+item.name
                        if allowed_artifact(name) and safe_path(self.root, task_id, 'task', name).is_file():
                            available.append({'name': name, 'url': '/artifacts/'+task_id+'/'+name, 'current': False})
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
        if mode == 'api':
            # 仅校验当前网页进程继承的配置，不发请求、不读取 .env、不输出配置值。
            from ride_agent import APIPlanner
            from ride_errors import TaskFailure
            try:
                APIPlanner()
            except TaskFailure as exc:
                raise WebError('模型服务配置未就绪，未创建任务或上传截图', 422, exc.category) from None
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
        allowed = {'component_ids', 'answer', 'color', 'crop', 'controls_json', 'osm_xml',
                   'fetch_map', 'consent_map', 'consent_upload', 'local_only', 'review_continue'}
        if data.keys() - allowed:
            raise WebError('未知继续参数（不接受文件路径）')
        folder = self.folder(task_id)
        self.tree_safe(folder)
        state = self.status(task_id)
        if state.get('hole_proposal', {}).get('status') == 'pending':
            raise WebError('请先批准或拒绝当前补孔提议；不可更改参数绕过', 409)
        upload = flag(data, 'consent_upload')
        local_only = flag(data, 'local_only')
        review_continue = flag(data, 'review_continue')
        if review_continue:
            if state['mode'] != 'api' or local_only:
                raise WebError('模型复核仅用于 API 任务，不可同时选择仅本地', 409)
            if data.keys() - {'review_continue', 'consent_upload'}:
                raise WebError('复核使用当前已保存证据，不接受同时修改参数或选择')
        consent_map = flag(data, 'consent_map')
        fetch_map = flag(data, 'fetch_map')
        if state['mode'] == 'api' and not upload and not local_only:
            raise WebError('真实 API 每次继续均需重新同意上传', 403)
        if fetch_map and not consent_map:
            raise WebError('下载地图须明确同意发送范围坐标', 403)
        if fetch_map and 'osm_xml' in data:
            raise WebError('地图下载与本地 OSM 不能同时选择')
        args = ['resume', '--task-dir', str(folder/'task')]
        if local_only:
            args.append('--local-only')
        if review_continue:
            args.append('--review-continue')
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
        if 'component_ids' in data:
            ids = data['component_ids']
            if (not isinstance(ids, list) or not 1 <= len(ids) <= 1000
                    or any(type(n) is not int or n < 1 for n in ids) or len(set(ids)) != len(ids)):
                raise WebError('请选择不重复的候选编号（可多个）')
            if 'color' in data or 'crop' in data:
                raise WebError('请先修改颜色/裁剪并查看新候选，再选择编号')
            args += ['--component-ids', *map(str, ids)]
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
        if not any(k in data for k in ('component_ids', 'answer', 'color', 'crop', 'controls_json', 'osm_xml')) and not fetch_map and not local_only and not review_continue:
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

    def holes(self, task_id, action, data):
        from web_hole_review import preview, apply
        from hole_proposal import decide
        folder = self.folder(task_id)
        self.tree_safe(folder)
        allowed = ({'proposal_id','decision','confirm_source_reviewed','reason'} if action == 'decide' else set() if action == 'preview' else {'review_id','fingerprint','fill_hole_ids','confirm_source_reviewed'})
        if data.keys() - allowed:
            raise WebError('未知逐孔复核参数；不接受路径或画笔坐标')
        try:
            from ride_agent import load
            if action != 'decide' and load(folder/'task').get('hole_proposal', {}).get('status') == 'pending':
                raise ValueError('请先处理当前模型提议')
            if action == 'decide':
                decide(folder/'task', data)
            elif action == 'preview':
                preview(folder/'task')
            else:
                apply(folder/'task', data)
        except ValueError as exc:
            # 仅返回本地固定校验提示，不回显异常或路径。
            raise WebError('逐孔操作未接受：候选过期、未确认、编号无效或输入超限；请刷新并重新生成预览', 409) from None
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
<fieldset><legend>2. 任务状态</legend><label>任务编号 <input id="task" type="text" placeholder="服务器生成的 UUID，可粘贴恢复"></label><button id="refresh">刷新状态</button><p id="executionNotice" role="status" style="font-weight:bold;border:2px solid #b66;padding:12px"></p><pre id="status">尚未创建任务</pre><h3>当前问题</h3><pre id="question"></pre><div id="questionEvidence"></div><h3>模型候选决定（未验证，非用户确认）</h3><pre id="candidateDecision"></pre></fieldset>
<fieldset><legend>3. 回答 / 修正 / 上传证据</legend><label>回答（Demo 首次可填 {"crop":[0,0,80,60],"color":"#00BFA5"}）<textarea id="answer"></textarea></label>
<fieldset><legend>候选像素区域 · 可视多选</legend>
<p>原图裁剪与精确候选像素，不是只有包围框。点击像素或“查看”高亮该候选、淡化其余，并查看局部放大。绿色保留，橙色排除；勾选立即本地预览，不请求模型、不计算中心线。选择不是路线真实性确认，不连接断点、不确定路径顺序或 GPS。</p>
<p id="componentNotice" role="status" aria-live="polite"></p>
<canvas id="componentCanvas" aria-label="原图裁剪与候选像素总览；也可用下方查看按钮键盘操作" style="max-width:100%;height:auto"></canvas>
<button id="clearFocus" type="button">查看全部候选</button>
<h4 id="zoomTitle">局部放大（先查看某个候选）</h4><div style="overflow:auto;max-height:480px"><canvas id="componentZoom" aria-labelledby="zoomTitle" style="image-rendering:pixelated"></canvas></div>
<div id="components"></div><button id="applyComponents" type="button" disabled>应用选择并重新生成</button>
<p>仅本地提交选中编号并重新生成，不上传、不下载地图；不会提交回答或颜色/裁剪修改。未选区域仍保留审计，空选不可提交。</p>
<div id="reviewEntry" hidden><p>API 模式不代表每步已调用模型。继续将使用当前任务已保存的候选选择与图像证据，不重新上传文件、不需要回答或重复选择；可能提出歧义问题或待批准补孔提议。</p>
<label class="consent"><input id="reviewConsent" type="checkbox">仅本次同意上传当前任务截图、派生预览、诊断及对话供模型复核（不继承创建授权；完成或失败后取消）</label>
<button id="continueReview" type="button">授权模型复核并继续</button></div></fieldset>
<fieldset><legend>逐孔人工复核 · 当前任务内纠错</legend>
<p>先生成当前原图/掩码绑定预览，默认全部保留。每孔三联图依次为未叠加原图、精确孔洞标红、二值掩码，可点击图片打开原尺寸放大。只有明确的漏选才勾选；真实回转孔、相邻臂间隙、边界混合孔请保留。暂不支持局部画笔，不能整孔安全填时不要选择。不连接图标开放断裂，不按环数最小化自动修复。</p>
<div id="holeProposal"></div><button id="previewHoles" type="button">生成当前任务逐孔预览</button><p id="holeNotice" role="status"></p><div id="holes"></div>
<label><input id="holeAck" type="checkbox">我已逐孔对照未叠加原图，所选孔是漏选而非真实间隙</label>
<button id="applyHoles" type="button" disabled>应用选中补孔并重新生成中心线</button>
<p>本地操作，不上传、不下载地图。仅增加选中封闭小孔，不删除原前景；前后掩码、原图、选择与中心线独立审计。已应用 mask 修正不代表验证了路线顺序或 GPS。</p></fieldset>
<label>修正颜色 <input id="color" type="text" placeholder="#00BFA5，空白不修改"></label>
<label>修正裁剪 <input id="crop" type="text" placeholder="[左,上,右,下]，空白不修改"></label>
<p>裁剪变更会使旧控制点失效。控制点必须使用当前裁剪坐标，网页不会自动地理定位。</p>
<label>控制点 JSON <input id="controls" type="file" accept=".json,application/json"></label><label>本地 OSM XML <input id="osm" type="file" accept=".osm,.xml"></label>
<p id="roadsNotice">注意：勾选下载地图或上传本地 OSM 后，点击“继续处理”会将任务目标自动切换为 roads（道路候选匹配），即使原目标为像素提取或配准。下载须先完成配准并通过独立检查；下载授权与 API 上传授权相互独立，未勾选不会下载。</p>
<label><input id="fetch" type="checkbox">下载当前配准范围的地图（不可同时上传 OSM）</label><label><input id="mapConsent" type="checkbox">仅本次同意向 Overpass 发送地理范围（不发送截图）</label>
<label><input id="localOnly" type="checkbox">仅本地继续（已有颜色/裁剪或有效掩码；不上传、不调用模型，可恢复中心线；地图下载仍单独授权）</label>
<button id="resume">继续处理</button></fieldset><p id="error" role="alert"></p><h2>产物预览</h2><p>失效产物仅提供审计链接，不显示为当前预览。当前产物也未经路线确认。structure-diagnostics.png 黄框仅表示被保守保护的间隙总范围，不表示已识别折返；同路往返次数和先后须人工确认。</p><div id="artifacts"></div>
<p><a href="/pixel-route">无需 API：本地人工有序像素路线标注、保存与导出</a></p>
<script nonce="__TOKEN__">
'use strict';const token='__TOKEN__';const $=id=>document.getElementById(id);
async function request(path,data){const r=await fetch(path,{method:data?'POST':'GET',headers:data?{'Content-Type':'application/json','X-CSRF-Token':token}:{},body:data?JSON.stringify(data):undefined});const d=await r.json();if(!r.ok)throw Error((d.error||'请求失败')+(d.error_category?' ['+d.error_category+']':'')+(d.recovery_hint?'\n'+d.recovery_hint:''));return d;}
function render(d){$('reviewEntry').hidden=d.mode!=='api';$('reviewConsent').checked=false;const source={local:'本地执行',api:'API复核流程',unknown:'历史执行来源未知'}[d.execution?.source]||'执行来源未知';const review={not_reviewed:'尚未模型复核',consistent:'模型已报告当前版本可见一致（非真值验证）',needs_revision:'模型发现当前版本需要修正',ambiguous:'模型报告当前版本存在歧义'}[d.review_status]||'尚未模型复核';$('executionNotice').textContent=(d.execution?.source==='local'&&d.centerlined&&d.user_components_applied?'候选已应用、本地中心线已生成，':'')+review+'。本轮来源：'+source+'；成功模型响应：'+(d.execution?.api_responses||0)+'。成功响应为0不代表未发送原图（可能已发送但未收到有效响应）；mode仅为任务配置；始终未验证、非原始GPS。';$('task').value=d.task_id;$('status').textContent=JSON.stringify(d,null,2);$('question').textContent=typeof d.question==='string'?d.question:JSON.stringify(d.question||'无');renderQuestionEvidence(d);$('candidateDecision').textContent=d.candidate_decision?JSON.stringify(d.candidate_decision.decisions,null,2):'暂无模型候选决定；用户选择优先';renderComponents(d);renderHoles(d);renderHoleProposal(d);$('artifacts').replaceChildren();for(const a of d.artifacts||[]){const div=document.createElement('div'),link=document.createElement('a');link.textContent=(a.current?'[当前未验证] ':'[已失效/仅审计] ')+a.name;link.href=a.url;link.target='_blank';link.rel='noopener';div.append(link);if(a.current&&a.name.endsWith('.png')&&!a.name.endsWith('component-ids.png')){const img=document.createElement('img');img.src=a.url;img.alt=a.name;div.append(document.createElement('br'),img);}$('artifacts').append(div);}}
// 所有预览只读本地白名单产物；编号逐像素解码，不把重叠包围框当成候选。
let componentView=null, componentEpoch=0;
function selectedComponentIds(){return Array.from(document.querySelectorAll('input[name=component]:checked'),b=>Number(b.value));}
function paintComponentPixels(source, labels, selected, focus){
 const out=new Uint8ClampedArray(source);const keep=new Set(selected);
 for(let i=0;i<labels.length;i++){
  const id=labels[i],p=i*4;
  if(focus&&id!==focus){for(let k=0;k<3;k++)out[p+k]=Math.round(source[p+k]*.2+255*.8);}
  else if(id){const color=keep.has(id)?[0,190,100]:[255,130,0];for(let k=0;k<3;k++)out[p+k]=Math.round(source[p+k]*.25+color[k]*.75);}
 }
 return out;
}
function paintComponents(){
 const v=componentView;if(!v)return;
 const selected=selectedComponentIds(),canvas=$('componentCanvas');
 canvas.getContext('2d').putImageData(new ImageData(paintComponentPixels(v.source,v.labels,selected,v.focus),v.w,v.h),0,0);
 $('componentNotice').textContent='本地待应用预览：保留 '+(selected.join('、')||'无')+'；未选像素以橙色标出。尚未重新生成中心线。'+(v.focus?' 当前高亮 #'+v.focus+'，其余淡化。':'');
 $('applyComponents').disabled=!selected.length;
 if(!v.focus){$('componentZoom').width=0;$('zoomTitle').textContent='局部放大（先查看某个候选）';}
 if(v.focus){
  const c=v.candidates.find(c=>c.id===v.focus),b=c.bbox_crop_xyxy;
  const x=Math.max(0,b[0]-12),y=Math.max(0,b[1]-12),w=Math.min(v.w,b[2]+12)-x,h=Math.min(v.h,b[3]+12)-y;
  const zoom=$('componentZoom'),scale=Math.max(1,Math.min(6,Math.floor(900/w)));zoom.width=w*scale;zoom.height=h*scale;
  const ctx=zoom.getContext('2d');ctx.imageSmoothingEnabled=false;ctx.drawImage(canvas,x,y,w,h,0,0,zoom.width,zoom.height);
  $('zoomTitle').textContent='候选 #'+v.focus+' 局部放大 ×'+scale+'（原像素，可滚动）';
 }
}
function renderQuestionEvidence(d){
 const root=$('questionEvidence');root.replaceChildren();if(!d.question_evidence)return;
 const text=document.createElement('p');text.textContent='具体追问区域（原图坐标）'+JSON.stringify(d.question_evidence.region)+' · '+d.question_evidence.notice;root.append(text);
 for(const name of ['extraction/question-source.png','extraction/question-overlay.png']){const a=d.artifacts?.find(x=>x.current&&x.name===name);if(!a)continue;const label=document.createElement('p');label.textContent=name.endsWith('source.png')?'原图局部 · 1:1':'当前候选局部 · 1:1';const scroll=document.createElement('div');scroll.style.cssText='overflow:auto;max-height:520px';const img=document.createElement('img');img.src=a.url;img.alt=label.textContent;img.style.cssText='max-width:none;image-rendering:pixelated';scroll.append(img);root.append(label,scroll);}
}
async function renderComponents(d){
 const epoch=++componentEpoch;componentView=null;$('components').replaceChildren();$('componentCanvas').width=0;$('componentZoom').width=0;$('zoomTitle').textContent='局部放大（先查看某个候选）';$('applyComponents').disabled=true;
 const candidates=d.component_selection?.candidates||[],find=name=>d.artifacts?.find(a=>a.current&&a.name===name);
 const source=find('extraction/source-crop.png'),ids=find('extraction/component-ids.png');
 if(!candidates.length||!source||!ids){$('componentNotice').textContent='暂无当前像素映射预览（旧任务请重新提取颜色/裁剪）；失效产物不可用于选择。';return;}
 $('componentNotice').textContent='正在读取本地精确像素预览…';
 try{
  const load=url=>new Promise((resolve,reject)=>{const im=new Image();im.onload=()=>resolve(im);im.onerror=()=>reject(Error('本地产物读取失败'));im.src=url;});
  const [im,map]=await Promise.all([load(source.url),load(ids.url)]);if(epoch!==componentEpoch)return;
  if(im.width!==map.width||im.height!==map.height||im.width*im.height>12000000)throw Error('预览尺寸无效');
  const canvas=$('componentCanvas');canvas.width=im.width;canvas.height=im.height;const ctx=canvas.getContext('2d');ctx.drawImage(im,0,0);const pixels=ctx.getImageData(0,0,im.width,im.height).data;
  ctx.drawImage(map,0,0);const raw=ctx.getImageData(0,0,map.width,map.height).data,labels=new Uint32Array(im.width*im.height),valid=new Set(candidates.map(c=>c.id));
  for(let i=0;i<labels.length;i++){labels[i]=raw[i*4]+raw[i*4+1]*256+raw[i*4+2]*65536;if(labels[i]&&!valid.has(labels[i]))throw Error('候选编号映射无效');}
  componentView={source:pixels,labels,w:im.width,h:im.height,candidates,focus:0,taskId:d.task_id};
  for(const c of candidates){const row=document.createElement('div'),label=document.createElement('label'),box=document.createElement('input'),view=document.createElement('button');box.type='checkbox';box.name='component';box.value=c.id;box.checked=c.selected;box.onchange=paintComponents;label.append(box,document.createTextNode('保留 #'+c.id+' · '+c.pixels+' 像素 · '+c.decision));view.type='button';view.textContent='查看 #'+c.id+' 并局部放大';view.onclick=()=>{componentView.focus=c.id;paintComponents();};row.append(label,view);$('components').append(row);}
  paintComponents();
 }catch(e){if(epoch===componentEpoch){componentView=null;$('componentNotice').textContent='预览不可用，请刷新或重新提取。';$('applyComponents').disabled=true;}}
}
$('componentCanvas').onclick=e=>{const v=componentView;if(!v)return;const r=e.currentTarget.getBoundingClientRect(),x=Math.floor((e.clientX-r.left)*v.w/r.width),y=Math.floor((e.clientY-r.top)*v.h/r.height);v.focus=v.labels[y*v.w+x]||0;paintComponents();};
$('clearFocus').onclick=()=>{if(componentView){componentView.focus=0;$('componentZoom').width=0;$('zoomTitle').textContent='局部放大（先查看某个候选）';paintComponents();}};
$('applyComponents').onclick=()=>action(async()=>{if(!componentView||componentView.taskId!==$('task').value.trim())throw Error('请先刷新当前任务的像素预览');const ids=selectedComponentIds();if(!ids.length)throw Error('请至少保留一个候选');render(await request(taskPath()+'/resume',{local_only:true,component_ids:ids}));});

function renderHoleProposal(d){
 const root=$('holeProposal');root.replaceChildren();const p=d.hole_proposal;if(!p)return;
 const notice=document.createElement('p');notice.textContent='模型补孔提议 · '+p.status+'：可能误认真实回转孔；未批准前零修改，不连接开放断裂。';root.append(notice);
 for(const h of p.holes){const label=document.createElement('p');label.textContent='仅提议孔 #'+h.id+' · '+h.kind+' · '+h.reason;const img=document.createElement('img');img.src='/artifacts/'+d.task_id+'/hole-reviews/'+p.review_id+'/hole-'+h.id+'.png';img.alt='提议孔 '+h.id+'：原图 / 精确新增像素 / 当前掩码';root.append(label,img);}
 if(p.status!=='pending'){const reason=document.createElement('p');reason.textContent='用户决定理由：'+p.user_reason;root.append(reason);return;}
 const ack=document.createElement('input');ack.type='checkbox';ack.id='proposalAck';const label=document.createElement('label');label.append(ack,document.createTextNode('我已核对以上未叠加原图，确认所有提议孔确为漏选而非真实间隙'));
 const reason=document.createElement('input');reason.type='text';reason.id='proposalReason';reason.placeholder='批准或拒绝的具体理由';reason.setAttribute('aria-label','补孔决定理由');
 const yes=document.createElement('button'),no=document.createElement('button');yes.textContent='批准上述提议并重建中心线';no.textContent='拒绝提议并保留掩码';yes.dataset.proposalApprove='true';yes.disabled=true;ack.onchange=()=>yes.disabled=!ack.checked;
 for(const [button,decision] of [[yes,'approve'],[no,'reject']])button.onclick=()=>action(async()=>{if(!reason.value.trim())throw Error('请填写决定理由');render(await request('/api/tasks/'+d.task_id+'/holes/decide',{proposal_id:p.proposal_id,decision,confirm_source_reviewed:ack.checked,reason:reason.value}));});
 root.append(label,reason,yes,no);
}
let holeView=null;
function holeSelection(){return [...document.querySelectorAll('input[name=hole]:checked')].map(x=>Number(x.value));}
function updateHoleChoice(){
 const ids=holeSelection();for(const box of document.querySelectorAll('input[name=hole]')){box.closest('section').style.border=box.checked?'3px solid #d05':'1px solid #aaa';box.closest('section').style.background=box.checked?'#fff0f5':'';}
 $('applyHoles').disabled=!!$('proposalAck')||!holeView||!ids.length||!$('holeAck').checked;
 if(holeView)$('holeNotice').textContent='本地待应用：'+(ids.length?'仅补孔 '+ids.join('、'):'全部保留（默认）')+'；尚未修改掩码或中心线。';
}
function renderHoles(d){
 holeView=null;$('holes').replaceChildren();$('holeAck').checked=false;$('applyHoles').disabled=true;
 const r=d.hole_review;
 if(!r?.current){$('holeNotice').textContent=r?.applied?'本次修正已应用；旧候选已消费。可生成新预览。':'尚无当前逐孔预览或候选已失效，请先完成提取再生成。';return;}
 holeView={...r,taskId:d.task_id};
 const base='/artifacts/'+d.task_id+'/hole-reviews/'+r.review_id+'/';
 const image=(name,alt)=>{const a=document.createElement('a'),im=document.createElement('img');a.href=base+name;a.target='_blank';a.rel='noopener';im.src=a.href;im.alt=alt;im.style.imageRendering='pixelated';a.append(im);return a;};
 $('holes').append(image('numbered.png','当前孔洞编号总图'));
 for(const h of r.holes){const row=document.createElement('section'),label=document.createElement('label'),box=document.createElement('input');box.type='checkbox';box.name='hole';box.value=h.id;box.disabled=!h.eligible;box.onchange=updateHoleChoice;label.append(box,document.createTextNode('补孔 #'+h.id+' · '+h.area+' 像素 · 框 '+JSON.stringify(h.bbox_xyxy)+(h.eligible?'':'（超界，仅保留）')));row.append(label,image('hole-'+h.id+'.png','孔 '+h.id+'：原图 / 精确红色孔像素 / 掩码放大'));$('holes').append(row);}
 updateHoleChoice();
}
$('holeAck').onchange=updateHoleChoice;
$('previewHoles').onclick=()=>action(async()=>render(await request(taskPath()+'/holes/preview',{})));
$('applyHoles').onclick=()=>action(async()=>{
 if(!holeView||holeView.taskId!==$('task').value.trim()||!$('holeAck').checked)throw Error('请先刷新当前逐孔预览并确认原图');
 render(await request(taskPath()+'/holes/apply',{review_id:holeView.review_id,fingerprint:holeView.fingerprint,fill_hole_ids:holeSelection(),confirm_source_reviewed:true}));
});

async function action(fn){$('error').textContent='处理中，请稍候（最长约 3 分钟）';for(const b of document.querySelectorAll('button'))b.disabled=true;try{await fn();$('error').textContent='';}catch(e){$('error').textContent=e.message;}finally{for(const b of document.querySelectorAll('button'))b.disabled=false;$('applyComponents').disabled=!componentView||!selectedComponentIds().length;$('applyHoles').disabled=!!$('proposalAck')||!holeView||!holeSelection().length||!$('holeAck').checked;$('previewHoles').disabled=!!$('proposalAck');const approve=document.querySelector('[data-proposal-approve]');if(approve)approve.disabled=!$('proposalAck').checked;$('uploadConsent').checked=false;$('reviewConsent').checked=false;$('mapConsent').checked=false;}}
function taskPath(){const id=$('task').value.trim();if(!/^[0-9a-f-]{36}$/.test(id))throw Error('请先创建任务或填写编号');return '/api/tasks/'+id;}
async function fileText(id,max){const f=$(id).files[0];if(!f)return undefined;if(f.size>max)throw Error('上传文件过大');return await f.text();}
$('create').onclick=()=>action(async()=>{const f=$('image').files[0];if(!f||f.size>20000000)throw Error('请选择 ≤20 MB 截图');const b64=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=reject;r.readAsDataURL(f);});render(await request('/api/tasks',{image_base64:b64,mode:$('mode').value,goal:$('goal').value,consent_upload:$('uploadConsent').checked}));});
// 专用继续入口只提交本次授权；不读取localOnly、回答、颜色/裁剪等可能陈旧的表单。
$('continueReview').onclick=()=>action(async()=>{if(!$('reviewConsent').checked)throw Error('请先勾选本次模型复核上传授权；尚未上传');render(await request(taskPath()+'/resume',{review_continue:true,consent_upload:true}));});
$('refresh').onclick=()=>action(async()=>render(await request(taskPath())));
$('resume').onclick=()=>action(async()=>{const d={local_only:$('localOnly').checked,consent_upload:$('uploadConsent').checked,fetch_map:$('fetch').checked,consent_map:$('mapConsent').checked};if($('answer').value)d.answer=$('answer').value;if($('color').value.trim())d.color=$('color').value.trim();if($('crop').value.trim())d.crop=JSON.parse($('crop').value);const controls=await fileText('controls',1000000),osm=await fileText('osm',30000000);if(controls!==undefined)d.controls_json=controls;if(osm!==undefined)d.osm_xml=osm;render(await request(taskPath()+'/resume',d));$('answer').value='';$('color').value='';$('crop').value='';$('controls').value='';$('osm').value='';$('fetch').checked=false;});
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
            if path == '/pixel-route':
                from pixel_route_ui import PAGE as MANUAL_PAGE
                self.send_content(200, MANUAL_PAGE.replace('__TOKEN__', html.escape(self.server.token, quote=True)).encode('utf-8'), 'text/html; charset=utf-8')
                return
            if path == '/api/config':
                self.send_json(200, {'csrf_token': self.server.token})
                return
            with self.server.change_lock:
                manual = re.fullmatch(r'/api/pixel-routes/([^/]+)(?:/(image|export))?', path)
                if manual:
                    import pixel_route
                    task_id, kind = manual.groups()
                    if kind == 'image':
                        self.send_content(200, pixel_route.image_bytes(self.server, task_id), 'image/png')
                    else:
                        route = pixel_route.load(self.server, task_id)
                        if kind == 'export' and len(route['points']) < 2:
                            raise WebError('至少保存两个有序点后才能导出', 409)
                        self.send_json(200, route if kind == 'export' else pixel_route.response(task_id, route))
                    return
                match = re.fullmatch(r'/api/tasks/([^/]+)', path)
                if match:
                    self.send_json(200, self.server.status(match[1]))
                    return
                match = re.fullmatch(r'/artifacts/([^/]+)/(.+)', path)
                if match:
                    task_id, name = match.groups()
                    self.server.folder(task_id)
                    if not allowed_artifact(name):
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
                if path == '/api/pixel-routes':
                    import pixel_route
                    self.send_json(201, pixel_route.create(self.server, data))
                    return
                manual = re.fullmatch(r'/api/pixel-routes/([^/]+)', path)
                if manual:
                    import pixel_route
                    self.send_json(200, pixel_route.save(self.server, manual[1], data))
                    return
                if path == '/api/tasks':
                    result = self.server.create(data)
                    self.send_json(201, result)
                    return
                hole_match = re.fullmatch(r'/api/tasks/([^/]+)/holes/(preview|apply|decide)', path)
                if hole_match:
                    self.send_json(200, self.server.holes(*hole_match.groups(), data))
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
    # 脚本入口也复用同一模块对象，避免扩展导入第二份 WebError 导致异常逃逸。
    sys.modules.setdefault('ride_web', sys.modules[__name__])
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
