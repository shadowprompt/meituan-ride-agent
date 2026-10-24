"""本地人工有序像素路线：不调用模型、不吸附、不推断地理位置。"""
import base64
import binascii
import hashlib
from io import BytesIO
import json
import math
import os
import uuid

from PIL import Image, ImageOps

MAX_POINTS = 10000
NOTICE = '人工点击近似折线；顺序和重复经过由用户提供，未经真实行程验证。不是 GPS 或可导航 GPX，不含时间和速度。'


def image_info(raw):
    """按内容验证格式；绑定 EXIF 转正后的原图像素空间。"""
    from ride_web import WebError
    try:
        with Image.open(BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG', 'WEBP'):
                raise ValueError()
            if image.width * image.height > 4_000_000:
                raise ValueError()
            image.load()
            oriented = ImageOps.exif_transpose(image)
            width, height = oriented.size
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise WebError('仅接受有效 PNG/JPEG/WebP，最多 400 万像素') from None
    return {'sha256': hashlib.sha256(raw).hexdigest(), 'width': width, 'height': height,
            'orientation': 'exif_transposed', 'hash_scope': 'original_file_bytes'}


def coordinate(value, limit):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= limit - 1


def validate_geometry(points, crop, image):
    from ride_web import WebError
    w, h = image['width'], image['height']
    if not isinstance(points, list) or len(points) > MAX_POINTS:
        raise WebError('有序点须为数组，最多 10000 点')
    for point in points:
        if not isinstance(point, list) or len(point) != 2 or not all(coordinate(v, n) for v, n in zip(point, (w, h))):
            raise WebError('点坐标必须是转正原图范围内的有限数值 [x,y]')
    if not isinstance(crop, list) or len(crop) != 4 or any(type(n) is not int for n in crop):
        raise WebError('显示裁剪须为整数 [左,上,右,下]')
    left, top, right, bottom = crop
    if not (0 <= left < right <= w and 0 <= top < bottom <= h):
        raise WebError('显示裁剪超出原图范围')


def document(image, points, crop, revision):
    # 点和线段不去重，闭环、同路往返、零长度停留均按用户输入保留。
    return {'schema': 'manual-ordered-pixel-route/v1', 'source': 'manual_click_approximation',
            'image': image, 'coordinate_space': {'name': 'exif_transposed_original_image_pixels',
            'origin': 'top_left', 'x_axis': 'right', 'y_axis': 'down',
            'crop_transform': 'original_x = crop_x + left; original_y = crop_y + top'},
            'view_crop': crop, 'points': points,
            'segments': [{'from': i, 'to': i + 1} for i in range(max(0, len(points)-1))],
            'repeated_points_and_segments_allowed': True, 'verified': False, 'original_gps': False,
            'notice': NOTICE, 'revision': revision,
            'audit': {'method': 'manual_ordered_clicks', 'automatic_snapping': False,
                      'shortest_path_inference': False, 'revision': revision}}


def atomic_json(server, task_id, name, value):
    from ride_web import safe_path
    target = safe_path(server.root, task_id, name)
    pending = safe_path(server.root, task_id, name + '.pending')
    with pending.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(pending, target)


def create(server, data):
    from ride_web import WebError, text, MAX_IMAGE, safe_path
    if set(data) != {'image_base64'}:
        raise WebError('人工入口只接受截图，不接受 API、路径或地理坐标参数')
    encoded = text(data['image_base64'], '截图 base64', ((MAX_IMAGE+2)//3)*4)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise WebError('截图 base64 无效') from None
    if not raw or len(raw) > MAX_IMAGE:
        raise WebError('截图须为 1–20 MB')
    image = image_info(raw)
    task_id = str(uuid.uuid4())
    folder = safe_path(server.root, task_id)
    folder.mkdir(mode=0o700)
    safe_path(server.root, task_id, 'manual-source').write_bytes(raw)
    route = document(image, [], [0, 0, image['width'], image['height']], 0)
    atomic_json(server, task_id, 'manual-route.json', route)
    # 审计初始绑定，独立于可修改的工作副本。
    atomic_json(server, task_id, 'manual-audit.json', [{'revision': 0, 'source': route['source'], 'image': image}])
    return response(task_id, route)


def load(server, task_id):
    from ride_web import WebError, safe_path
    server.folder(task_id)
    try:
        route = json.loads(safe_path(server.root, task_id, 'manual-route.json').read_text('utf-8'))
        raw = safe_path(server.root, task_id, 'manual-source').read_bytes()
        if image_info(raw) != route['image']:
            raise WebError('原图绑定已变化，拒绝继续或导出', 409)
        validate_geometry(route['points'], route['view_crop'], route['image'])
        return document(route['image'], route['points'], route['view_crop'], route['revision'])
    except (OSError, ValueError, KeyError, TypeError):
        raise WebError('人工路线不存在或已损坏', 409) from None


def response(task_id, route):
    return {'task_id': task_id, 'route': route, 'image_url': '/api/pixel-routes/' + task_id + '/image',
            'export_url': '/api/pixel-routes/' + task_id + '/export'}


def save(server, task_id, data):
    from ride_web import WebError, safe_path
    if set(data) != {'image', 'points', 'view_crop', 'revision'}:
        raise WebError('保存参数不正确，不接受任意路径或自定义导出字段')
    route = load(server, task_id)
    if data['image'] != route['image'] or type(data['revision']) is not int or data['revision'] != route['revision']:
        raise WebError('图像绑定或保存版本已变化，请重新打开核对', 409)
    validate_geometry(data['points'], data['view_crop'], route['image'])
    updated = document(route['image'], data['points'], data['view_crop'], route['revision'] + 1)
    audit = json.loads(safe_path(server.root, task_id, 'manual-audit.json').read_text('utf-8'))
    if len(audit) >= 1000:
        raise WebError('任务保存次数达到 1000 次上限，请导出并新建任务')
    audit.append({'revision': updated['revision'], 'source': updated['source'], 'image': updated['image'],
                  'point_count': len(updated['points']),
                  'geometry_sha256': hashlib.sha256(json.dumps([updated['points'], updated['view_crop']],
                                                               separators=(',', ':')).encode()).hexdigest()})
    # 先落审计再替换工作副本；异常中断可能多一条审计，不丢失已保存路线。
    atomic_json(server, task_id, 'manual-audit.json', audit)
    atomic_json(server, task_id, 'manual-route.json', updated)
    return response(task_id, updated)


def image_bytes(server, task_id):
    from ride_web import safe_path
    load(server, task_id)
    raw = safe_path(server.root, task_id, 'manual-source').read_bytes()
    with Image.open(BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened).convert('RGB')
        out = BytesIO()
        image.save(out, format='PNG')
    return out.getvalue()
