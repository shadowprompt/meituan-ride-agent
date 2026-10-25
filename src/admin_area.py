"""保守行政区层级查询；不联网、不补父级、不选择全国同名区县。"""
import json
import re


def levels_for(original, explicit, error):
    if explicit is not None:
        if (not isinstance(explicit, list) or not 2 <= len(explicit) <= 3
                or any(not isinstance(n, str) for n in explicit)
                or ''.join(explicit) != original or len(set(explicit)) != len(explicit)):
            raise error('Invalid explicit hierarchy', code='invalid_input')
        levels = explicit
    else:
        if not re.fullmatch(r'[\u3400-\u9fff]{1,40}', original):
            raise error('Unsupported administrative name', code='invalid_input')
        levels = re.findall(r'[^省市区县]+?(?:省|市|区|县)', original)
        if ''.join(levels) != original or not 1 <= len(levels) <= 3:
            raise error('Cannot conservatively parse hierarchy', code='area_parent_required')
    if any(not re.fullmatch(r'[\u3400-\u9fff]{1,40}', n) for n in levels):
        raise error('Invalid hierarchy name', code='invalid_input')
    return levels


def resolve(levels, download, deadline, error, identifier, reject_constant):
    """一次QL：父唯一才继续；subarea归属与父范围内匹配必须同时成立。"""
    resolution = {'original_name': ''.join(levels), 'query_levels': levels, 'candidates': [], 'status': 'query_pending'}
    quote = lambda n: json.dumps(n, ensure_ascii=False)
    filters = lambda n: f'["name"={quote(n)}]["boundary"="administrative"]["type"="boundary"]'
    parts = ['[out:json][timeout:10][maxsize:2097152];',
             f'rel{filters(levels[0])}->.p0;.p0 out tags 3;']
    for i, name in enumerate(levels[1:], 1):
        prev = i-1
        parts.append(f'if(p{prev}.count(relations) == 1){{'
                     f'.p{prev} map_to_area ->.a{prev};'
                     f'rel(r.p{prev}:"subarea"){filters(name)}->.members{i};'
                     f'rel.members{i}(area.a{prev})->.p{i};'
                     f'.p{i} out tags 3;'
                     # 仅输出必要父子ID，绝不传输市级relation全部way成员。
                     f'if(p{i}.count(relations) == 1){{'
                     f'make evidence parent_id=p{prev}.min(id()),child_id=p{i}.min(id());out;}}')
    parts.append(f'.p{len(levels)-1} map_to_area;out tags 3;')
    parts.extend('}' for _ in levels[1:])
    try:
        raw = download(''.join(parts), deadline=deadline, max_bytes=2000000)
        data = json.loads(raw.decode('utf-8'), parse_constant=reject_constant)
        if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
            raise ValueError()
        if data.get('remark'):
            raise error('Incomplete hierarchy response', code='server_timeout' if 'timed out' in str(data['remark']).lower() else 'response_incomplete')
        elements = data['elements']
        if len(elements) > 12:
            raise error('Hierarchy object budget exceeded', code='response_limit')
        witnesses = [e for e in elements if isinstance(e, dict) and e.get('type') == 'evidence']
        elements = [e for e in elements if e not in witnesses]
        if (len(witnesses) > len(levels)-1 or any(
                not isinstance(e.get('tags'), dict) or set(e['tags']) != {'parent_id', 'child_id'}
                or any(not isinstance(v, str) or not v.isascii() or not v.isdigit() for v in e['tags'].values())
                for e in witnesses)):
            raise ValueError()
        resolution['status'] = 'response_received'
        seen = set()
        for e in elements:
            if (not isinstance(e, dict) or e.get('type') not in ('relation', 'area')
                    or not identifier(e.get('id')) or not isinstance(e.get('tags'), dict)
                    or e['tags'].get('name') not in levels
                    or e['tags'].get('boundary') != 'administrative'
                    or (e['type'], e['id']) in seen):
                raise ValueError()
            seen.add((e['type'], e['id']))
        parent = None
        for name in levels:
            matches = [e for e in elements if e['type'] == 'relation' and e['tags']['name'] == name]
            resolution['candidates'].append({'name': name, 'relation_ids': [e['id'] for e in matches]})
            if not matches:
                raise error('No constrained parent/child', code='area_not_found' if parent is None else 'area_membership_unverified')
            if len(matches) != 1:
                raise error('Ambiguous parent/child', code='area_ambiguous')
            child = matches[0]
            level = child['tags'].get('admin_level', '')
            if (child['tags'].get('type') != 'boundary' or not isinstance(level, str)
                    or not level.isascii() or not level.isdigit() or not 2 <= int(level) <= 12):
                raise ValueError()
            if parent is not None:
                if (int(level) <= int(parent['tags']['admin_level'])
                        or sum(e['tags'] == {'parent_id': str(parent['id']), 'child_id': str(child['id'])}
                               for e in witnesses) != 1):
                    raise error('Missing membership evidence', code='area_membership_unverified')
            parent = child
        areas = [e for e in elements if e['type'] == 'area']
        if (len(areas) != 1 or areas[0]['id'] != parent['id'] + 3600000000
                or areas[0]['tags']['name'] != levels[-1]):
            raise error('No unique derived child area', code='area_membership_unverified')
        resolution['status'] = 'resolved'
        resolution['membership_evidence'] = 'parent_subarea_membership_and_overpass_parent_area_filter'
        return areas[0]['id'], raw, resolution
    except error as exc:
        if resolution['status'] == 'query_pending':
            resolution['status'] = 'unavailable'
        exc.area_resolution = resolution
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as exc:
        raise error('Invalid hierarchy response', area_resolution=resolution) from exc
