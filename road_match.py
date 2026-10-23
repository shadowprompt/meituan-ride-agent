"""Conservative offline, independent-segment OSM shape candidates; NOT navigation.

Requires explicit WGS84 registration. Graph is undirected: direction, turn rules,
current access, bridges/layers and rideability are NOT verified. Straight OSM edges are densified into bounded virtual nodes; this does not
recover missing road geometry or connections.
No global traversal order is inferred; overlapping branches stay independent.
Limits intentionally reject large/nonlocal data rather than silently truncate it.
"""
import argparse
import heapq
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw

MAX_NODES = 100000
MAX_EDGES = 200000


def _number(x):
    return not isinstance(x, bool) and isinstance(x, (int, float)) and math.isfinite(x)


def _coord(p):
    if (not isinstance(p, (list, tuple)) or len(p) != 2
            or not all(_number(v) for v in p)
            or not -180 <= p[0] <= 180 or not -85 <= p[1] <= 85):
        raise ValueError('Expected finite [longitude, latitude] within supported WGS84 range')
    return tuple(p)


def _load_segments(path):
    path = Path(path)
    if path.stat().st_size > 5000000:
        raise ValueError('Segments exceed 5 MB')
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection' or data.get('crs') is not None:
        raise ValueError('Expected FeatureCollection without legacy CRS')
    features = data.get('features')
    if not isinstance(features, list) or not 1 <= len(features) <= 128:
        raise ValueError('Require 1..128 segments')
    result, count = [], 0
    for f in features:
        if not isinstance(f, dict) or f.get('type') != 'Feature' or f.get('crs') is not None:
            raise ValueError('Expected Feature')
        props, geom = f.get('properties'), f.get('geometry')
        if not isinstance(props, dict) or props.get('coordinate_system') != 'WGS84':
            raise ValueError('Each segment requires explicit properties.coordinate_system=WGS84')
        if not isinstance(geom, dict) or geom.get('type') != 'LineString':
            raise ValueError('Each segment must be LineString')
        coords = geom.get('coordinates')
        if not isinstance(coords, list) or len(coords) < 2:
            raise ValueError('Require at least two coordinates')
        count += len(coords)
        if count > 20000:
            raise ValueError('Exceeds 20000 total vertices')
        points = []
        for p in coords:
            q = _coord(p)
            if not points or points[-1] != q:
                points.append(q)
        if len(points) < 2:
            raise ValueError('Zero-length segment')
        result.append(points)
    return result


def _load_osm(path):
    path = Path(path)
    if path.stat().st_size > 30000000:
        raise ValueError('OSM exceeds 30 MB')
    raw = path.read_bytes()
    if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('OSM DTD/entities unsupported')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('Invalid OSM XML') from exc
    if root.tag != 'osm':
        raise ValueError('Expected OSM XML root')
    nodes = {}
    for el in root.findall('node'):
        if len(nodes) >= MAX_NODES:
            raise ValueError('OSM node limit exceeded')
        try:
            key = int(el.attrib['id'])
            p = _coord([float(el.attrib['lon']), float(el.attrib['lat'])])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError('Invalid OSM node') from exc
        if key in nodes:
            raise ValueError('Duplicate OSM node')
        nodes[key] = p
    edges = set()
    references = 0
    for way in root.findall('way'):
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        if (not tags.get('highway') or tags['highway'] in ('motorway', 'motorway_link')
                or tags.get('bicycle') == 'no' or tags.get('access') in ('no', 'private')):
            continue
        try:
            ids = [int(n.attrib['ref']) for n in way.findall('nd')]
        except (KeyError, ValueError) as exc:
            raise ValueError('Invalid OSM way reference') from exc
        references += len(ids)
        if references > 400000:
            raise ValueError('OSM way reference limit exceeded')
        for a, b in zip(ids, ids[1:]):
            # Missing nodes break the way; never bridge across missing references.
            if a in nodes and b in nodes and a != b:
                edges.add(tuple(sorted((a, b))))
                if len(edges) > MAX_EDGES:
                    raise ValueError('OSM edge limit exceeded')
    return nodes, sorted(edges)


