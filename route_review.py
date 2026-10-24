"""有界语义复核协议：只观察，不改像素、不推断骑行顺序。"""
import base64
import hashlib
import io
import json
from pathlib import Path
from PIL import Image

FILES = ('input.png', 'extraction/mask.png', 'extraction/overlay.png',
         'centerline/skeleton.png', 'centerline/graph-overlay.png')
MAX_PREVIEWS = 6
MAX_REVIEWS = 6
MAX_REEXTRACTIONS = 3


def safe_file(folder, relative):
    root = Path(folder).resolve()
    path = root / relative
    if not path.resolve().is_relative_to(root) or path.stat().st_size > 20_000_000:
        raise ValueError('复核文件越界或过大')
    return path


def version(state, folder):
    """同时绑定像素和叠图；即便文件被旁路修改也不能沿用旧复核。"""
    if not state.get('extracted') or not state.get('centerlined'):
        return None
    digest = hashlib.sha256(json.dumps(state.get('extraction_config'), sort_keys=True).encode())
    for relative in FILES:
        digest.update(relative.encode())
        digest.update(safe_file(folder, relative).read_bytes())
    return digest.hexdigest()


def region_box(region, size):
    if (not isinstance(region, list) or len(region) != 4
            or any(type(x) is not int for x in region)
            or not 0 <= region[0] < region[2] <= size[0]
            or not 0 <= region[1] < region[3] <= size[1]
            or region[2]-region[0] > 512 or region[3]-region[1] > 512):
        raise ValueError('局部观察需原图整数坐标，单边不超过512像素')
    return region


def text(value, limit=1500):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('复核说明为空或过长')
    return value


def observe(state, folder, action):
    if set(action) != {'action', 'region', 'hypothesis', 'reason'}:
        raise ValueError('局部观察参数不合法')
    box = region_box(action['region'], state['image_size'])
    hypothesis = action['hypothesis']
    if hypothesis not in ('turnback', 'two_arms', 'occlusion', 'repeated_order', 'uncertain'):
        raise ValueError('未知结构假设')
    text(action['reason'])
    if state.get('review_preview_count', 0) >= MAX_PREVIEWS:
        return None
    # 仅原图范围，不把缩放预览坐标冒充原始像素。
    crop = state.get('extraction_config', {}).get('crop')
    if crop and not (crop[0] <= box[0] < box[2] <= crop[2]
                     and crop[1] <= box[1] < box[3] <= crop[3]):
        raise ValueError('局部观察必须位于当前提取框内')
    state['review_preview_count'] = state.get('review_preview_count', 0)+1
    result = {'region_original_xyxy': box, 'hypothesis': hypothesis,
              'reason': action['reason'], 'version': version(state, folder),
              'extraction_origin_xy': crop[:2] if crop else None,
              'coordinate_rule': '原图坐标=提取局部坐标+extraction_origin_xy；局部预览像素=原图坐标-region左上角；1:1无缩放'}
    state['review_region'] = result
    return result


def local_feedback(state, folder):
    observation = state.get('review_region')
    if not observation or observation['version'] != version(state, folder):
        return []
    box = observation['region_original_xyxy']
    crop = state.get('extraction_config', {}).get('crop')
    content = [{'type': 'text', 'text': 'LOCAL_REVIEW_EVIDENCE '+json.dumps(observation, ensure_ascii=False)}]
    for relative in ('input.png', 'extraction/overlay.png', 'centerline/graph-overlay.png'):
        if relative != 'input.png':
            flag = 'centerlined' if relative.startswith('centerline/') else 'extracted'
            if not crop or not state.get(flag):
                continue
        local_box = box if relative == 'input.png' else [box[0]-crop[0], box[1]-crop[1], box[2]-crop[0], box[3]-crop[1]]
        with Image.open(safe_file(folder, relative)) as image:
            if image.width*image.height > 4_000_000:
                raise ValueError('复核图片像素过大')
            preview = image.convert('RGB').crop(local_box)
            buffer = io.BytesIO()
            preview.save(buffer, format='PNG')
        content.extend([{'type': 'text', 'text': relative+' 局部1:1证据（非全图坐标）'},
                        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()}}])
    return content


def record(state, folder, action):
    if set(action) != {'action', 'version', 'result', 'observations', 'limitations'}:
        raise ValueError('复核参数不合法')
    current = version(state, folder)
    if not current or action['version'] != current or state.get('review_presented_version') != current:
        return {'tool': 'review_route', 'status': 'rejected', 'reason': '当前原图/掩码/中心线证据尚未呈现，或版本已失效'}
    if action['result'] not in ('consistent', 'needs_revision', 'ambiguous'):
        raise ValueError('复核结论不合法')
    observations = action['observations']
    if not isinstance(observations, list) or not 1 <= len(observations) <= 8:
        raise ValueError('需1至8条带原图区域的结构观察')
    presented = state.get('review_presented_regions', {})
    for item in observations:
        if not isinstance(item, dict) or set(item) != {'region', 'hypothesis', 'evidence'}:
            raise ValueError('观察需区域、假设和图像证据')
        region_box(item['region'], state['image_size'])
        if presented.get(current, []).count(item['region']) == 0:
            return {'tool': 'review_route', 'status': 'rejected',
                    'reason': '观察区域尚未通过observe_region呈现当前版本1:1局部证据，不能仅填写区域和版本过门'}
        if item['hypothesis'] not in ('turnback', 'two_arms', 'occlusion', 'repeated_order', 'uncertain'):
            raise ValueError('未知结构假设')
        text(item['evidence'])
    text(action['limitations'])
    previous = state.get('route_review', {})
    # 同参数重跑不等于新证据；保留已质疑哈希，避免清状态后原地改口。
    disputed = state.setdefault('review_disputed_versions', [])
    if action['result'] in ('needs_revision', 'ambiguous') and current not in disputed:
        disputed.append(current)
    if action['result'] == 'consistent' and current in disputed:
        return {'tool': 'review_route', 'status': 'rejected',
                'reason': '此像素版本已报告不符或歧义；重复提取相同内容不是新证据，请调整或ask'}
    if (action['result'] == 'consistent' and previous.get('version') == current
            and previous.get('result') in ('needs_revision', 'ambiguous')):
        return {'tool': 'review_route', 'status': 'rejected',
                'reason': '已记录当前版本存在不符或歧义；须重提取产生新证据或ask，不能只改口确认'}
    state['route_review'] = {k: action[k] for k in ('version', 'result', 'observations', 'limitations')}
    return {'tool': 'review_route', 'status': 'recorded', **state['route_review'], 'verified': False, 'original_gps': False}


def approved(state, folder):
    review = state.get('route_review', {})
    current = version(state, folder)
    return bool(current and review.get('version') == current
                and state.get('review_presented_version') == current
                and review.get('result') == 'consistent')
