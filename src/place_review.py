"""Local, ID-only POI proposals. No coordinates generated and no supplier upload."""
import hashlib
from pathlib import Path


def image_version(folder):
    return hashlib.sha256((Path(folder)/'input.png').read_bytes()).hexdigest()


def review_places(places, action, folder, image_size):
    if (set(action) != {'action','version','image_version','candidate_ids','pixel_bbox','evidence','ambiguous'}
            or action['version'] != places.get('version')
            or action['image_version'] != image_version(folder)
            or places.get('image_version') != action['image_version']):
        raise ValueError('Current candidates and screenshot version required; no free coordinates')
    ids, bbox = action['candidate_ids'], action['pixel_bbox']
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(not isinstance(i,str) for i in ids)
            or len(set(ids)) != len(ids) or type(action['ambiguous']) is not bool
            or not isinstance(action['evidence'], str) or not 1 <= len(action['evidence'].strip()) <= 1000):
        raise ValueError('Bounded evidence required')
    if (not isinstance(bbox,list) or len(bbox)!=4 or any(type(v) is not int for v in bbox)
            or not 0 <= bbox[0] < bbox[2] <= image_size[0] or not 0 <= bbox[1] < bbox[3] <= image_size[1]):
        raise ValueError('Original screenshot pixel bbox required')
    candidates = {p['id']:p for p in places['candidates']}
    if any(i not in candidates for i in ids):
        raise ValueError('Stale or unknown candidate ID')
    ambiguous = action['ambiguous'] or len(ids) != 1 or any(candidates[i]['region_anomaly'] for i in ids)
    return {'status':'ambiguous' if ambiguous else 'anchors_proposed', 'candidate_ids':ids,
            'version':action['version'],'image_version':action['image_version'],
            'pixel_bbox':bbox,'evidence':action['evidence'],'verified':False,
            'usable_for_registration':False, 'notice':'仅像素证据与候选ID关联提案；单候选不等于验证，不自动生成配准控制点或经纬度轨迹。'}
