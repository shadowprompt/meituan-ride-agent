"""离线、确定性道路走廊候选；不是GPS恢复或导航。

Python接口：inspect_roads(osm_path, [道路名]) 查本地对象；
build_corridor(osm_path, hypothesis, output_dir=None) -> {geojson, report}。
CLI：python3 src/road_corridor.py inspect --osm local.osm --name 道路名
     python3 src/road_corridor.py build --osm local.osm --hypothesis plan.json --output out

hypothesis严格schema（不接受任何自由经纬度、路径或代码）：
{"schema_version":1,"coordinate_system":"WGS84",
 "source":{"kind":"user"或"model","evidence":"道路序列的假设依据"},
 "legs":[{"road":{"name":"道路名"},"via":[{"node_id":1},{"node_id":2}]}]}
road也可用{"way_ids":[123,456]}，或同时提供name与way_ids（逐条核对名称）。
每段via至少两个节点，按顺序分别寻路；相邻段必须共用同一个边界节点。
折返须明确重复via/legs，不会被合并为一次全局最短路。无最近点吸附。
所有坐标仅取自指定OSM节点；每条输出边都记录原way_id。调用者须在
工具层将osm_path/output_dir映射到已授权本地文件，不能放任模型指定任意路径。
输入/输出均无网络行为；OSM通行标签只是保守过滤，不构成骑行合法性验证。
"""
import argparse
import hashlib
import heapq
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET

MAX_OSM_BYTES = 30_000_000
MAX_PLAN_BYTES = 256_000
MAX_NODES = 100_000
MAX_WAYS = 50_000
MAX_REFS = 400_000
MAX_EXPANSIONS = 1_000_000
MAX_PATH_NODES = 100_000
ALLOWED_HIGHWAYS = frozenset(('residential', 'tertiary', 'secondary', 'primary',
    'unclassified', 'living_street', 'cycleway', 'service', 'road', 'track',
    'path', 'footway', 'pedestrian', 'tertiary_link', 'secondary_link', 'primary_link'))
WARNINGS = [
    '仅为有序道路假设产生的候选，不是原始GPS，也未证明实际骑行路线。',
    '起点和终点均为未确认的OSM节点，不代表截图精确起终点；未推算像素误差范围。',
    '无向路网仅验证几何连通；未验证单行、转向限制、路口过街、层级和实时骑行权限。',
    '每对必经点间采用指定道路内最短路径，未指定的细节仍需模型叠图回看或用户确认。',
    '叠图仅为本地OSM示意图，不是原截图配准叠图，不构成视觉验收。',
]


class CorridorError(ValueError):
    """可向调用方安全展示的输入/路径/预算错误。"""


def _require(ok, message):
    if not ok:
        raise CorridorError(message)


def _id(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value < 2**63


def _text(value, maximum=512):
    return isinstance(value, str) and 0 < len(value.strip()) <= maximum and not any(ord(c) < 32 for c in value)


def _keys(value, allowed, required):
    _require(isinstance(value, dict) and set(required) <= set(value) <= set(allowed),
             '对象字段不符合schema（不接受自由坐标或未知字段）')


def _read(path, limit):
    try:
        p = Path(path)
        _require(p.is_file() and p.stat().st_size <= limit, '本地输入不存在、不是普通文件或超过大小限制')
        with p.open('rb') as stream:
            raw = stream.read(limit + 1)
        _require(len(raw) <= limit, '输入超过大小限制')
        return raw
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, CorridorError):
            raise
        raise CorridorError('无法读取指定本地文件') from exc


def _distance(a, b):
    # 球面距离只作候选权重，不更改输入节点位置。
    x, y = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(y / 2)**2 + math.cos(math.radians(a[1])) * math.cos(math.radians(b[1])) * math.sin(x / 2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1., max(0., h))))


