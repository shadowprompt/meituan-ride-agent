"""本地几何分量选择：保守自动排除、歧义保留；不做语义或地理识别。"""
from collections import deque
import math
import numpy as np


def select_components(mask, component_ids=None):
    """编号按面积降序、坐标破同分；显式选择可保留任意多个断裂片段。"""
    h, w = mask.shape
    labels = np.zeros(mask.shape, dtype=np.int32)
    records = []
    for y, x in zip(*np.nonzero(mask)):
        if labels[y, x]:
            continue
        label = len(records) + 1
        labels[y, x] = label
        todo = deque([(int(y), int(x))])
        count = 0
        x0 = x1 = int(x)
        y0 = y1 = int(y)
        while todo:
            cy, cx = todo.popleft()
            count += 1
            x0, x1 = min(x0, cx), max(x1, cx)
            y0, y1 = min(y0, cy), max(y1, cy)
            for ny in range(max(0, cy-1), min(h, cy+2)):
                for nx in range(max(0, cx-1), min(w, cx+2)):
                    if mask[ny, nx] and not labels[ny, nx]:
                        labels[ny, nx] = label
                        todo.append((ny, nx))
        records.append({'label': label, 'pixels': count,
                        'bbox_crop_xyxy': [x0, y0, x1+1, y1+1]})
    records.sort(key=lambda c: (-c['pixels'], c['bbox_crop_xyxy']))
    for i, c in enumerate(records, 1):
        c['id'] = i
        x0, y0, x1, y1 = c['bbox_crop_xyxy']
        dx, dy = x1-x0, y1-y0
        c['span'] = max(dx, dy)
        c['aspect'] = max(dx, dy)/min(dx, dy)
        c['occupancy'] = c['pixels']/(dx*dy)
    all_ids = [c['id'] for c in records]
    selected = all_ids
    status = 'single_candidate' if len(records) == 1 else 'ambiguous'
    reason = '单一颜色分量仍需目视检查，不代表已识别路线。' if len(records) == 1 else '几何证据不足，保留全部候选，须明确选择一个或多个分量后再生成中心线。'
    if not records and component_ids is not None:
        raise ValueError('没有当前候选可选择')
    if not records:
        status, reason = 'empty', '没有候选分量。'
    elif component_ids is not None:
        if (not isinstance(component_ids, list) or not component_ids or len(component_ids) > len(records)
                or any(type(v) is not int or v not in all_ids for v in component_ids)
                or len(set(component_ids)) != len(component_ids)):
            raise ValueError('分量编号须为当前候选中不重复的正整数列表')
        selected = sorted(component_ids)
        status, reason = 'explicit', '用户显式选择；未选分量保留审计，不推断断裂连接或行进顺序。'
    elif len(records) > 1:
        anchor = records[0]
        box = anchor['bbox_crop_xyxy']
        width = anchor['pixels']/anchor['span']
        dominant = (anchor['aspect'] >= 5 and anchor['span'] >= max(h, w)*0.45
                    and anchor['pixels']/int(mask.sum()) >= 0.85)
        for c in records[1:]:
            b = c['bbox_crop_xyxy']
            gap = math.hypot(max(0, box[0]-b[2], b[0]-box[2]),
                             max(0, box[1]-b[3], b[1]-box[3]))
            c['anchor_bbox_gap'] = gap
            # 必须同时满足紧凑、小跨度、远离长带，且所有旁支均满足；不是最大分量兜底。
            c['isolated_compact'] = (c['aspect'] <= 2 and c['span'] <= anchor['span']*0.10
                                     and c['occupancy'] >= 0.15
                                     and gap >= max(3*width, anchor['span']*0.12))
        if dominant and all(c['isolated_compact'] for c in records[1:]):
            selected = [anchor['id']]
            status = 'auto_isolated_compact'
            reason = '长带跨度≥裁剪长边45%、长宽比≥5、面积占比≥85%；其余均紧凑(长宽比≤2、占框≥15%)、跨度≤长带10%，间距≥长带12%且≥估计带宽3倍。仅几何启发式，可用编号覆盖。'
    selected_mask = np.zeros(mask.shape, dtype=bool)
    for c in records:
        c['selected'] = c['id'] in selected
        c['decision'] = '保留候选/待确认' if status == 'ambiguous' else ('选中' if c['selected'] else '排除但保留审计')
        if c['selected']:
            selected_mask |= labels == c['label']
        del c['label']
    return selected_mask, {'policy': 'conservative_geometry_v1', 'status': status, 'reason': reason,
                           'requires_selection': status == 'ambiguous', 'selected_ids': selected,
                           'excluded_ids': [i for i in all_ids if i not in selected], 'candidates': records}