def _project(points, origin):
    return (np.asarray(points, dtype=float) - origin) * [111195 * math.cos(math.radians(origin[1])), 111195]


def _simplify(points, epsilon):
    """Bounded iterative RDP; distance is to finite segments, not infinite lines.

    Finite-segment distance preserves reversal extrema, including closed/retraced
    lines. This is geometric approximation only, not branch merging or pruning.
    """
    keep = {0, len(points)-1}
    stack = [(0, len(points)-1)]
    budget = 2000000
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        interior = points[start+1:end]
        budget -= len(interior)
        if budget < 0:
            raise ValueError('Reference simplification budget exceeded')
        distances = _distance(interior, points[[start, end]])
        offset = int(np.argmax(distances))
        if distances[offset] > epsilon:
            pivot = start + 1 + offset
            keep.add(pivot)
            stack.extend(((start, pivot), (pivot, end)))
    return points[sorted(keep)]


def _sample(points, step, maximum=512):
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    cum = np.r_[0., np.cumsum(lengths)]
    if cum[-1] <= 0 or cum[-1] > 20000:
        raise ValueError('Segment length must be positive and <=20 km')
    n = max(2, int(math.ceil(cum[-1] / step)) + 1)
    if n > maximum:
        raise ValueError('Sampling budget exceeded; use smaller local segments')
    # Include original vertices: do not smooth corners or erase out-and-back turns.
    positions = np.unique(np.r_[cum, np.linspace(0, cum[-1], n)])
    if len(positions) > maximum:
        raise ValueError('Sampling vertex budget exceeded')
    return np.column_stack([np.interp(positions, cum, points[:, k]) for k in (0, 1)])


def _distance(points, line):
    best = np.full(len(points), np.inf)
    for a, b in zip(line, line[1:]):
        v = b - a
        denom = float(v @ v)
        if denom == 0:
            d = np.linalg.norm(points - a, axis=1)
        else:
            t = np.clip((points - a) @ v / denom, 0, 1)
            d = np.linalg.norm(points - (a + t[:, None] * v), axis=1)
        best = np.minimum(best, d)
    return best


def _frechet(a, b):
    previous = np.full(len(b), np.inf)
    for i, p in enumerate(a):
        row = np.full(len(b), np.inf)
        for j, distance in enumerate(np.linalg.norm(b - p, axis=1)):
            before = 0 if i == j == 0 else min(previous[j], row[j-1] if j else np.inf,
                                                             previous[j-1] if j else np.inf)
            row[j] = max(distance, before)
        previous = row
    return float(previous[-1])


