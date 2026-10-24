"""Manual WGS84 control-point affine registration. No automatic map matching."""
import argparse
import json
import math
from pathlib import Path
import numpy as np

SCALE = 111195.0


def read_json(path):
    path = Path(path)
    if path.stat().st_size > 10_000_000:
        raise ValueError('JSON exceeds 10 MB')
    obj = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(obj, dict):
        raise ValueError('Expected JSON object')
    return obj


def pair(value, geographic=False):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError('Expected coordinate pair')
    if any(isinstance(v, bool) or not isinstance(v, (int,float)) or not math.isfinite(v) for v in value):
        raise ValueError('Coordinates must be finite numbers')
    if geographic and not (-180 <= value[0] <= 180 and -85 <= value[1] <= 85):
        raise ValueError('Unsupported WGS84 range')
    return list(value)


def control_arrays(points):
    if not isinstance(points, list) or len(points) > 1000:
        raise ValueError('Expected at most 1000 control/check points')
    pixels, geo = [], []
    for point in points:
        if not isinstance(point, dict):
            raise ValueError('Expected control point object')
        pixels.append(pair(point.get('pixel_xy')))
        geo.append(pair(point.get('wgs84'), True))
    if len({tuple(p) for p in pixels}) != len(pixels):
        raise ValueError('Duplicate control/check pixel positions')
    return np.array(pixels, dtype=float).reshape(-1,2), np.array(geo, dtype=float).reshape(-1,2)


def project(geo, origin):
    return (geo-origin)*np.array([SCALE*math.cos(math.radians(origin[1])), SCALE])


def fit_registration(spec, max_error_m=35):
    if not math.isfinite(max_error_m) or max_error_m <= 0:
        raise ValueError('max_error_m must be finite and positive')
    if spec.get('coordinate_system') != 'WGS84' or spec.get('pixel_frame') != 'local_mask':
        raise ValueError('Require coordinate_system=WGS84 and pixel_frame=local_mask')
    px, geo = control_arrays(spec.get('control_points'))
    if len(px) < 3:
        raise ValueError('At least three noncollinear control points required')
    check_px, check_geo = control_arrays(spec.get('check_points', []))
    if set(map(tuple,px)) & set(map(tuple,check_px)):
        raise ValueError('Check points must not reuse fitting pixel positions')
    origin = geo.mean(axis=0)
    target = project(geo, origin)
    if np.any(np.linalg.norm(target, axis=1) > 100000):
        raise ValueError('Controls must describe a local region within 100 km')
    center = px.mean(axis=0)
    spread = np.sqrt(np.mean((px-center)**2, axis=0))
    if np.any(spread < 1e-8):
        raise ValueError('Degenerate control layout')
    design = np.column_stack(((px-center)/spread, np.ones(len(px))))
    if np.linalg.matrix_rank(design) < 3 or np.linalg.cond(design) > 10000:
        raise ValueError('Collinear or ill-conditioned controls')
    coefficients = np.linalg.lstsq(design, target, rcond=None)[0]
    linear = coefficients[:2]/spread[:,None]
    if np.linalg.matrix_rank(linear) < 2 or np.linalg.cond(linear) > 10000:
        raise ValueError('Degenerate geographic mapping')
    training_errors = np.linalg.norm(design@coefficients-target, axis=1)
    if len(check_px):
        check_target = project(check_geo, origin)
        if np.any(np.linalg.norm(check_target, axis=1) > 100000):
            raise ValueError('Check points outside local region')
        check_design = np.column_stack(((check_px-center)/spread, np.ones(len(check_px))))
        check_errors = np.linalg.norm(check_design@coefficients-check_target, axis=1)
    else:
        check_errors = np.array([])
    passed = bool(len(check_errors) >= 2 and max(training_errors) <= max_error_m and max(check_errors) <= max_error_m)
    report = {'verified': False, 'original_gps': False, 'status': 'needs_confirmation',
              'method': 'manual_control_points_local_metric_affine', 'coordinate_system': 'WGS84',
              'pixel_frame': 'local_mask', 'control_count': len(px), 'check_count': len(check_px),
              'fit_errors_m': training_errors.tolist(), 'check_errors_m': check_errors.tolist(),
              'fit_rmse_m': float(np.sqrt(np.mean(training_errors**2))),
              'check_rmse_m': float(np.sqrt(np.mean(check_errors**2))) if len(check_errors) else None,
              'max_error_m': max_error_m, 'independent_error_gate_passed': passed,
              'origin_wgs84': origin.tolist(), 'pixel_center': center.tolist(),
              'pixel_scale': spread.tolist(), 'affine_coefficients': coefficients.tolist(),
              'warnings': ['Controls and check points are supplied manually; their geographic truth is not verified.',
                           'A low fitting error is not independent validation; three controls fit exactly.',
                           'No road snapping, cycling legality check, or screenshot coordinate-system detection.',
                           'An affine fit cannot correct arbitrary map distortion or wrong landmark associations.']}
    if len(check_errors) < 2:
        report['warnings'].append('At least two independent check points required to pass error gate.')
    return report


