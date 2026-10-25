"""调研收敛的局部证据与确定性护栏；空间关系不是截图配准或路线真值。"""
import re


def location_note(action):
    """Bounded model note; never promotes a claim into map truth or permission."""
    from route_review import text
    if set(action) != {'action','anchors','query_plan','map_comparison','image_comparison','status','next_step'}:
        raise ValueError('Location note schema required')
    if action['status'] not in ('unlocated','candidate','ambiguous'):
        raise ValueError('Unknown location status')
    anchors = action['anchors']
    if not isinstance(anchors,list) or not 2 <= len(anchors) <= 8:
        raise ValueError('Multiple bounded anchors required')
    for anchor in anchors:
        if not isinstance(anchor,dict) or set(anchor) != {'label','kind','image_evidence'} or anchor['kind'] not in ('road','landmark','structure'):
            raise ValueError('Independent image anchor schema required')
        text(anchor['label'],100)
        text(anchor['image_evidence'],1000)
    if not isinstance(action['query_plan'],list) or not 1 <= len(action['query_plan']) <= 3:
        raise ValueError('Limited discriminating query plan required')
    for query in action['query_plan']: text(query,100)
    for field in ('map_comparison','image_comparison','next_step'): text(action[field],2000)
    return {**action,'verified':False}


def discovery_comparisons(discoveries):
    """Compare actual search extents across distinct queries, NOT screen projections."""
    import math
    pairs = []
    for i,a in enumerate(discoveries):
        for b in discoveries[i+1:]:
            if not a.get('query') or not b.get('query') or a['query'] == b['query']:
                continue
            aa,bb = a['bbox_wgs84'],b['bbox_wgs84']
            x,y = (aa[0]+aa[2])/2,(aa[1]+aa[3])/2
            xx,yy = (bb[0]+bb[2])/2,(bb[1]+bb[3])/2
            distance = 6371*2*math.asin(min(1,math.sqrt(math.sin(math.radians(yy-y)/2)**2 + math.cos(math.radians(y))*math.cos(math.radians(yy))*math.sin(math.radians(xx-x)/2)**2)))
            pairs.append({'from':[a['type'],a['id']], 'to':[b['type'],b['id']],
                'queries':[a['query'],b['query']], 'bbox_center_distance_km':round(distance,3),
                'map_center_direction':('north' if yy>y else 'south' if yy<y else '')+('east' if xx>x else 'west' if xx<x else ''),
                'screenshot_match':'unknown', 'notice':'Search extents only; long road centers can mislead. Compare extent and image structure, not distance alone.'})
    return pairs[:64]


def location_snapshot(state):
    """笔记按对象版本+成功/失败查询次数标记过期；不擅自改成定位成功。"""
    note = state.get('research_location')
    if not note:
        return None
    basis = [state.get('research_version'), len(state.get('research_requests', []))]
    stale = note.get('evidence_basis') != basis
    return {**note, 'stale':stale, 'current_evidence_basis':basis,
            'status': 'stale' if stale else note.get('status', 'unlocated'),
            'recorded_status':note.get('status'),
            'notice':'Historical model note; consult current returned evidence, not an automatic localization decision.'}


