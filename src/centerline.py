"""Binary mask -> skeleton and local pixel graph. No GPS or ride-order inference."""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def thin(mask, max_iterations=2048):
    """Zhang-Suen 细化：补零边界，每个子步骤同时删点，避免遍历顺序影响结果。

    输入必须是线路区域而非两侧轮廓；细化本身不会理解或合并平行边缘。
    孔洞由上游有原图证据的掩码清理处理，这里保留剩余拓扑，不强删真实环路。
    """
    a = np.pad(np.asarray(mask, dtype=bool), 1)
    for iteration in range(max_iterations):
        changed = False
        for step in (0, 1):
            p = [a[:-2, 1:-1], a[:-2, 2:], a[1:-1, 2:], a[2:, 2:],
                 a[2:, 1:-1], a[2:, :-2], a[1:-1, :-2], a[:-2, :-2]]
            count = sum(v.astype(np.uint8) for v in p)
            transitions = sum((~p[i] & p[(i+1) % 8]).astype(np.uint8) for i in range(8))
            remove = a[1:-1, 1:-1] & (count >= 2) & (count <= 6) & (transitions == 1)
            if step == 0:
                remove &= ~(p[0] & p[2] & p[4]) & ~(p[2] & p[4] & p[6])
            else:
                remove &= ~(p[0] & p[2] & p[6]) & ~(p[0] & p[4] & p[6])
            if remove.any():
                a[1:-1, 1:-1][remove] = False
                changed = True
        if not changed:
            return a[1:-1, 1:-1], iteration+1
    raise ValueError('Thinning iteration limit reached; no output produced')


def pixel_graph(skeleton):
    pixels = {(int(x), int(y)) for y, x in zip(*np.nonzero(skeleton))}
    if len(pixels) > 10000:
        raise ValueError('Skeleton exceeds 10000 pixels; crop or resize input')
    graph = {}
    for x, y in sorted(pixels, key=lambda p: (p[1], p[0])):
        neighbors = []
        for dx, dy in ((-1,-1), (0,-1), (1,-1), (-1,0), (1,0), (-1,1), (0,1), (1,1)):
            q = (x+dx, y+dy)
            if q not in pixels:
                continue
            # Suppress redundant diagonal shortcut when an orthogonal connection exists.
            if dx and dy and ((x+dx,y) in pixels or (x,y+dy) in pixels):
                continue
            neighbors.append(q)
        graph[(x,y)] = sorted(neighbors, key=lambda p: (p[1],p[0]))
    return graph


def analyze(skeleton):
    graph = pixel_graph(skeleton)
    remaining = set(graph)
    components = []
    while remaining:
        seed = min(remaining, key=lambda p: (p[1],p[0]))
        remaining.remove(seed)
        stack, component = [seed], []
        while stack:
            p = stack.pop()
            component.append(p)
            for q in graph[p]:
                if q in remaining:
                    remaining.remove(q)
                    stack.append(q)
        components.append(component)
    endpoints = [p for p in graph if len(graph[p]) == 1]
    junctions = [p for p in graph if len(graph[p]) > 2]
    isolated = [p for p in graph if not graph[p]]
    visited, segments = set(), []

    def edge(a, b):
        return frozenset((a,b))

    def walk(start, second):
        path = [start, second]
        visited.add(edge(start, second))
        prev, current = start, second
        while len(graph[current]) == 2 and current != start:
            nxt = next(p for p in graph[current] if p != prev)
            if edge(current, nxt) in visited:
                break
            visited.add(edge(current, nxt))
            path.append(nxt)
            prev, current = current, nxt
        return path

    for start in graph:
        if len(graph[start]) != 2:
            for second in graph[start]:
                if edge(start,second) not in visited:
                    segments.append(walk(start,second))
    # Remaining edges are closed cycles without endpoints.
    for start in graph:
        for second in graph[start]:
            if edge(start,second) not in visited:
                segments.append(walk(start,second))
    edge_count = sum(map(len, graph.values()))//2
    cycles = edge_count-len(graph)+len(components)
    simple = len(components) == 1 and len(endpoints) == 2 and not junctions and not isolated
    path = segments[0] if simple and len(segments) == 1 else None
    warnings = ['Pixel geometry only; no geographic coordinates or original GPS.',
                'No gap filling or branch pruning; same-colored symbols may create branches.',
                'Repeated traversal and actual ride direction cannot be recovered from a static mask.']
    if len(components) > 1:
        warnings.append('Disconnected skeleton: segments are not joined automatically.')
    if junctions:
        warnings.append('Branching skeleton: traversal order is ambiguous.')
    if cycles:
        warnings.append('Cycle detected: start point and traversal order are unknown.')
    if not graph:
        warnings.append('Empty skeleton.')
    return {'status': 'needs_confirmation' if graph else 'no_detection',
            'verified': False, 'original_gps': False,
            'coordinate_system': 'local_mask_pixels_xy_top_left',
            'skeleton_pixels': len(graph), 'edge_count': edge_count,
            'component_count': len(components), 'cycle_rank': cycles,
            'endpoints_xy': endpoints, 'junction_pixels_xy': junctions,
            'isolated_pixels_xy': isolated, 'segments_xy': segments,
            'ordered_path_xy': path,
            'path_direction': 'arbitrary_not_ride_direction' if path else None,
            'warnings': warnings}


def process(mask_path, output_dir):
    source, out = Path(mask_path), Path(output_dir)
    if source.stat().st_size > 20_000_000:
        raise ValueError('Mask exceeds 20 MB limit')
    if source.resolve() in [(out/name).resolve() for name in ('skeleton.png','graph-overlay.png','pixel-graph.json')]:
        raise ValueError('Output must not overwrite source mask')
    with Image.open(source) as image:
        if image.width*image.height > 4_000_000:
            raise ValueError('Mask exceeds 4 million pixels')
        if image.mode not in ('1','L'):
            raise ValueError('Expected binary grayscale mask, not RGB screenshot')
        array = np.array(image.convert('L'))
    if not np.isin(array, [0,255]).all():
        raise ValueError('Mask must contain only 0 and 255')
    skeleton, iterations = thin(array > 0)
    report = analyze(skeleton)
    report.update({'thinning_iterations': iterations, 'mask_size': [array.shape[1], array.shape[0]],
                   'source_mask': source.name})
    overlay = Image.fromarray(array).convert('RGB')
    data = np.array(overlay)
    data[skeleton] = [255,0,80]
    overlay = Image.fromarray(data)
    draw = ImageDraw.Draw(overlay)
    for points, color in ((report['endpoints_xy'], '#00a050'), (report['junction_pixels_xy'], '#2050ff')):
        for x,y in points:
            draw.ellipse((x-2,y-2,x+2,y+2), fill=color)
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(skeleton.astype(np.uint8)*255).save(out/'skeleton.png')
    overlay.save(out/'graph-overlay.png')
    (out/'pixel-graph.json').write_text(json.dumps(report, indent=2, allow_nan=False)+'\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mask', required=True)
    parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    try:
        result = process(args.mask, args.output_dir)
    except (ValueError, OSError, Image.DecompressionBombError) as exc:
        parser.error(str(exc))
    print(json.dumps({k: result[k] for k in ('status','skeleton_pixels','component_count','cycle_rank')}))


if __name__ == '__main__':
    main()