def transform(points, model):
    pixels = np.array([pair(p) for p in points], dtype=float).reshape(-1,2)
    design = np.column_stack(((pixels-np.array(model['pixel_center']))/np.array(model['pixel_scale']), np.ones(len(pixels))))
    metric = design@np.array(model['affine_coefficients'])
    if np.any(np.linalg.norm(metric, axis=1) > 100000):
        raise ValueError('Transformed geometry outside 100 km local projection')
    origin = np.array(model['origin_wgs84'])
    geo = metric/np.array([SCALE*math.cos(math.radians(origin[1])), SCALE])+origin
    return [pair(p, True) for p in geo.tolist()]


def register(graph_path, control_path, output_dir, max_error_m=35):
    graph, spec = read_json(graph_path), read_json(control_path)
    if graph.get('coordinate_system') != 'local_mask_pixels_xy_top_left':
        raise ValueError('Expected local mask pixel graph from centerline.py')
    segments = graph.get('segments_xy')
    if not isinstance(segments, list) or not segments or len(segments) > 10000:
        raise ValueError('Expected nonempty bounded segments list')
    if any(not isinstance(s,list) or len(s)<2 for s in segments) or sum(map(len,segments)) > 30000:
        raise ValueError('Invalid/oversized pixel segments')
    model = fit_registration(spec, max_error_m)
    features = []
    lo = np.min([p['pixel_xy'] for p in spec['control_points']], axis=0)
    hi = np.max([p['pixel_xy'] for p in spec['control_points']], axis=0)
    outside = 0
    for i, segment in enumerate(segments):
        coordinates = transform(segment, model)
        outside += sum(bool(np.any(np.array(p)<lo) or np.any(np.array(p)>hi)) for p in segment)
        features.append({'type': 'Feature', 'properties': {'segment_id': i, 'verified': False,
                         'original_gps': False, 'coordinate_system': 'WGS84',
                         'traversal_order_known': False},
                         'geometry': {'type': 'LineString', 'coordinates': coordinates}})
    model['vertices_outside_control_bbox'] = outside
    if outside:
        model['warnings'].append('Geometry extends outside control bounding box: extrapolation risk; bbox check is not convex-hull validation.')
    out = Path(output_dir)
    if any(Path(p).resolve() == (out/n).resolve() for p in (graph_path,control_path)
           for n in ('registration.json','registered-segments.geojson')):
        raise ValueError('Output must not overwrite input')
    out.mkdir(parents=True, exist_ok=True)
    (out/'registration.json').write_text(json.dumps(model, indent=2, allow_nan=False)+'\n')
    (out/'registered-segments.geojson').write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, indent=2, allow_nan=False)+'\n')
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', required=True)
    parser.add_argument('--controls', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--max-error-m', type=float, default=35)
    args = parser.parse_args()
    try:
        result = register(args.graph, args.controls, args.output_dir, args.max_error_m)
    except (ValueError, TypeError, KeyError, OSError, np.linalg.LinAlgError) as exc:
        parser.error(str(exc))
    print(json.dumps({k: result[k] for k in ('status','fit_rmse_m','check_rmse_m','independent_error_gate_passed')}))


if __name__ == '__main__':
    main()