def _load(osm_path):
    raw = _read(osm_path, MAX_OSM_BYTES)
    _require(b'<!DOCTYPE' not in raw.upper() and b'<!ENTITY' not in raw.upper(), '不支持XML实体或DTD')
    try:
        root = ET.fromstring(raw)
        _require(root.tag == 'osm', '需要OSM XML根节点')
        nodes, ways, refs = {}, {}, 0
        for el in root.findall('node'):
            key = int(el.attrib['id'])
            lon, lat = float(el.attrib['lon']), float(el.attrib['lat'])
            _require(_id(key) and key not in nodes and len(nodes) < MAX_NODES, 'OSM节点ID重复、非法或超过预算')
            _require(math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -85 <= lat <= 85,
                     'OSM坐标非有限WGS84或超出范围')
            nodes[key] = (lon, lat)
        for el in root.findall('way'):
            key = int(el.attrib['id'])
            _require(_id(key) and key not in ways and len(ways) < MAX_WAYS, 'OSM道路ID重复、非法或超过预算')
            ids = [int(n.attrib['ref']) for n in el.findall('nd')]
            refs += len(ids)
            _require(refs <= MAX_REFS and all(_id(n) for n in ids), 'OSM引用非法或超过预算')
            tags = {}
            for tag in el.findall('tag'):
                k, v = tag.get('k'), tag.get('v')
                _require(_text(k, 255) and isinstance(v, str) and len(v) <= 4096 and k not in tags,
                         'OSM标签非法或重复')
                tags[k] = v
            h = tags.get('highway')
            allowed = (h in ALLOWED_HIGHWAYS and tags.get('bicycle') not in ('no', 'private', 'use_sidepath')
                       and tags.get('access') not in ('no', 'private')
                       and tags.get('vehicle') not in ('no', 'private')
                       and tags.get('area') != 'yes'
                       and (h not in ('footway', 'pedestrian') or tags.get('bicycle') in ('yes', 'designated', 'permissive')))
            ways[key] = {'nodes': ids, 'tags': tags, 'eligible': allowed}
    except (ET.ParseError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, CorridorError):
            raise
        raise CorridorError('OSM XML格式、节点或道路字段非法') from exc
    return nodes, ways, hashlib.sha256(raw).hexdigest()


