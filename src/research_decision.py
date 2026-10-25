"""调研收敛的局部证据与确定性护栏；空间关系不是截图配准或路线真值。"""
import re


def spatial(archive, discoveries=(), requests=()):
    objects = archive['objects']
    ways = [v['object'] for (kind, _), v in objects.items() if kind == 'way']
    result = []
    for way in ways[:128]:
        refs = way['nodes']
        points = [objects[('node', n)]['object'] for n in refs if ('node', n) in objects]
        road = bool(way.get('tags', {}).get('highway')) and way.get('tags', {}).get('area') != 'yes'
        item = {'id': way['id'], 'name': way.get('tags', {}).get('name'),
                'kind': 'road' if road else 'non_road_boundary',
                'node_ids': refs[:256], 'coordinates_complete': len(points) == len(refs),
                'bbox_wgs84': None, 'end_to_end_direction': 'unknown', 'connections': []}
        if points:
            item['bbox_wgs84'] = [min(p['lon'] for p in points), min(p['lat'] for p in points),
                                  max(p['lon'] for p in points), max(p['lat'] for p in points)]
        if len(points) == len(refs) and len(points) > 1 and refs[0] != refs[-1]:
            dx, dy = points[-1]['lon']-points[0]['lon'], points[-1]['lat']-points[0]['lat']
            item['end_to_end_direction'] = ('north' if dy > 0 else 'south' if dy < 0 else '') + ('east' if dx > 0 else 'west' if dx < 0 else '')
        if road:
            for other in ways:
                shared = sorted(set(refs) & set(other['nodes']))
                if other['id'] != way['id'] and other.get('tags', {}).get('highway') and other.get('tags', {}).get('area') != 'yes' and shared:
                    item['connections'].append({'way_id': other['id'], 'name': other.get('tags', {}).get('name'),
                                                'shared_node_ids': shared, 'needs_full': any(('node', n) not in objects for n in other['nodes'])})
        result.append(item)
    # 仅比较真实 bbox 中心的地理方位，不虚构屏幕投影、距离或道路相接。
    relations = []
    bounded = [r for r in result if r['bbox_wgs84']][:32]
    for i, a in enumerate(bounded):
        for b in bounded[i+1:]:
            aa, bb = a['bbox_wgs84'], b['bbox_wgs84']
            dx, dy = bb[0]+bb[2]-aa[0]-aa[2], bb[1]+bb[3]-aa[1]-aa[3]
            relations.append({'from_way': a['id'], 'to_way': b['id'],
                              'bbox_center_direction': ('north' if dy > 0 else 'south' if dy < 0 else '') + ('east' if dx > 0 else 'west' if dx < 0 else ''),
                              'screenshot_correspondence': 'unknown'})
    # 只给已有返回ID的可执行建议，不自动执行或强制模型走唯一分支。
    suggestions = []
    for item in result:
        if item['kind'] != 'road':
            continue
        if not item['coordinates_complete']:
            suggestions.append({'action':'inspect_map_object','object_type':'way','object_id':item['id'],
                'evidence':'Returned road lacks member coordinates; test local turn/relative-position hypothesis with way/full.'})
        else:
            connected = {n for c in item['connections'] for n in c['shared_node_ids']}
            for node in dict.fromkeys([item['node_ids'][0], item['node_ids'][-1]] if item['node_ids'] else []):
                if node not in connected:
                    suggestions.append({'action':'inspect_map_object','object_type':'node','object_id':node,
                        'evidence':'Returned road endpoint: test shared-node road continuation, not proximity. Select only if relevant to local screenshot turn.'})
    completed = {(r.get('request',{}).get('object_type'), r.get('request',{}).get('object_id')) for r in requests if r.get('outcome') == 'succeeded'}
    suggestions = [a for a in suggestions if (a['object_type'],a['object_id']) not in completed]
    return {'ways': result, 'relative_positions': relations, 'suggested_queries': suggestions[:16], 'discovery_bounds': list(discoveries)[-15:],
            'limitations': 'WGS84 object facts only. Partial bbox is not full extent. Direction follows stored node order, not travel. No screen projection/registration; north-up screenshot orientation must be checked. Proximity never proves screenshot identity or connectivity.'}


