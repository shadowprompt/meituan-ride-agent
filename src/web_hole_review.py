"""网页任务内的逐孔人工复核；只补封闭小孔，不连接开放断点。"""
import json
from pathlib import Path
import shutil
import uuid

import numpy as np
from PIL import Image

from centerline import process
from review_mask_holes import review, load_inputs
from ride_agent import load, save

AUDIT_NAMES = frozenset(('result.json', 'selection.json', 'mask-before.png',
    'mask-corrected.png', 'comparison.png', 'source.png', 'graph-before.json',
    'graph-after.json', 'extraction-before.json', 'overlay-before.png'))


def current_inputs(folder, state):
    if not state.get('extracted') or state.get('component_selection', {}).get('requires_selection'):
        raise ValueError('请先完成当前提取和候选分量选择')
    return folder/'extraction/source-crop.png', folder/'extraction/mask.png'


def preview(folder):
    state = load(folder)
    source, mask = current_inputs(folder, state)
    count = state.get('hole_review_count', 0)
    if count >= 32:
        raise ValueError('本任务已达32次复核上限，请创建新任务')
    review_id = str(uuid.uuid4())
    out = folder/'hole-reviews'/review_id
    review(source, mask, out)
    manifest = json.loads((out/'review.json').read_text())
    state['hole_review_count'] = count+1
    state['hole_review'] = {'review_id': review_id, 'fingerprint': manifest['fingerprint'],
                            'holes': manifest['holes'], 'applied': False}
    save(state, folder)
    return state['hole_review']


def apply(folder, data):
    """先全部校验并在独立审计目录计算，再撤销下游状态、替换当前掩码。"""
    state = load(folder)
    source, mask = current_inputs(folder, state)
    candidate = state.get('hole_review', {})
    if (candidate.get('applied') or data.get('review_id') != candidate.get('review_id')
            or data.get('fingerprint') != candidate.get('fingerprint')):
        raise ValueError('候选已过期或已应用，请重新生成逐孔预览')
    _, _, digest = load_inputs(source, mask)
    if digest != candidate.get('fingerprint'):
        raise ValueError('原图或掩码已改变，请重新生成逐孔预览')
    ids = data.get('fill_hole_ids')
    allowed = {h['id'] for h in candidate['holes'] if h['eligible']}
    if (data.get('confirm_source_reviewed') is not True or not isinstance(ids, list)
            or not 1 <= len(ids) <= 128 or any(type(i) is not int for i in ids)
            or len(set(ids)) != len(ids) or any(i not in allowed for i in ids)):
        raise ValueError('须明确复核原图并选择不重复的可补小孔；默认全部保留')
    audit_id = str(uuid.uuid4())
    out = folder/'hole-audit'/audit_id
    out.mkdir(parents=True)
    selection = out/'selection.json'
    selection.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')
    result = review(source, mask, out, selection)
    graph = process(out/'mask-corrected.png', out/'centerline')
    shutil.copyfile(out/'centerline/pixel-graph.json', out/'graph-after.json')
    for name, dest in (('centerline/pixel-graph.json','graph-before.json'),
                       ('extraction/extraction.json','extraction-before.json'),
                       ('extraction/overlay.png','overlay-before.png')):
        if (folder/name).is_file():
            shutil.copyfile(folder/name, out/dest)
    # 修正后提取报告保留自动证据，但显式标注它不再描述未经修正的当前掩码。
    report_path = folder/'extraction/extraction.json'
    report = json.loads(report_path.read_text())
    corrected = np.array(Image.open(out/'mask-corrected.png')) > 0
    report['selected_pixels'] = int(corrected.sum())
    report['manual_hole_correction'] = {'audit_id': audit_id, **result}
    rgb = np.array(Image.open(source).convert('RGB'))
    rgb[corrected] = (rgb[corrected]*.35+np.array([255,0,80])*.65).astype('uint8')
    Image.fromarray(rgb).save(out/'overlay-after.png')
    # 从此刻起若磁盘写失败，旧下游产物也不能继续冒充当前结果。
    state.update(extracted=False, centerlined=False, registered=False,
                 registration_gate_passed=False, match_attempted=False, verified=False, approximate_complete=False)
    state.pop('summary', None)
    for key in ('route_review','review_region','review_presented_version','review_presented_regions'):
        state.pop(key, None)
    save(state, folder)
    shutil.copyfile(out/'mask-corrected.png', mask)
    shutil.copyfile(out/'overlay-after.png', folder/'extraction/overlay.png')
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    (folder/'centerline').mkdir(exist_ok=True)
    for name in ('skeleton.png','graph-overlay.png','pixel-graph.json'):
        shutil.copyfile(out/'centerline'/name, folder/'centerline'/name)
    candidate['applied'] = True
    state.update(extracted=True, centerlined=graph['status'] != 'no_detection',
        status='needs_confirmation', original_gps=False,
        hole_correction={'audit_id': audit_id, **result},
        summary='已应用人工 mask 修正并重新生成中心线；未验证连接、重复通行顺序或 GPS，不可导航。',
        question='请复核修正后的掩码和中心线。混合边界孔不支持局部画笔，请保留；开放图标断裂不会自动连接。')
    # 原分量候选仍属于修正前，避免页面显示旧像素映射是当前可选结果。
    state.pop('component_selection', None)
    for key in ('road_diagnostics','road_match_status','artifacts','extraction_diagnostics'):
        state.pop(key, None)
    save(state, folder)
    return result