def road_seed_plan(state, archive):
    """地标定位与道路种子分开规划，不固定2+1配额，不把闭合地块当道路入口。"""
    discoveries = state.get('research_discoveries', [])
    roads = [v['object']['id'] for (k,_),v in archive['objects'].items() if k == 'way'
             and v['object'].get('tags',{}).get('highway') and v['object'].get('tags',{}).get('area') != 'yes']
    road_candidates = [d for d in discoveries if d.get('type') == 'way' and d.get('candidate_kind') == 'road_candidate'
                       and (('way',d['id']) not in archive['objects'] or d['id'] in roads)]
    anchors = (state.get('research_location') or {}).get('anchors', [])
    searched = [n for r in state.get('research_requests',[]) for n in r.get('request',{}).get('names',[])]
    queries = [{'action':'search_map_evidence','names':[a['label']],
                'evidence':a['image_evidence']} for a in anchors
               if a.get('kind') == 'road' and a.get('label') not in searched]
    searches = state.get('research_budget_used',{}).get('searches',0)
    no_seed = not roads and not road_candidates
    return {'road_ids':roads, 'road_candidates':road_candidates, 'road_search_options':queries,
            'landmark_queries_used': [n for n in searched if any(a.get('label') == n and a.get('kind') != 'road' for a in anchors)],
            'risk':'search_exhausted_without_road_seed' if no_seed and searches >= 3 else 'no_road_seed' if no_seed else None,
            'stop_without_road_seed':bool(no_seed and searches >= 3 and discoveries and
                all(d.get('candidate_kind') == 'non_road' or (('way',d['id']) in archive['objects']
                    and d['id'] not in roads) for d in discoveries)),
            'next_step':'Usually 1-2 effective independent clues suffice for a locality hypothesis; a road can itself cross-check locality. Reserve remaining searches and requests for a relevant readable road and way/full, not a third redundant landmark or parcel-node crawling. Additional landmark queries are justified only by a concrete unresolved locality discriminator. No fixed allocation; unknown return types remain unknown.'}


def planning_evidence(state, archive, consent=False):
    """规划输入仅来自真实已返回对象；建议不是授权，也不是截图对应结论。"""
    import research_plan
    plan = research_plan.ledger(state)
    facts = spatial(archive, state.get('research_discoveries', []), state.get('research_requests', []))
    attempted = {(r.get('request', {}).get('object_type'), r.get('request', {}).get('object_id'))
                 for r in state.get('research_requests', []) if r.get('tool') == 'inspect_map_object'}
    archived = archive['objects']
    nonroads = {(d['type'],d['id']) for d in state.get('research_discoveries',[]) if d.get('candidate_kind') == 'non_road'}
    candidates = [{'action':'inspect_map_object','object_type':'way','object_id':ident,
                   'evidence':'Returned uninspected candidate only; test tags and full geometry against a specific competing screenshot clue, not assumed identity.'}
                  for kind, ident in state.get('research_seen_ids', [])
                  if kind == 'way' and ('way',ident) not in nonroads and ('way', ident) not in archived and ('way', ident) not in attempted]
    suggestions = candidates + [a for a in facts['suggested_queries']
                               if (a['object_type'], a['object_id']) not in attempted]
    used = state.get('research_budget_used', {})
    network_allowed = (consent is True and state.get('research_online') is True
                       and plan['phase'] != 'closure'
                       and used.get('requests', 0) < 8 and plan['counts']['network'] < 8)
    comparisons = discovery_comparisons(state.get('research_discoveries', []))
    searched = [r.get('request',{}).get('names',[]) for r in state.get('research_requests',[]) if r.get('tool') == 'search_map_evidence']
    note = location_snapshot(state) or {}
    seed = road_seed_plan(state, archive)
    localized = note.get('status') == 'candidate'
    # Before a candidate locality exists, generic endpoint expansion is a bad default.
    # Keep all real candidates accessible, but stop actively recommending blind crawling.
    if not localized and used.get('searches',0) < 3:
        suggestions = [a for a in suggestions if a['object_type'] == 'way']
    return {'road_seed_plan':seed, 'localization':{'status':note.get('status','unlocated'), 'recorded_status':note.get('recorded_status'), 'stale':note.get('stale',False), 'evidence_basis':note.get('evidence_basis'), 'model_claim_only':True,
                'searched_names':searched, 'anchors':note.get('anchors',[]),
                'priority':('Test the route-bearing geometry and alternatives in the candidate locality.' if localized else
                    seed['next_step']),
                'geometry_warning':'Do not spend remaining requests on serial background-road segments; retain unknown branches.'},
            'discovery_comparisons':comparisons,
            'uninspected_candidates':candidates[:32], 'known_geometry':facts['ways'],
            'network_suggestions':suggestions[:32], 'network_allowed_this_invocation':network_allowed,
            'remaining':{'model':max(0, research_plan.MAX_MODEL_CALLS-plan['model_calls']),
                         'requests':max(0, 8-used.get('requests', 0)),
                         'searches':max(0, 3-used.get('searches', 0)),
                         'categories':{k:max(0, v-plan['counts'][k]) for k,v in research_plan.LIMITS.items()}},
            'observed_regions':research_plan.convergence(state)['observations'],
            'observation_purposes':[m.get('content') for m in state.get('messages', [])
                                    if m.get('role') == 'assistant' and 'observe_region' in str(m.get('content'))][-6:],
            'focus':state.get('research_focus'),
            'notice':'Candidate evidence only. Map-relative positions are not screenshot positions without independent registration. Local comparison and network plans are separate; real seen IDs, fresh separate consent, deduplication and all existing gates still apply.'}