def _validate(plan):
    try:
        encoded = json.dumps(plan, ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
        raise CorridorError('假设必须是有限、可编码的JSON对象') from exc
    _require(len(encoded) <= MAX_PLAN_BYTES, '假设输入超过256KB')
    _keys(plan, ('schema_version', 'coordinate_system', 'source', 'legs'), ('schema_version', 'coordinate_system', 'source', 'legs'))
    _require(type(plan['schema_version']) is int and plan['schema_version'] == 1, '仅支持schema_version=1')
    _require(plan['coordinate_system'] == 'WGS84', '仅支持显式WGS84')
    _keys(plan['source'], ('kind', 'evidence'), ('kind', 'evidence'))
    _require(plan['source']['kind'] in ('user', 'model') and _text(plan['source']['evidence'], 4096), '需要假设来源及依据')
    legs = plan['legs']
    _require(isinstance(legs, list) and 1 <= len(legs) <= 64, '需要1..64个有序路段')
    count = 0
    for leg in legs:
        _keys(leg, ('road', 'via'), ('road', 'via'))
        road = leg['road']
        _keys(road, ('name', 'way_ids'), ())
        _require(bool(road), '需要道路名或way_ids')
        if 'name' in road:
            _require(_text(road['name'], 255), '道路名称非法')
        if 'way_ids' in road:
            ids = road['way_ids']
            _require(isinstance(ids, list) and 1 <= len(ids) <= 256 and all(_id(n) for n in ids)
                     and len(set(ids)) == len(ids), 'way_ids必须是1..256个唯一正整数')
        via = leg['via']
        _require(isinstance(via, list) and 2 <= len(via) <= 128, '每段需要2..128个有序OSM必经节点')
        count += len(via)
        _require(count <= 512, '必经节点总预算512')
        for point in via:
            _keys(point, ('node_id',), ('node_id',))
            _require(_id(point['node_id']), '必经点只能引用正整数OSM node_id，不能提供浮点坐标')
    return hashlib.sha256(encoded).hexdigest()


def inspect_roads(osm_path, names):
    """精确名称查询本地道路、节点和交叉口；帮助模型引用真实ID而非发明坐标。"""
    _require(isinstance(names, list) and 1 <= len(names) <= 16 and all(_text(n, 255) for n in names),
             '查询需要1..16个精确道路名')
    nodes, ways, digest = _load(osm_path)
    selected = {k: w for k, w in ways.items() if w['tags'].get('name') in names}
    _require(len(selected) <= 512, '查询道路过多，请使用更小本地OSM')
    ids = {n for w in selected.values() for n in w['nodes'] if n in nodes}
    _require(len(ids) <= 5000, '查询节点超过5000，请使用更小本地OSM')
    membership = {}
    for w in selected.values():
        if w['eligible']:
            for n in w['nodes']:
                if n in nodes:
                    membership.setdefault(n, set()).add(w['tags']['name'])
    return {'schema_version': 1, 'osm_sha256': digest, 'coordinate_system': 'WGS84',
            'verified': False, 'original_gps': False,
            'missing_names': [n for n in names if not any(w['tags'].get('name') == n for w in selected.values())],
            'ways': [{'way_id': k, 'name': w['tags'].get('name'), 'eligible': w['eligible'], 'node_ids': w['nodes']}
                     for k, w in sorted(selected.items())],
            'nodes': [{'node_id': n, 'coordinates': list(nodes[n])} for n in sorted(ids)],
            'intersections': [{'node_id': n, 'names': sorted(ns)} for n, ns in sorted(membership.items()) if len(ns) > 1]}


def _graph(road, nodes, ways):
    if 'way_ids' in road:
        selected = road['way_ids']
        _require(all(k in ways for k in selected), '指定way_id不存在')
        _require(all(ways[k]['eligible'] for k in selected), '指定道路不满足保守骑行权限/道路类型过滤')
        if 'name' in road:
            _require(all(ways[k]['tags'].get('name') == road['name'] for k in selected), 'way_id与道路名不一致')
    else:
        selected = [k for k, w in ways.items() if w['tags'].get('name') == road['name'] and w['eligible']]
        _require(bool(selected), '道路名不存在或没有符合权限过滤的道路')
    graph = {}
    for key in sorted(selected):
        for a, b in zip(ways[key]['nodes'], ways[key]['nodes'][1:]):
            # 缺失引用切断道路，不能跨过它补边；地理相交也不会凭空连通。
            if a not in nodes or b not in nodes or a == b:
                continue
            distance = _distance(nodes[a], nodes[b])
            _require(distance <= 5000, 'OSM单边超过5km，拒绝不可靠局部候选')
            graph.setdefault(a, []).append((b, distance, key))
            graph.setdefault(b, []).append((a, distance, key))
    return graph, sorted(selected)


def _shortest(graph, start, end, budget):
    _require(start in graph and end in graph, '必经节点不在指定道路可用边上；禁止吸附到最近道路')
    if start == end:
        return [start], [], 0.
    queue, costs, previous = [(0., start)], {start: 0.}, {}
    while queue:
        cost, node = heapq.heappop(queue)
        if cost != costs[node]:
            continue
        if node == end:
            break
        for target, weight, way in graph[node]:
            budget[0] -= 1
            _require(budget[0] >= 0, '路径计算超过边扩展预算')
            value = cost + weight
            if value < costs.get(target, math.inf):
                costs[target] = value
                previous[target] = (node, way)
                heapq.heappush(queue, (value, target))
    _require(end in previous, '指定道路内不连通；拒绝跨道路偷接或跨缺失节点补线')
    path, way_ids = [end], []
    while path[-1] != start:
        node, way = previous[path[-1]]
        path.append(node)
        way_ids.append(way)
        _require(len(path) <= MAX_PATH_NODES, '路径节点超过预算')
    return path[::-1], way_ids[::-1], costs[end]


def build_corridor(osm_path, hypothesis, output_dir=None, *, max_expansions=MAX_EXPANSIONS):
    """验证假设并逐段构建；失败抛CorridorError，不返回部分成功或擅自修正假设。

    output_dir=None时不写文件；否则导出三个固定文件且禁止覆盖已有文件。
    返回geojson/report及可选artifacts。上层仍须独立处理上传同意及视觉复核gate。
    """
    _require(type(max_expansions) is int and 1 <= max_expansions <= MAX_EXPANSIONS, '计算预算必须为1..1000000整数')
    plan_hash = _validate(hypothesis)
    nodes, ways, osm_hash = _load(osm_path)
    budget, all_nodes, all_ways, legs = [max_expansions], [], [], []
    total_length = 0.
    for index, leg in enumerate(hypothesis['legs']):
        via = [v['node_id'] for v in leg['via']]
        _require(all(n in nodes for n in via), '必经node_id不在本地OSM中')
        if all_nodes:
            _require(all_nodes[-1] == via[0], '相邻路段边界必须是同一个OSM节点，禁止断裂连接或自动吸附')
        graph, selected = _graph(leg['road'], nodes, ways)
        path, edge_ways, length = [], [], 0.
        for start, end in zip(via, via[1:]):
            part, used, distance = _shortest(graph, start, end, budget)
            path.extend(part if not path else part[1:])
            edge_ways.extend(used)
            length += distance
            _require(len(path) + len(all_nodes) <= MAX_PATH_NODES, '输出节点超过预算')
        _require(len(path) >= 2 and length > 0, '路段不能全为零长度，折返必须指定转折节点')
        total_length += length
        _require(total_length <= 100_000, '候选总长超过100km')
        _require(all(_distance(nodes[path[0]], nodes[n]) <= 25_000 for n in path), '单路段地理跨度超过25km')
        offset = max(0, len(all_nodes) - 1)
        all_nodes.extend(path if not all_nodes else path[1:])
        all_ways.extend(edge_ways)
        legs.append({'index': index, 'road': leg['road'], 'selected_way_ids': selected,
                     'via_node_ids': via, 'path_node_ids': path, 'edge_way_ids': edge_ways,
                     'start_vertex_index': offset, 'end_vertex_index': len(all_nodes) - 1,
                     'length_m': round(length, 3), 'connected': True})
    properties = {'verified': False, 'original_gps': False, 'navigation_verified': False,
                  'coordinate_system': 'WGS84', 'method': 'ordered_osm_road_corridor',
                  'source': hypothesis['source'], 'osm_sha256': osm_hash,
                  'hypothesis_sha256': plan_hash, 'start_confirmed': False, 'end_confirmed': False,
                  'length_m': round(total_length, 3), 'attribution': '© OpenStreetMap contributors',
                  'warnings': WARNINGS.copy()}
    geojson = {'type': 'FeatureCollection', 'features': [
        {'type': 'Feature', 'properties': properties.copy(),
         'geometry': {'type': 'LineString', 'coordinates': [list(nodes[n]) for n in all_nodes]}}]}
    report = dict(properties, schema_version=1, status='candidate_needs_review',
                  legs=legs, path_node_ids=all_nodes, edge_way_ids=all_ways,
                  expansions_used=max_expansions - budget[0],
                  endpoints={'start': {'node_id': all_nodes[0], 'confirmed': False},
                             'end': {'node_id': all_nodes[-1], 'confirmed': False}},
                  uncertainty={'endpoint_error_m': None, 'route_error_m': None,
                               'reason': '未配准原截图，无法从本地道路假设估计骑行位置误差'})
    result = {'geojson': geojson, 'report': report}
    if output_dir is not None:
        result['artifacts'] = _export(output_dir, geojson, report, nodes, ways)
    return result


def _render(nodes, ways, report):
    from PIL import Image, ImageDraw
    coords = [nodes[n] for n in report['path_node_ids']]
    west, east = min(p[0] for p in coords), max(p[0] for p in coords)
    south, north = min(p[1] for p in coords), max(p[1] for p in coords)
    coslat = math.cos(math.radians((south + north) / 2))
    width, height = max((east - west) * coslat, .0001), max(north - south, .0001)
    scale = min(1100 / width, 700 / height)
    def pixel(p):
        return (90 + (p[0] - west) * coslat * scale, 120 + (north - p[1]) * scale)
    image = Image.new('RGB', (1280, 960), '#f7f7f4')
    draw = ImageDraw.Draw(image)
    # 仅绘制视口内边；底图边数受输入引用预算限制。
    for way in ways.values():
        if not way['tags'].get('highway'):
            continue
        for a, b in zip(way['nodes'], way['nodes'][1:]):
            if a in nodes and b in nodes:
                pa, pb = pixel(nodes[a]), pixel(nodes[b])
                if all(-5000 < v < 6000 for p in (pa, pb) for v in p):
                    draw.line((pa, pb), fill='#cbd0d3', width=2)
    draw.line([pixel(p) for p in coords], fill='#e23a49', width=5)
    for index, leg in enumerate(report['legs']):
        p = pixel(nodes[leg['via_node_ids'][0]])
        draw.ellipse((p[0]-6, p[1]-6, p[0]+6, p[1]+6), fill='#008040')
        draw.text((p[0]+8, p[1]+5), str(index + 1), fill='black')
    for label, point in [('START?', coords[0]), ('END?', coords[-1])]:
        x, y = pixel(point)
        draw.text((x+8, y-18 if label == 'START?' else y+18), label, fill='black')
    draw.rectangle((0, 0, 1280, 90), fill='white')
    draw.text((20, 15), 'LOCAL OSM SCHEMATIC - UNVERIFIED CANDIDATE / NOT ORIGINAL GPS', fill='black')
    draw.text((20, 40), 'Not screenshot alignment. Direction/access/turns NOT navigation-verified.', fill='black')
    draw.text((20, 65), '(c) OpenStreetMap contributors. Repeated edges overlap; see ordered node IDs in report.', fill='black')
    return image


def _export(output_dir, geojson, report, nodes, ways):
    created = []
    try:
        folder = Path(output_dir)
        folder.mkdir(parents=True, exist_ok=True)
        targets = {k: folder / v for k, v in [('geojson', 'road-corridor.geojson'),
                   ('report', 'road-corridor.json'), ('overlay', 'road-corridor.png')]}
        _require(all(not p.exists() and not p.is_symlink() for p in targets.values()), '输出已存在，拒绝覆盖')
        # 先完成渲染和编码，写入使用x模式防止竞态覆盖；任何失败清理本次文件。
        image = _render(nodes, ways, report)
        for kind, value in [('geojson', geojson), ('report', report)]:
            with targets[kind].open('x', encoding='utf-8') as stream:
                created.append(targets[kind])
                json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        with targets['overlay'].open('xb') as stream:
            created.append(targets['overlay'])
            image.save(stream, format='PNG')
        return {k: str(v) for k, v in targets.items()}
    except (OSError, TypeError, ValueError, ImportError) as exc:
        for p in created:
            p.unlink(missing_ok=True)
        if isinstance(exc, CorridorError):
            raise
        raise CorridorError('无法导出候选文件（需要可写目录与Pillow），本次文件已清理') from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--osm', required=True)
    inspect.add_argument('--name', action='append', required=True)
    build = commands.add_parser('build')
    build.add_argument('--osm', required=True)
    build.add_argument('--hypothesis', required=True)
    build.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'inspect':
            result = inspect_roads(args.osm, args.name)
        else:
            raw = _read(args.hypothesis, MAX_PLAN_BYTES)
            try:
                plan = json.loads(raw)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise CorridorError('假设文件不是有效JSON') from exc
            built = build_corridor(args.osm, plan, args.output)
            result = {'status': built['report']['status'], 'verified': False, 'original_gps': False,
                      'artifacts': built['artifacts']}
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except CorridorError as exc:
        print(json.dumps({'status': 'rejected', 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
