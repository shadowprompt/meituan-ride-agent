"""Offline geometry candidate evaluator, not screenshot recognition or navigation.
Reference must be an ordered WGS84 LineString, independently registered/labelled.
Only stdlib is required. Scores are heuristics, NOT calibrated probabilities.
"""
import argparse
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET


def load_route(path):
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError('Route JSON exceeds 5 MB limit')
    obj = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(obj, dict):
        raise ValueError('Expected GeoJSON object')
    if not isinstance(obj.get('geometry'), dict):
        raise ValueError('Expected geometry object')
    if obj.get('type') != 'Feature' or obj.get('geometry', {}).get('type') != 'LineString':
        raise ValueError('Expected GeoJSON Feature with LineString geometry')
    props = obj.get('properties') or {}
    if not isinstance(props, dict):
        raise ValueError('Expected properties object')
    if obj.get('crs') is not None:
        raise ValueError('Legacy CRS declarations unsupported; use explicit WGS84 properties')
    if props.get('coordinate_system') != 'WGS84':
        raise ValueError('Explicit properties.coordinate_system=WGS84 required')
    points = obj['geometry']['coordinates']
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError('At least two points required')
    if len(points) > 20000:
        raise ValueError('Route exceeds 20000 vertex limit')
    clean = []
    for p in points:
        if not isinstance(p, list) or len(p) != 2:
            raise ValueError('Expected [longitude, latitude] pairs')
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p):
            raise ValueError('Coordinates must be finite numbers')
        if not (-180 <= p[0] <= 180 and -85 <= p[1] <= 85):
            raise ValueError('Coordinate outside supported range')
        if not clean or p != clean[-1]:
            clean.append(p)
    if len(clean) < 2:
        raise ValueError('Zero-length route')
    return clean


def project(points, origin):
    lon, lat = origin
    return [((x-lon)*111195*math.cos(math.radians(lat)), (y-lat)*111195) for x, y in points]


def length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:]))


def resample(points, count=96):
    total = length(points)
    if total <= 0:
        raise ValueError('Zero-length route')
    cumulative = [0.0]
    for a, b in zip(points, points[1:]):
        cumulative.append(cumulative[-1] + math.dist(a, b))
    result, j = [], 1
    for i in range(count):
        target = total*i/(count-1)
        while j < len(points)-1 and cumulative[j] < target:
            j += 1
        t = (target-cumulative[j-1])/(cumulative[j]-cumulative[j-1])
        a, b = points[j-1], points[j]
        result.append((a[0]+t*(b[0]-a[0]), a[1]+t*(b[1]-a[1])))
    return result


def frechet(a, b):
    """Discrete Frechet distance: respects order and repeated traversals."""
    previous = [math.inf]*len(b)
    for i, p in enumerate(a):
        row = []
        for j, q in enumerate(b):
            predecessor = 0 if i == j == 0 else min(
                previous[j], row[j-1] if j else math.inf,
                previous[j-1] if j else math.inf)
            row.append(max(math.dist(p, q), predecessor))
        previous = row
    return previous[-1]


def score_route(reference, candidate, tolerance_m=35):
    if not math.isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError('tolerance_m must be positive and finite')
    origin = reference[0]
    r, c = project(reference, origin), project(candidate, origin)
    # This projection is intended for local rides, not cross-country/geodesic use.
    if any(math.hypot(*p) > 100000 for p in r+c):
        raise ValueError('Routes must stay within 100 km of reference origin')
    rd, cd = resample(r), resample(c)
    distance = frechet(rd, cd)
    endpoint = (math.dist(r[0], c[0])+math.dist(r[-1], c[-1]))/2
    ratio = length(c)/length(r)
    length_score = math.exp(-abs(math.log(ratio)))
    # Direction agreement at equal normalized arc length penalizes lost/reordered retraces.
    direction = []
    for a, b, x, y in zip(rd, rd[1:], cd, cd[1:]):
        u, v = (b[0]-a[0], b[1]-a[1]), (y[0]-x[0], y[1]-x[1])
        denom = math.hypot(*u)*math.hypot(*v)
        if denom:
            direction.append((1+max(-1, min(1, (u[0]*v[0]+u[1]*v[1])/denom)))/2)
    heading = sum(direction)/len(direction) if direction else 0
    shape = math.exp(-distance/tolerance_m)
    alignment = math.exp(-endpoint/tolerance_m)
    score = 0.55*shape+0.15*alignment+0.15*length_score+0.15*heading
    return {'score': score, 'frechet_m': distance, 'endpoint_error_m': endpoint,
            'length_ratio': ratio, 'reference_length_m': length(r),
            'candidate_length_m': length(c), 'direction_agreement': heading,
            'geometry_gate_passed': distance <= tolerance_m and endpoint <= tolerance_m and 0.8 <= ratio <= 1.25}


