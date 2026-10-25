"""候选阶段有界语义观察/保留协议；不编辑像素、不连接片段。"""
import base64
import hashlib
import io
import json
from pathlib import Path
import numpy as np
from PIL import Image
import route_review

FILES = ('input.png', 'extraction/candidates-mask.png', 'extraction/component-ids.png',
         'extraction/source-crop.png', 'extraction/components-overlay.png',
         'extraction/mask.png', 'extraction/overlay.png')


def version(state, folder):
    if not state.get('extracted') or not state.get('component_selection'):
        return None
    digest = hashlib.sha256(json.dumps(state.get('extraction_config'), sort_keys=True).encode())
    for name in FILES:
        digest.update(route_review.safe_file(folder, name).read_bytes())
    return digest.hexdigest()


def observe(state, folder, action):
    if set(action) != {'action', 'region', 'reason'}:
        raise ValueError('候选观察只接受原图区域和理由')
    route_review.text(action['reason'])
    box = route_review.region_box(action['region'], state['image_size'])
    crop = state['extraction_config']['crop']
    if not (crop[0] <= box[0] < box[2] <= crop[2] and crop[1] <= box[1] < box[3] <= crop[3]):
        raise ValueError('候选观察必须在提取框内')
    if not state.get('component_selection', {}).get('requires_selection'):
        raise ValueError('当前没有待决定候选')
    if state.get('candidate_observe_count', 0) >= 8:
        return None
    local = [box[0]-crop[0], box[1]-crop[1], box[2]-crop[0], box[3]-crop[1]]
    with Image.open(route_review.safe_file(folder, 'extraction/component-ids.png')) as image:
        rgb = np.array(image.convert('RGB').crop(local), dtype=np.int32)
    labels = rgb[:,:,0] + (rgb[:,:,1]<<8) + (rgb[:,:,2]<<16)
    ids = [int(x) for x in np.unique(labels) if x]
    result = {'version': version(state, folder), 'region': box, 'visible_ids': ids,
              'reason': action['reason'], 'coordinate_rule': '原图整数坐标；局部图1:1，无缩放'}
    state['candidate_observation'] = result
    state['candidate_observe_count'] = state.get('candidate_observe_count', 0)+1
    return result


def feedback(state, folder):
    observation = state.get('candidate_observation')
    if not observation or observation['version'] != version(state, folder):
        return []
    box = observation['region']
    crop = state['extraction_config']['crop']
    local = [box[0]-crop[0], box[1]-crop[1], box[2]-crop[0], box[3]-crop[1]]
    content = [{'type':'text', 'text':'CANDIDATE_LOCAL_EVIDENCE '+json.dumps(observation, ensure_ascii=False)}]
    for name in ('input.png', 'extraction/components-overlay.png', 'extraction/component-ids.png'):
        with Image.open(route_review.safe_file(folder, name)) as image:
            preview = image.convert('RGB').crop(box if name == 'input.png' else local)
            buffer = io.BytesIO()
            preview.save(buffer, format='PNG')
        content.extend([{'type':'text', 'text':name+' 1:1；component-ids RGB编码编号，非路线顺序'},
                        {'type':'image_url', 'image_url':{'url':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()}}])
    return content


def mark_presented(state, folder):
    observation = state.get('candidate_observation')
    if observation and observation['version'] == version(state, folder):
        records = state.setdefault('candidate_presented', [])
        if observation not in records:
            records.append(dict(observation))