def conflict(state, action):
    if action.get('action') != 'search_map_evidence':
        return False
    evidence = action.get('evidence', '')
    if not isinstance(evidence, str):
        return False
    if re.search(r'判读有误|判作|读错|contradict|misread|previous.*wrong', evidence, re.I):
        return True
    names = action.get('names', [])
    if not isinstance(names, list):
        return False
    old = [n for r in state.get('research_requests', []) if r.get('outcome') == 'succeeded'
           for n in r.get('request', {}).get('names', [])]
    return any(isinstance(n, str) and n.endswith(('路', '街')) and len(n) == len(p) and n != p
               and sum(a == b for a, b in zip(n, p)) >= max(2, len(n)-1) for n in names for p in old)


def gate(state, archive, action):
    """返回可恢复反馈，不消耗地图预算；模型动作总预算仍然不变。"""
    # 安全/授权/工具形状仍交给既有严格执行器；门禁不能软化额外参数。
    name = action.get('action')
    required = {'action','names','evidence'} if name == 'search_map_evidence' else {'action','object_type','object_id','evidence'}
    allowed = required | ({'region'} if name == 'search_map_evidence' else set())
    if not required <= set(action) or set(action) - allowed:
        return None
    if name == 'inspect_map_object' and (not isinstance(action.get('object_type'),str) or type(action.get('object_id')) is not int):
        return None
    if name == 'search_map_evidence' and isinstance(action.get('names'), list) and len(action['names']) == 1 and isinstance(action['names'][0], str) and ('region' not in action or isinstance(action['region'], str)):
        from research_online import search_query, _successful_query
        from map_fetch import MapFetchError
        try:
            query = search_query(action).casefold()
        except MapFetchError:
            return None  # strict executor owns security validation
        if any(r.get('tool') == name and r.get('outcome') == 'succeeded' and _successful_query(r).casefold() == query for r in state.get('research_requests', [])):
            return {'status':'cached_search', 'next_step':'Already searched this normalized query; use existing discovery IDs and local spatial evidence. Do not repeat search or change region without new focus/evidence.'}
    if action.get('action') == 'search_map_evidence' and conflict(state, action):
        focus = state.get('research_focus', {})
        observation = state.get('review_region', {})
        if not (focus.get('preview_count') == state.get('review_preview_count', 0)
                and focus.get('preview_count', 0) > state.get('research_last_search_preview', 0)
                and focus.get('region') == observation.get('region_original_xyxy')):
            return {'status': 'needs_image_recheck', 'next_step': 'Conflicting text readings: observe_region on the disputed label, then research_focus with competing readings, explicit unknown/support/exclusion, and a falsifiable next query purpose. Do not silently switch names.'}
    if action.get('action') == 'inspect_map_object':
        kind, ident = action.get('object_type'), action.get('object_id')
        for record in state.get('research_requests', []):
            if record.get('outcome') == 'succeeded' and record.get('request') == {'object_type': kind, 'object_id': ident}:
                return {'status': 'cached_object', 'next_step': 'Already inspected; use current spatial evidence, do not repeat this endpoint.'}
        if kind == 'node':
            parents = [v['object'] for (k, _), v in archive['objects'].items() if k == 'way' and ident in v['object']['nodes']]
            if parents and not any(p.get('tags', {}).get('highway') and p.get('tags', {}).get('area') != 'yes' for p in parents):
                return {'status': 'non_road_boundary', 'next_step': 'Known parents are non-road boundaries, not intersections. No road connection is established. Inspect a discovered road way/full instead; do not spend node/ways on this parcel boundary.'}
    return None


def outcome(archive, action, result):
    ways = [v['object'] for v in result.get('objects', []) if v['object'].get('type') == 'way' and not v.get('discovery_only')]
    roads = [w['id'] for w in ways if w.get('tags', {}).get('highway') and w.get('tags', {}).get('area') != 'yes']
    return {'query_purpose': action.get('evidence'),
            'supported': {'returned_road_ids': roads, 'object_facts_only': True},
            'excluded': ['non-road objects cannot establish a road intersection'] if ways and not roads else [],
            'unknown': ['screenshot identity', 'ordered traversal including turnbacks', 'unreturned roads'],
            'next_step': 'Compare connections and WGS84 bbox directions with a localized screenshot hypothesis; fetch relevant connecting way/full if coordinates missing. Record what would falsify each competing reading. A miss/failure excludes no road.'}
