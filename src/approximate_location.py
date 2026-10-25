"""隔离的近似定位：锚点来自用户/公开证据，绝不视为严格配准或原始 GPS。"""
import json
import math
from pathlib import Path
import numpy as np
from register_pixels import read_json, fit_registration, transform


def fit_approximation(spec):
    if not isinstance(spec, dict) or spec.get('mode') != 'approximate':
        raise ValueError('Require mode=approximate; strict controls are a different input')
    anchors = spec.get('anchors')
    if not isinstance(anchors, list) or not 3 <= len(anchors) <= 1000:
        raise ValueError('Require 3..1000 noncollinear evidence anchors')
    note = spec.get('uncertainty_note')
    if not isinstance(note, str) or not note.strip() or len(note) > 4000:
        raise ValueError('Require uncertainty_note (not a statistical confidence guarantee)')
    for anchor in anchors:
        if not isinstance(anchor, dict):
            raise ValueError('Expected anchor object')
        source = anchor.get('source')
        error = anchor.get('uncertainty_m')
        if not isinstance(source, str) or not source.strip() or len(source) > 2000:
            raise ValueError('Each anchor requires source text or public evidence URL')
        if isinstance(error, bool) or not isinstance(error, (int, float)) or not math.isfinite(error) or not 0 < error <= 100000:
            raise ValueError('Each anchor requires positive finite uncertainty_m <= 100000')
    # 只复用原有有界仿射数学与几何退化校验；不更改35米严格门槛，且无独立检查通过资格。
    model = fit_registration({'coordinate_system': spec.get('coordinate_system'),
                              'pixel_frame': spec.get('pixel_frame'),
                              'control_points': anchors, 'check_points': []})
    model.update(approximate=True, verified=False, original_gps=False, navigable=False,
                 status='approximate_candidate', method='evidence_anchors_approximate_affine',
                 independent_error_gate_passed=False, strict_registration_eligible=False,
                 anchors=anchors, uncertainty_note=note,
                 synthetic=spec.get('synthetic') is True,
                 anchor_uncertainty_max_m=max(a['uncertainty_m'] for a in anchors),
                 uncertainty_interpretation='Declared anchor uncertainty and residuals are not a route error bound. Wrong associations, affine distortion and extrapolation can cause unbounded additional error.')
    model['warnings'].append('近似候选不可导航、不可道路匹配；来源未自动核实。误差声明不是轨迹精度保证。')
    return model


def approximate(graph_path, anchors_path, output_dir):
    graph, spec = read_json(graph_path), read_json(anchors_path)
    model = fit_approximation(spec)
    if graph.get('coordinate_system') != 'local_mask_pixels_xy_top_left':
        raise ValueError('Expected local mask pixel graph')
    segments = graph.get('segments_xy')
    if not isinstance(segments, list) or not 1 <= len(segments) <= 10000:
        raise ValueError('Expected bounded nonempty segments')
    if any(not isinstance(s, list) or len(s) < 2 for s in segments) or sum(map(len, segments)) > 30000:
        raise ValueError('Invalid/oversized segments')
    px = np.array([a['pixel_xy'] for a in spec['anchors']])
    lo, hi = px.min(axis=0), px.max(axis=0)
    features, outside = [], 0
    # 用凸包检测外推，而不将包围盒内误称内插。计算沿用拟合的归一化像素空间。
    ordered = sorted(set(map(tuple, px.tolist())))
    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
    def half(points):
        hull = []
        for p in points:
            while len(hull) >= 2 and cross(hull[-2], hull[-1], p) <= 0:
                hull.pop()
            hull.append(p)
        return hull
    hull = half(ordered)[:-1] + half(ordered[::-1])[:-1]
    hull_outside = 0
    for i, segment in enumerate(segments):
        coordinates = transform(segment, model)
        extrapolated = sum(any(cross(hull[j], hull[(j+1) % len(hull)], p) < -1e-8 for j in range(len(hull))) for p in segment)
        hull_outside += extrapolated
        outside += sum(bool(np.any(np.array(p) < lo) or np.any(np.array(p) > hi)) for p in segment)
        features.append({'type':'Feature', 'properties':{
            'segment_id':i, 'coordinate_system':'WGS84', 'approximate':True,
            'verified':False, 'original_gps':False, 'navigable':False,
            'traversal_order_known':False, 'vertices_outside_anchor_hull':extrapolated},
            'geometry':{'type':'LineString', 'coordinates':coordinates}})
    model.update(vertices_outside_control_bbox=outside, vertices_outside_anchor_hull=hull_outside,
                 extrapolation_present=bool(hull_outside), anchor_hull_pixel_xy=[list(p) for p in hull])
    out = Path(output_dir)
    names = ('approximation.json', 'approximate-segments.geojson')
    if any(Path(p).resolve() == (out/n).resolve() for p in (graph_path, anchors_path) for n in names):
        raise ValueError('Output must not overwrite input')
    out.mkdir(parents=True, exist_ok=True)
    collection = {'type':'FeatureCollection', 'approximate':True, 'verified':False,
                  'original_gps':False, 'navigable':False, 'metadata':model, 'features':features}
    for name, data in zip(names, (model, collection)):
        (out/name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return model