def select(state, folder, action):
    """当前证据送达后才接受逐候选理由；用户锁定优先，模型不能冒充人工选择。"""
    if set(action) != {'action', 'version', 'decisions'}:
        raise ValueError('候选保留协议参数不合法')
    current = version(state, folder)
    selection = state.get('component_selection', {})
    if ('component_ids' in state.get('user_extraction_constraints', {})
            or not selection.get('requires_selection')):
        return {'tool':'select_candidates', 'status':'rejected', 'reason':'用户选择已锁定或当前无需候选决定'}
    if not current or action['version'] != current:
        return {'tool':'select_candidates', 'status':'rejected', 'reason':'候选版本已失效'}
    decisions = action['decisions']
    candidates = selection['candidates']
    if not isinstance(decisions, list) or not 1 <= len(decisions) <= 64:
        raise ValueError('候选决定需1至64条；候选过多时缩小裁剪或具体追问')
    seen = {i for record in state.get('candidate_presented', []) if record['version'] == current for i in record['visible_ids']}
    ids = []
    for item in decisions:
        if not isinstance(item, dict) or set(item) != {'id', 'keep', 'reason'} or type(item['id']) is not int or type(item['keep']) is not bool:
            raise ValueError('每个候选需编号、保留布尔值和理由')
        route_review.text(item['reason'])
        ids.append(item['id'])
    if len(set(ids)) != len(ids) or set(ids) != {c['id'] for c in candidates}:
        raise ValueError('须对当前全部候选逐项决定，不可静默全选')
    if not set(ids) <= seen:
        return {'tool':'select_candidates', 'status':'rejected', 'reason':'先observe_candidates并在后续请求查看每个候选的当前1:1证据'}
    kept = sorted(item['id'] for item in decisions if item['keep'])
    if not kept:
        return {'tool':'select_candidates', 'status':'rejected', 'reason':'没有可信目标时请针对具体区域ask，不得生成空路线'}
    with Image.open(route_review.safe_file(folder, 'extraction/component-ids.png')) as image:
        rgb = np.array(image.convert('RGB'), dtype=np.int32)
    labels = rgb[:,:,0] + (rgb[:,:,1]<<8) + (rgb[:,:,2]<<16)
    coverage = np.zeros(labels.shape, dtype=bool)
    crop = state['extraction_config']['crop']
    for record in state.get('candidate_presented', []):
        if record['version'] == current:
            x0,y0,x1,y1 = record['region']
            coverage[y0-crop[1]:y1-crop[1], x0-crop[0]:x1-crop[0]] = True
    if np.any((labels > 0) & ~coverage):
        return {'tool':'select_candidates', 'status':'rejected', 'reason':'当前1:1分块尚未覆盖全部候选像素；补充观察或具体追问，不以一小块替代完整候选证据'}
    mask = np.isin(labels, kept)
    root = Path(folder)/'extraction'
    with Image.open(route_review.safe_file(folder, 'extraction/source-crop.png')) as image:
        overlay = np.array(image.convert('RGB'))
    overlay[mask] = (overlay[mask]*0.35 + np.array([255,0,80])*0.65).astype(np.uint8)
    Image.fromarray(mask.astype(np.uint8)*255).save(root/'mask.png')
    Image.fromarray(overlay).save(root/'overlay.png')
    selection.update(status='model_candidate', requires_selection=False, selected_ids=kept,
                     excluded_ids=sorted(set(ids)-set(kept)), source='model_unverified',
                     reason='模型有理由保留的未验证像素集合；不是用户明确选择，不连接断点，不代表有序路线。')
    for candidate in candidates:
        candidate['selected'] = candidate['id'] in kept
        candidate['decision'] = '模型保留/未验证' if candidate['selected'] else '模型排除/保留审计'
    audit = {'version':current, 'decisions':decisions, 'evidence':state['candidate_presented'],
             'verified':False, 'original_gps':False}
    state['candidate_decision'] = audit
    report_path = root/'extraction.json'
    report = json.loads(report_path.read_text())
    report.update(component_selection=selection, selected_pixels=int(mask.sum()),
                  components=[c for c in candidates if c['selected']], candidate_decision=audit)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    state.update(centerlined=False, registered=False, match_attempted=False, approximate_complete=False)
    for key in ('route_review', 'review_region', 'review_presented_version', 'review_presented_regions'):
        state.pop(key, None)
    return {'tool':'select_candidates', 'status':'selected_unverified', 'selected_ids':kept,
            'verified':False, 'original_gps':False}


def question_evidence(state, folder, action):
    """具体区域追问须绑定实际送达的当前局部证据；网页直接展示，不要求研究编号。"""
    state.pop('question_evidence', None)
    if 'region' not in action:
        return
    box = route_review.region_box(action['region'], state['image_size'])
    current = version(state, folder)
    presented = any(r['version'] == current and r['region'] == box for r in state.get('candidate_presented', []))
    review_version = route_review.version(state, folder)
    reviewed = bool(review_version and box in state.get('review_presented_regions', {}).get(review_version, []))
    if not presented and not reviewed:
        raise ValueError('具体区域追问须先呈现当前版本1:1证据')
    crop = state['extraction_config']['crop']
    local = [box[0]-crop[0], box[1]-crop[1], box[2]-crop[0], box[3]-crop[1]]
    for source, target in (('input.png','question-source.png'),
                           ('extraction/overlay.png' if reviewed else 'extraction/components-overlay.png','question-overlay.png')):
        with Image.open(route_review.safe_file(folder, source)) as image:
            image.convert('RGB').crop(box if source == 'input.png' else local).save(Path(folder)/'extraction'/target)
    state['question_evidence'] = {'region':box, 'candidate_version':current, 'review_version':review_version,
                                  'notice':'当前原图与候选局部1:1对照；未验证，不代表真实路线或行进顺序'}