class RoadIndex:
    """Checks existing OSM highway edges only; does NOT establish cycling legality."""
    def __init__(self, path):
        root = ET.parse(path).getroot()
        nodes = {n.get('id'): (float(n.get('lon')), float(n.get('lat'))) for n in root.findall('node')}
        self.edges = set()
        for way in root.findall('way'):
            tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
            if not tags.get('highway'):
                continue
            refs = [n.get('ref') for n in way.findall('nd')]
            for a, b in zip(refs, refs[1:]):
                if a in nodes and b in nodes:
                    self.edges.add(frozenset((nodes[a], nodes[b])))

    def check(self, points):
        missing = [i for i, (a, b) in enumerate(zip(points, points[1:]))
                   if frozenset((tuple(a), tuple(b))) not in self.edges]
        return {'all_segments_are_osm_edges': not missing, 'unmatched_segment_indices': missing,
                'cycling_legality_verified': False,
                'method': 'Exact unsimplified OSM node-coordinate edge check; undirected; no snapping'}


def evaluate(reference_path, candidates, osm_path=None, tolerance_m=35):
    if not math.isfinite(tolerance_m) or tolerance_m <= 0:
        raise ValueError('tolerance_m must be positive and finite')
    if not 1 <= len(candidates) <= 100:
        raise ValueError('Provide between 1 and 100 candidates')
    reference = load_route(reference_path)
    roads = RoadIndex(osm_path) if osm_path else None
    results = []
    for path in candidates:
        try:
            candidate = load_route(path)
            result = {'candidate': str(path), **score_route(reference, candidate, tolerance_m)}
            result['road_check'] = roads.check(candidate) if roads else None
            if roads and not result['road_check']['all_segments_are_osm_edges']:
                result['geometry_gate_passed'] = False
            results.append(result)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            results.append({'candidate': str(path), 'error': str(exc), 'geometry_gate_passed': False})
    results.sort(key=lambda r: r.get('score', -1), reverse=True)
    valid = [r for r in results if 'score' in r]
    margin = valid[0]['score']-valid[1]['score'] if len(valid) > 1 else None
    return {'verified': False, 'original_gps': False, 'status': 'needs_confirmation',
            'reference': str(reference_path), 'tolerance_m': tolerance_m,
            'score_type': 'heuristic_not_probability', 'top_score_margin': margin,
            'ambiguous': margin is None or margin < 0.08,
            'warnings': ['Reference registration/labels are not independently verified by this tool.',
                         'No OCR, automatic screenshot geolocation, candidate generation, or cycling legality check.',
                         'Static screenshots cannot uniquely determine traversal counts or order at overlaps.'],
            'candidates': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference', required=True)
    parser.add_argument('--candidate', action='append', required=True)
    parser.add_argument('--osm')
    parser.add_argument('--tolerance-m', type=float, default=35)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        report = evaluate(args.reference, args.candidate, args.osm, args.tolerance_m)
        if not math.isfinite(args.tolerance_m) or args.tolerance_m <= 0:
            raise ValueError('tolerance_m must be positive and finite')
    except (ValueError, OSError, ET.ParseError) as exc:
        parser.error(str(exc))
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    print(json.dumps({'output': str(out), 'candidates': len(report['candidates']),
                      'status': report['status'], 'ambiguous': report['ambiguous']}))


if __name__ == '__main__':
    main()