def node_geometry(way, objects, requests, connections):
    """Consumable real coordinates in stored order, not inferred junctions or projection."""
    import math
    refs = way['nodes']
    available = [objects[('node', n)]['object'] for n in refs if ('node', n) in objects]
    if not available:
        return {'nodes':[], 'node_summary_truncated':False}
    west, south = min(p['lon'] for p in available), min(p['lat'] for p in available)
    east, north = max(p['lon'] for p in available), max(p['lat'] for p in available)
    cx, cy = (west+east)/2, (south+north)/2
    scale_x, scale_y = 111320*math.cos(math.radians(cy)), 111320
    checked = {r.get('request',{}).get('object_id') for r in requests
               if r.get('tool') == 'inspect_map_object' and r.get('outcome') == 'succeeded'
               and r.get('request',{}).get('object_type') == 'node'}
    nodes = []
    for i, ident in enumerate(refs[:256]):
        p = objects.get(('node',ident),{}).get('object')
        if not p:
            nodes.append({'id':ident,'index':i,'coordinates':'missing'})
            continue
        turn = None
        if 0 < i < len(refs)-1:
            a = objects.get(('node',refs[i-1]),{}).get('object')
            b = objects.get(('node',refs[i+1]),{}).get('object')
            if a and b:
                u = ((p['lon']-a['lon'])*scale_x,(p['lat']-a['lat'])*scale_y)
                v = ((b['lon']-p['lon'])*scale_x,(b['lat']-p['lat'])*scale_y)
                if math.hypot(*u) and math.hypot(*v):
                    turn = round(math.degrees(math.atan2(u[0]*v[1]-u[1]*v[0],u[0]*v[0]+u[1]*v[1])),1)
        nodes.append({'id':ident,'index':i,'wgs84':[p['lon'],p['lat']],
            'offset_from_way_bbox_center_m':[round((p['lon']-cx)*scale_x,1),round((p['lat']-cy)*scale_y,1)],
            'role_in_returned_way':'endpoint' if i in (0,len(refs)-1) else 'interior',
            'bend_degrees':turn, 'tags':p.get('tags',{}),
            'known_connected_way_ids':[c['way_id'] for c in connections if ident in c['shared_node_ids']],
            'parent_membership':'queried' if ident in checked else 'unknown_not_queried',
            'junction_status':'shared_node_proven' if any(ident in c['shared_node_ids'] for c in connections) else 'unknown',
            'query_selection':'not_recommended_by_default; require independent locality and specific image turn/relative-position rationale'})
    return {'nodes':nodes,'node_summary_truncated':len(refs)>256,
        'node_count':len(refs),'offset_rule':'[east,north] meters from returned way bbox center; map-internal only',
        'connection_test':{'question':'At a spatially justified returned node, does node/ways reveal a distinct connecting road compatible with the separately observed transverse/turning image leg?',
            'required_rationale':'Explain why this node location or bend discriminates competing screenshot structures after independent locality comparison; endpoint or crossing tag alone is insufficient. Reject remote/incompatible nodes without querying.',
            'falsifier':'No distinct road parent, or its full geometry has incompatible direction/relative position: reject this connection hypothesis, not all nearby roads.',
            'limits':'node/ways proves membership only; connecting way/full is needed for missing coordinates. A tagged crossing is not proof of a junction. No automatic query or screenshot registration.'}}


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
        if road:
            item.update(node_geometry(way, objects, requests, item['connections']))
        if item['bbox_wgs84']:
            # Independent search bounds compared to actual road extent, never pixels.
            bounds = {'type':'way','id':way['id'],'query':'returned road geometry',
                      'bbox_wgs84':item['bbox_wgs84']}
            item['discovery_relations'] = discovery_comparisons([bounds]+list(discoveries))[:len(discoveries)]
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
    # Nodes are selectable evidence, NOT default endpoint-crawl recommendations.
    # A model must supply the independently supported spatial/turn discriminator.
    suggestions = []
    for item in result:
        if item['kind'] == 'road' and not item['coordinates_complete']:
            suggestions.append({'action':'inspect_map_object','object_type':'way','object_id':item['id'],
                'evidence':'Returned road lacks member coordinates; test local turn/relative-position hypothesis with way/full.'})
    completed = {(r.get('request',{}).get('object_type'), r.get('request',{}).get('object_id')) for r in requests if r.get('outcome') == 'succeeded'}
    suggestions = [a for a in suggestions if (a['object_type'],a['object_id']) not in completed]
    return {'ways': result, 'relative_positions': relations, 'suggested_queries': suggestions[:16], 'discovery_bounds': list(discoveries)[-15:],
            'limitations': 'WGS84 object facts only. Partial bbox is not full extent. Direction follows stored node order, not travel. No screen projection/registration; north-up screenshot orientation must be checked. Proximity never proves screenshot identity or connectivity. A rejected ridden road can remain a location reference, not an automatic node-query seed.'}


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
        # 三次发现额度不应被同名加区县耗尽；未取得道路几何不等于道路不存在。
        normalized_name = ' '.join(action['names'][0].split()).casefold()
        repeated_name = any(r.get('tool') == name and r.get('outcome') == 'succeeded'
                            and any(isinstance(n, str) and ' '.join(n.split()).casefold() == normalized_name
                                    for n in r.get('request', {}).get('names', []))
                            for r in state.get('research_requests', []))
        seed = road_seed_plan(state, archive)
        note = state.get('research_location') or {}
        label = action['names'][0]
        landmark = any(a.get('label') == label and a.get('kind') == 'landmark' for a in note.get('anchors', []))
        road_planned = any(q == a.get('label') for q in note.get('query_plan', [])
                           for a in note.get('anchors', []) if a.get('kind') == 'road')
        # 仅明确“最后搜索仍是地标且无道路计划”时给一次规划反馈；不引入focus仪式。
        import research_plan
        plan = research_plan.ledger(state)
        if (state.get('research_budget_used',{}).get('searches',0) == 2 and seed['risk'] == 'no_road_seed'
                and landmark and not road_planned and not plan.get('road_planning_feedback')):
            plan['road_planning_feedback'] = True
            return {'status':'road_seed_planning', 'planning_feedback':True, 'road_seed_plan':seed,
                    'next_step':'No request sent. This would consume the last search on another landmark with no road seed or road plan. Reconsider a relevant readable road from current image evidence, or explain a concrete locality ambiguity that requires this landmark; the next valid choice is not forced to a fixed allocation. No focus action required.'}
        if repeated_name:
            focus = state.get('research_focus') or {}
            claims = focus.get('hypotheses', [])
            fresh = (focus.get('preview_count', 0) > state.get('research_last_search_preview', 0)
                     and focus.get('region') == state.get('review_region', {}).get('region_original_xyxy')
                     and focus.get('region') is not None)
            independent = any(isinstance(h, dict) and action['names'][0] not in h.get('claim', '')
                              for h in claims)
            note = state.get('research_location') or {}
            documented = (note.get('status') == 'candidate' and len(note.get('anchors',[])) >= 2
                          and note.get('map_comparison') and note.get('image_comparison'))
            if not (documented or (fresh and len(claims) >= 2 and independent and focus.get('falsifier'))):
                return {'status':'search_diversity_recheck',
                        'next_step':'No request sent; search quota unchanged. A Nominatim miss/place hit does NOT exclude a road. Prefer another independent visible label. Before retrying the same name with another region, document the independent image and returned-map comparison in research_location (or reuse a relevant existing focus); no duplicate observation ritual is required. Do not ask the user to supply roads. Candidate map-relative positions never establish screenshot-relative positions.'}
    if action.get('action') == 'inspect_map_object':
        kind, ident = action.get('object_type'), action.get('object_id')
        for record in state.get('research_requests', []):
            if record.get('outcome') == 'succeeded' and record.get('request') == {'object_type': kind, 'object_id': ident}:
                return {'status': 'cached_object', 'next_step': 'Already inspected; use current spatial evidence, do not repeat this endpoint.'}
        if kind == 'way' and any(d.get('type') == kind and d.get('id') == ident and d.get('candidate_kind') == 'non_road' for d in state.get('research_discoveries', [])):
            return {'status':'non_road_candidate', 'next_step':'Returned category already identifies a non-road. Use its bbox for locality; do not fetch full merely to exclude a park or crawl parcel nodes. Choose a relevant road seed.'}
        if kind == 'node':
            parents = [v['object'] for (k, _), v in archive['objects'].items() if k == 'way' and ident in v['object']['nodes']]
            road_node = any(d.get('type') == 'node' and d.get('id') == ident and d.get('candidate_kind') == 'road_candidate' for d in state.get('research_discoveries', []))
            if not road_node and not any(p.get('tags', {}).get('highway') and p.get('tags', {}).get('area') != 'yes' for p in parents):
                return {'status': 'non_road_boundary', 'next_step': 'No returned road parent or highway-node evidence; parcel/place nodes are not intersections. No road connection is established. Inspect a discovered road way/full instead; do not spend node/ways on this parcel boundary.'}
    return None