def _candidate(reference, nodes, edges, tolerance):
    origin = np.asarray(reference[0])
    ref = _project(reference, origin)
    if np.max(np.abs(ref)) > 25000:
        raise ValueError('Nonlocal segment extent')
    # Dense registered pixel chains can have thousands of redundant vertices.
    # Reserve simplification error in the final shape threshold; retain the raw
    # reference for extent, length and corridor bounds. Never raise tolerance.
    simplification_error = min(1., tolerance / 10)
    simplified = _simplify(ref, simplification_error)
    anchors = _sample(simplified, tolerance / 2, 256)
    # Densify eligible straight OSM edges into bounded virtual graph nodes.
    # These preserve graph connectivity, unlike independently snapping anchors
    # to arbitrary nearby edges (which could jump between disconnected roads).
    local_nodes, local_edges = {}, []
    lower, upper = ref.min(axis=0) - tolerance, ref.max(axis=0) + tolerance
    virtual_id = min([0] + list(nodes)) - 1
    for a, b in edges:
        pair = _project([nodes[a], nodes[b]], origin)
        if not (np.all(pair.max(axis=0) >= lower) and np.all(pair.min(axis=0) <= upper)):
            continue
        length = float(np.linalg.norm(pair[1] - pair[0]))
        steps = max(1, int(math.ceil(length / (tolerance / 2))))
        if steps > 5000 or len(local_nodes) + steps + 1 > 5000:
            raise ValueError('Local densified graph exceeds 5000 nodes')
        local_nodes[a], local_nodes[b] = nodes[a], nodes[b]
        previous = a
        for k in range(1, steps):
            t = k / steps
            local_nodes[virtual_id] = tuple((1-t)*np.asarray(nodes[a]) + t*np.asarray(nodes[b]))
            local_edges.append((previous, virtual_id))
            previous = virtual_id
            virtual_id -= 1
        local_edges.append((previous, b))
    nodes, edges = local_nodes, local_edges
    ids = list(nodes)
    if not ids:
        return None
    xy = _project([nodes[i] for i in ids], origin)
    lo, hi = ref.min(axis=0) - tolerance, ref.max(axis=0) + tolerance
    keep = np.flatnonzero(np.all((xy >= lo) & (xy <= hi), axis=1))
    if len(keep) > 5000:
        raise ValueError('Local graph exceeds 5000 nodes')
    if not len(keep):
        return None
    local_ids = [ids[i] for i in keep]
    xy = xy[keep]
    index = {key: i for i, key in enumerate(local_ids)}
    choices = []
    for p in anchors:
        dist = np.linalg.norm(xy - p, axis=1)
        nearby = np.flatnonzero(dist <= tolerance)
        choices.append(sorted(nearby, key=lambda i: (dist[i], local_ids[i]))[:4])
    if any(not c for c in choices):
        return None
    graph = [[] for _ in local_ids]
    for a, b in edges:
        if a in index and b in index:
            i, j = index[a], index[b]
            length = float(np.linalg.norm(xy[i] - xy[j]))
            if length > 0:
                graph[i].append((j, length))
                graph[j].append((i, length))
    states = {i: (float(np.linalg.norm(xy[i] - anchors[0])), [i]) for i in choices[0]}
    budget = 200000
    for k in range(1, len(anchors)):
        line = anchors[k-1:k+1]
        allowed = _distance(xy, line) <= tolerance
        next_states = {}
        edge_cache = {}
        max_length = float(np.linalg.norm(line[1] - line[0])) * 3 + 2 * tolerance
        for start, (base, prefix) in states.items():
            if not allowed[start]:
                continue
            queue, costs, parents, lengths = [(0., start)], {start: 0.}, {}, {start: 0.}
            while queue:
                cost, u = heapq.heappop(queue)
                budget -= 1
                if budget < 0:
                    raise ValueError('Graph search budget exceeded')
                if cost != costs[u]:
                    continue
                for v, length in graph[u]:
                    if not allowed[v] or lengths[u] + length > max_length:
                        continue
                    key = tuple(sorted((u, v)))
                    if key not in edge_cache:
                        # A straight edge between two corridor points stays within
                        # this convex capsule; midpoint penalty favors alignment.
                        penalty = float(_distance(np.asarray([(xy[u]+xy[v])/2]), line)[0])
                        edge_cache[key] = length * (1 + 4 * (penalty / tolerance)**2)
                    new = cost + edge_cache[key]
                    if new < costs.get(v, math.inf):
                        costs[v], parents[v], lengths[v] = new, u, lengths[u] + length
                        heapq.heappush(queue, (new, v))
            for end in choices[k]:
                if end not in costs:
                    continue
                score = base + costs[end] + float(np.linalg.norm(xy[end] - anchors[k]))
                if end not in next_states or score < next_states[end][0]:
                    suffix, u = [], end
                    while u != start:
                        suffix.append(u)
                        u = parents[u]
                    next_states[end] = (score, prefix + suffix[::-1])
        states = next_states
        if not states:
            return None
    # Validate full ordered shape: proximity alone must never count as a match.
    for _, path in sorted(states.values(), key=lambda item: item[0]):
        if len(path) < 2:
            continue
        route = xy[path]
        length = float(np.linalg.norm(np.diff(route, axis=0), axis=1).sum())
        ref_length = float(np.linalg.norm(np.diff(ref, axis=0), axis=1).sum())
        if length < ref_length * .5 or length > ref_length * 2 + 2 * tolerance:
            continue
        try:
            sampled = _sample(route, tolerance / 2)
        except ValueError:
            continue
        distance = _frechet(anchors, sampled)
        if distance <= tolerance - simplification_error:
            return [nodes[local_ids[i]] for i in path], distance + simplification_error
    return None


