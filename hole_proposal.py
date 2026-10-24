"""模型只提出有证据的小孔修正；实际改像素始终由网页用户明确批准。"""
import base64
import hashlib
import io
import json
import uuid
from pathlib import Path
from PIL import Image
import route_review


def binding(state, folder):
    if not route_review.version(state, folder):
        raise ValueError('请先生成当前中心线')
    root = Path(folder)
    for parent in (root, *root.rglob('*')):
        if parent.is_symlink():
            raise ValueError('任务树不接受符号链接')
    names = ('extraction/source-crop.png', 'extraction/mask.png', 'centerline/pixel-graph.json')
    result = {name: hashlib.sha256(route_review.safe_file(folder, name).read_bytes()).hexdigest() for name in names}
    result['version'] = route_review.version(state, folder)
    return result


def observe(state, folder, action):
    if set(action) != {'action'}:
        raise ValueError('孔观察不接受路径、坐标或填充参数')
    if state.get('hole_observe_count', 0) >= 6:
        raise ValueError('孔观察已达上限，请具体追问')
    from web_hole_review import preview
    # preview 自行加载状态；先落盘，防止覆盖当前模型历史。
    from ride_agent import save
    save(state, folder)
    folder = Path(folder)
    hashes = binding(state, folder)
    candidate = preview(folder)
    state['hole_review'] = candidate
    state['hole_review_count'] = state.get('hole_review_count', 0)+1
    state['hole_observe_count'] = state.get('hole_observe_count', 0)+1
    result = {'review_id': candidate['review_id'], 'fingerprint': candidate['fingerprint'],
              'hashes': hashes, 'holes': candidate['holes']}
    state['hole_observation'] = result
    return result


def feedback(state, folder):
    observation = state.get('hole_observation')
    if not observation or not route_review.version(state, folder) or observation['hashes'] != binding(state, folder):
        return []
    content = [{'type': 'text', 'text': 'HOLE_LOCAL_EVIDENCE '+json.dumps(observation, ensure_ascii=False)}]
    for hole in observation['holes']:
        name = f"hole-reviews/{observation['review_id']}/hole-{hole['id']}.png"
        with Image.open(route_review.safe_file(folder, name)) as image:
            # 现有人工预览为最近邻4倍；还原为原始像素大小，三栏分别原图/新增像素/当前掩码。
            image = image.resize((image.width//4, image.height//4), Image.Resampling.NEAREST)
            buffer = io.BytesIO(); image.save(buffer, format='PNG')
        content += [{'type':'text', 'text':f"孔 #{hole['id']}，三栏1:1：未叠加原图 / 精确新增像素 / 当前掩码；不是通行顺序"},
                    {'type':'image_url', 'image_url':{'url':'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode()}}]
    return content


def propose(state, folder, action):
    if set(action) != {'action', 'review_id', 'fingerprint', 'hashes', 'holes'}:
        raise ValueError('提议参数不合法')
    observation = state.get('hole_observation')
    if (not observation or any(action[k] != observation[k] for k in ('review_id','fingerprint','hashes'))
            or action['hashes'] != binding(state, folder)
            or state.get('hole_presented') != observation):
        raise ValueError('须先在API请求中查看当前孔的精确原图证据，旧哈希不可用')
    holes = action['holes']
    allowed = {h['id'] for h in observation['holes'] if h['eligible']}
    if not isinstance(holes, list) or not 1 <= len(holes) <= 128:
        raise ValueError('提议需1至128个有界孔')
    ids = []
    for item in holes:
        if (not isinstance(item, dict) or set(item) != {'id','kind','reason'}
                or type(item['id']) is not int or item['id'] not in allowed
                or item['kind'] not in ('arrow', 'gray_band_omission')):
            raise ValueError('仅接受当前可补孔和箭头/灰带漏选语义理由')
        route_review.text(item['reason']); ids.append(item['id'])
    if len(set(ids)) != len(ids):
        raise ValueError('孔编号重复')
    # 拒绝后更换理由或重新编号不能反复提出同一像素版本的同一集合。
    key = hashlib.sha256(json.dumps([action['hashes'], sorted(ids)], sort_keys=True).encode()).hexdigest()
    if key in state.get('hole_proposal_seen', []):
        raise ValueError('此像素版本的相同提议已处理，请具体追问，不能重复要求批准')
    if state.get('hole_proposal_count', 0) >= 6:
        raise ValueError('提议预算已用完')
    state['hole_proposal_count'] = state.get('hole_proposal_count', 0)+1
    state.setdefault('hole_proposal_seen', []).append(key)
    proposal = {k: action[k] for k in ('review_id','fingerprint','hashes','holes')}
    proposal.update(proposal_id=str(uuid.uuid4()), status='pending', key=key,
                    verified=False, original_gps=False)
    state['hole_proposal'] = proposal
    state.update(status='waiting_for_user', question='模型提出局部补孔；请逐项查看下方原图与精确新增像素后明确批准或拒绝。默认不修改；真实回转孔可能被模型误认。')
    return proposal


def decide(folder, data):
    from ride_agent import load, save
    from web_hole_review import apply
    state = load(folder)
    if set(data) != {'proposal_id','decision','confirm_source_reviewed','reason'}:
        raise ValueError('批准/拒绝只接受固定参数，不接受替换孔编号')
    proposal = state.get('hole_proposal', {})
    if (proposal.get('status') != 'pending' or proposal.get('proposal_id') != data['proposal_id']
            or proposal['hashes'] != binding(state, folder)
            or state.get('hole_review', {}).get('review_id') != proposal['review_id']):
        raise ValueError('提议已过期、跨任务或已消费')
    route_review.text(data['reason'])
    if data['decision'] not in ('approve','reject') or type(data['confirm_source_reviewed']) is not bool:
        raise ValueError('批准/拒绝不合法')
    if data['decision'] == 'approve':
        if data['confirm_source_reviewed'] is not True:
            raise ValueError('必须明确确认未叠加原图')
        apply(folder, {'review_id':proposal['review_id'], 'fingerprint':proposal['fingerprint'],
                       'fill_hole_ids':[h['id'] for h in proposal['holes']], 'confirm_source_reviewed':True,
                       'model_proposal':proposal, 'user_reason':data['reason']})
        state = load(folder)
        for key in ('route_review','review_region','review_presented_version','review_presented_regions'):
            state.pop(key, None)
        proposal['status'] = 'approved'
    else:
        proposal['status'] = 'rejected'
        state.update(status='waiting_for_user', question='补孔提议已拒绝，掩码未改变。继续API时须保留拒绝理由，另提具体问题。')
    proposal['user_reason'] = data['reason']
    state['hole_proposal'] = proposal
    state.setdefault('hole_proposal_audit', []).append(dict(proposal))
    state.update(verified=False, original_gps=False)
    state.setdefault('messages', []).append({'role':'user','content':'LOCAL_HOLE_USER_DECISION '+json.dumps(proposal, ensure_ascii=False)})
    save(state, folder)