def outcome(archive, action, result):
    ways = [v['object'] for v in result.get('objects', []) if v['object'].get('type') == 'way' and not v.get('discovery_only')]
    roads = [w['id'] for w in ways if w.get('tags', {}).get('highway') and w.get('tags', {}).get('area') != 'yes']
    hits = result.get('objects', [])
    discovery = [v for v in hits if v.get('discovery_only')]
    road_hits = [v['object']['id'] for v in discovery if v.get('candidate_kind') == 'road_candidate']
    search_feedback = None
    if action.get('action') == 'search_map_evidence':
        search_feedback = {'status':'road_candidates' if road_hits else 'no_road_candidates_returned',
            'road_candidate_ids':road_hits,'returned_count':len(hits),
            'name_reading':'unconfirmed', 'road_existence':'not_excluded',
            'next_test':'Use returned non-road bounds only as locality clues. Recheck ambiguous image text or a distinct independent anchor; do not fetch parcel nodes. If searches are exhausted, a spatially justified seen road connection may be tested; otherwise stop with this evidence gap. No fuzzy-name road lookup or area/intersection search tool exists.'}
    return {'query_purpose': action.get('evidence'), 'search_feedback':search_feedback,
            'supported': {'returned_road_ids': roads, 'object_facts_only': True},
            'excluded': ['non-road objects cannot establish a road intersection'] if ways and not roads else [],
            'unknown': ['screenshot identity', 'ordered traversal including turnbacks', 'unreturned roads'],
            'next_step': 'Reserve scarce searches for relevant road seeds after useful independent locality clues; do not require three landmarks. Never fetch a known park full to exclude it. First test the locality with independent visible anchors and distinct search bounds; a background road is not a located route. After a candidate locality exists, compare route shape and real geometry, then fetch only discriminating connecting way/full if needed. Map directions are not screenshot positions; no road label or registration prerequisite for multi-evidence candidates. A miss/failure excludes no road.'}