def _overlay(path, references, candidates, nodes, edges):
    points = np.asarray([p for line in references + candidates for p in line], dtype=float)
    origin = points[0]
    ref_xy = _project(points, origin)
    low, high = ref_xy.min(axis=0), ref_xy.max(axis=0)
    margin = max(100., float(np.max(high-low)) * .08)
    low, high = low-margin, high+margin
    scale = min(940 / (high[0]-low[0]), 700 / (high[1]-low[1]))
    image = Image.new('RGB', (1000, 780), 'white')
    draw = ImageDraw.Draw(image)

    def pixels(line):
        q = _project(line, origin)
        q = np.clip(q, low-1e5, high+1e5)  # bounded PIL integer conversion
        return [(30 + float((p[0]-low[0])*scale), 740-float((p[1]-low[1])*scale)) for p in q]

    for a, b in edges:
        q = _project([nodes[a], nodes[b]], origin)
        if np.all(q.max(axis=0) >= low) and np.all(q.min(axis=0) <= high):
            draw.line(pixels([nodes[a], nodes[b]]), fill='#cccccc', width=1)
    for line in references:
        draw.line(pixels(line), fill='blue', width=5)
    for line in candidates:
        draw.line(pixels(line), fill='red', width=2)
    draw.text((20, 755), 'Gray: OSM | Blue: registered reference | Red: unverified candidates (not navigation)', fill='black')
    image.save(path)


def match(segments_path, osm_path, output_dir, tolerance_m=50):
    """Write unverified candidates/report/overlay; invalid input raises ValueError.

    A partial result is needs_confirmation if ANY segment matched. Always inspect
    matched_segment_count; missing segments are never joined or fabricated.
    """
    if not _number(tolerance_m) or not 1 <= tolerance_m <= 500:
        raise ValueError('tolerance_m must be finite within 1..500')
    references = _load_segments(segments_path)
    nodes, edges = _load_osm(osm_path)
    warnings = ['Heuristic offline candidates only; not navigation or original GPS.',
                'Undirected graph ignores one-way/turn restrictions and current rideability.',
                'Segments/branches are independent; no traversal order across segments inferred.',
                'Bounded virtual-node snapping/search may reject valid sparse/complex roads.']
    features, candidates = [], []
    for i, reference in enumerate(references):
        try:
            result = _candidate(reference, nodes, edges, tolerance_m)
        except ValueError as exc:
            warnings.append(f'Segment {i}: {exc}; no candidate emitted')
            result = None
        if result is None:
            warnings.append(f'Segment {i}: no connected shape-aligned candidate')
            continue
        coords, distance = result
        candidates.append(coords)
        features.append({'type': 'Feature', 'properties': {
            'input_segment_index': i, 'coordinate_system': 'WGS84', 'verified': False,
            'original_gps': False, 'status': 'needs_confirmation',
            'ordered_shape_distance_m': round(distance, 3)},
            'geometry': {'type': 'LineString', 'coordinates': coords}})
    report = {'verified': False, 'original_gps': False,
              'status': 'needs_confirmation' if features else 'no_match',
              'matched_segment_count': len(features), 'input_segment_count': len(references),
              'tolerance_m': tolerance_m, 'attribution': 'Map input supplied by user; retain source license. OSM data: © OpenStreetMap contributors, https://www.openstreetmap.org/copyright', 'warnings': warnings}
    out = Path(output_dir)
    for source in (segments_path, osm_path):
        if Path(source).resolve() in [(out/name).resolve() for name in ('road-match.json','road-candidates.geojson','road-overlay.png')]:
            raise ValueError('Output must not overwrite source')
    out.mkdir(parents=True, exist_ok=True)
    (out / 'road-candidates.geojson').write_text(json.dumps(
        {'type': 'FeatureCollection', 'features': features}, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    _overlay(out / 'road-overlay.png', references, candidates, nodes, edges)
    (out / 'road-match.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--segments', required=True)
    parser.add_argument('--osm', required=True)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--tolerance-m', type=float, default=50)
    args = parser.parse_args()
    try:
        report = match(args.segments, args.osm, args.output_dir, args.tolerance_m)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
