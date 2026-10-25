"""Bounded research-only action ledger; persisted across resume, never a retry grant."""
LIMITS = {'observe': 6, 'focus': 4, 'network': 8, 'local': 9, 'correction': 2, 'closure': 2}
MAX_MODEL_CALLS = 28
CLOSURE_RESERVE = 6


def ledger(state):
    if 'research_plan' not in state:
        # Migrate historical task messages; do not grant a fresh budget on resume.
        import json
        counts = {k: 0 for k in LIMITS}
        calls = 0
        for message in state.get('messages', []):
            if message.get('role') != 'assistant': continue
            calls += 1
            try:
                action = json.loads(message['content'])
                counts[category(action.get('action'))] += 1
            except (ValueError, KeyError, TypeError):
                counts['correction'] += 1
        if 'research_budget_used' in state:
            counts['network'] = state['research_budget_used'].get('requests', 0)
        state['research_plan'] = {'model_calls': calls, 'counts': counts,
            'limits':dict(LIMITS), 'model_limit':MAX_MODEL_CALLS, 'phase':'research'}
    return state['research_plan']


def category(name):
    if name == 'observe_region': return 'observe'
    if name == 'research_focus': return 'focus'
    if name in ('search_map_evidence', 'inspect_map_object'): return 'network'
    if name in ('ask', 'research_checkpoint', 'deliver'): return 'closure'
    return 'local'


def charge(state, kind):
    plan = ledger(state)
    if plan['counts'][kind] >= LIMITS[kind]:
        return False
    plan['counts'][kind] += 1
    return True


def observation_key(state, region):
    # 同证据版本同区域不因重写reason变成新观察；新对象版本可重新对比。
    return [state.get('research_version'), region]


def convergence(state):
    plan = ledger(state)
    if 'convergence' not in plan:
        seen = []
        checkpoints = 0
        for message in state.get('messages', []):
            if message.get('role') == 'assistant':
                try:
                    import json
                    if json.loads(message.get('content', '')).get('action') == 'research_checkpoint':
                        checkpoints += 1
                except (ValueError, TypeError, AttributeError):
                    pass
        # 兼容旧任务：已保存局部不能又被推荐成“新下一步”。
        region = state.get('review_region', {})
        if region.get('region_original_xyxy'):
            seen.append([region.get('research_version', state.get('research_version')),
                         region['region_original_xyxy']])
        import json
        for message in state.get('messages', []):
            content = message.get('content', '')
            if message.get('role') != 'user' or not isinstance(content, str) or not content.startswith('LOCAL_TOOL_RESULT '):
                continue
            try:
                result = json.loads(content[len('LOCAL_TOOL_RESULT '):])
            except (ValueError, TypeError):
                continue
            if result.get('tool') == 'observe_region' and result.get('region_original_xyxy'):
                # 无历史research_version时按当前版本保守去重，不把旧任务当新预算。
                key = [result.get('research_version', state.get('research_version')),
                       result['region_original_xyxy']]
                if key not in seen:
                    seen.append(key)
        plan['convergence'] = {'checkpoints': checkpoints, 'no_progress': 0, 'no_progress_total': 0,
                               'observations': seen, 'decisions': []}
        for field in ('research_hypothesis', 'research_review', 'research_focus'):
            existing = state.get(field)
            if isinstance(existing, dict) and existing.get('action'):
                key = decision_key(state, existing)
                if key is not None:
                    plan['convergence']['decisions'].append(key)
    return plan['convergence']


def decision_key(state, action):
    name = action.get('action')
    # 文案变化不算路线假说/复核进展。相同证据、相同结论不反复计数。
    if name == 'research_location':
        return [state.get('research_version'), name, action.get('anchors'), action.get('status'),
                action.get('map_comparison'), action.get('image_comparison')]
    if name == 'research_hypothesis':
        return [state.get('research_version'), name, action.get('ordered_roads')]
    if name == 'review_research':
        return [state.get('research_version'), name, action.get('result'),
                (state.get('research_hypothesis') or {}).get('ordered_roads')]
    if name == 'research_focus':
        return [state.get('research_version'), name, action.get('region'),
                [[h.get('claim'), h.get('status')] for h in action.get('hypotheses', []) if isinstance(h, dict)]]
    if name == 'inspect_roads':
        return [state.get('research_version'), name, action.get('names')]
    if name == 'build_corridor':
        return [state.get('research_version'), name, action.get('hypothesis')]
    return None


def duplicate(state, action):
    c = convergence(state)
    if action.get('action') == 'observe_region':
        return observation_key(state, action.get('region')) in c['observations']
    key = decision_key(state, action)
    return key is not None and key in c['decisions']


def record_progress(state, action):
    c = convergence(state)
    if action.get('action') == 'observe_region':
        c['observations'].append(observation_key(state, action['region']))
    key = decision_key(state, action)
    if key is not None:
        c['decisions'].append(key)
    c['no_progress'] = 0


def local_next(state, proposed=None):
    """Only executable local work; never invent a road, a region or network permission."""
    plan = ledger(state)
    if plan['model_calls'] >= MAX_MODEL_CALLS:
        return None
    if proposed:
        allowed = {'observe_region', 'research_focus', 'research_hypothesis',
                   'review_research', 'inspect_roads', 'build_corridor', 'research_location'}
        if (not isinstance(proposed, dict) or proposed.get('action') not in allowed
                or plan['counts'][category(proposed.get('action'))] >= LIMITS[category(proposed.get('action'))]
                or duplicate(state, proposed)):
            return None
        name = proposed['action']
        if name == 'research_location':
            import research_decision
            try:
                research_decision.location_note(proposed)
            except (ValueError, TypeError, KeyError):
                return None
        elif name == 'observe_region':
            import route_review
            try:
                route_review.region_box(proposed.get('region'), state['image_size'])
                if set(proposed) != {'action','region','hypothesis','reason'}: return None
                if proposed['hypothesis'] not in ('turnback','two_arms','occlusion','repeated_order','uncertain'): return None
                route_review.text(proposed['reason'])
            except (ValueError, KeyError, TypeError):
                return None
            if state.get('review_preview_count', 0) >= 6: return None
        elif name == 'research_focus':
            if (set(proposed) != {'action','region','hypotheses','next_query','falsifier'}
                    or proposed['region'] != state.get('review_region', {}).get('region_original_xyxy')
                    or not proposed['hypotheses'] or not proposed['next_query'] or not proposed['falsifier']): return None
        elif name in ('research_hypothesis', 'review_research'):
            if proposed.get('version') != state.get('research_version'): return None
            if state.get('research_observed_version') != state.get('research_version'): return None
            if name == 'review_research':
                if (set(proposed) != {'action','version','result','evidence','limitations'}
                        or proposed.get('result') not in ('consistent','ambiguous','needs_revision')
                        or not state.get('research_hypothesis')
                        or state.get('research_presented_version') != state.get('research_version')): return None
            elif (set(proposed) != {'action','version','ordered_roads','evidence','limitations'}
                  or not isinstance(proposed.get('ordered_roads'), list)
                  or not proposed['ordered_roads']): return None
        elif name == 'inspect_roads':
            if (set(proposed) != {'action','names'} or not isinstance(proposed.get('names'), list)
                    or not proposed['names'] or not state.get('research_object_count')): return None
        elif name == 'build_corridor':
            roads = (state.get('research_hypothesis') or {}).get('ordered_roads')
            if (set(proposed) != {'action','hypothesis'} or not isinstance(proposed.get('hypothesis'), dict)
                    or not roads or None in roads or not state.get('corridor_inspection')): return None
        return proposed
    # 旧reason-only协议仍可保存。若当前观察尚无结构化比较，给出可执行的
    # focus任务（不是编造候选）；模型须根据已有对象/像素填写真实候选和证伪条件。
    region = state.get('review_region', {}).get('region_original_xyxy')
    if (region and not state.get('research_focus')
            and state.get('research_hypothesis') and not state.get('research_review')
            and not state.get('research_recovery') and state.get('research_progress', {}).get('status') != 'continuing_local'
            and plan['counts']['focus'] < LIMITS['focus']):
        return {'action':'research_focus', 'region':region,
                'task':'Compare the saved ordered hypothesis evidence with cached road directions/shared nodes. '
                       'Name concrete competing pixel/returned-object claims, evidence and a falsifier; '
                       'keep unknowns. Fill hypotheses, next_query (local comparison purpose), falsifier. '
                       'Alternatively propose a DIFFERENT relevant original-image region; do not reobserve this region.'}
    return None


def checkpoint(state, reason, action=None, allow_continue=False, planning=None):
    """保存与停止分离；仅当前调用授权内继续，不触发resume或网络动作。"""
    plan = ledger(state)
    c = convergence(state)
    if reason == 'model_checkpoint':
        c['checkpoints'] += 1
        c['no_progress_total'] = c.get('no_progress_total', 0) + 1
        c['no_progress'] += 1  # checkpoint itself is never evidence/progress
    proposed = action.get('next_local_action') if action else None
    next_action = local_next(state, proposed) if reason == 'model_checkpoint' else None
    # 已有focus并不代表所有候选已核查。reason-only旧协议给一次真实证据规划修复，
    # 不生成focus/区域/配准，也不占用额外closure或扩大任何预算。
    repair = (reason == 'model_checkpoint' and not proposed and next_action is None
              and bool(state.get('research_focus')) and planning is not None
              and plan.get('planner_repairs', 0) < 1 and plan['model_calls'] < MAX_MODEL_CALLS
              and (planning.get('uninspected_candidates') or planning.get('known_geometry')))
    # 有当前授权及真实可执行对象时，保存进度不能把阶段永久降成local-only。
    network_repair = (reason == 'model_checkpoint' and not proposed and next_action is None
                      and not state.get('research_review')
                      and planning and planning.get('network_allowed_this_invocation')
                      and planning.get('network_suggestions') and plan.get('planner_repairs',0) < 1)
    repair = repair or network_repair
    if planning and planning.get('road_seed_plan',{}).get('stop_without_road_seed'):
        next_action, repair = None, False
    continuing = (allow_continue and (next_action is not None or repair) and c['no_progress'] < 3
                  and plan['counts']['closure'] < LIMITS['closure'])
    if continuing and repair:
        plan['planner_repairs'] = plan.get('planner_repairs', 0) + 1
    blocked = None if continuing else reason
    if reason == 'model_checkpoint' and not continuing:
        blocked = ('no_progress_limit' if c['no_progress'] >= 3 else
                   'checkpoint_limit' if plan['counts']['closure'] >= LIMITS['closure'] else
                   'no_executable_local_action')
    focus = state.get('research_focus', {})
    progress = {'blocked_reason': blocked, 'checkpoint_reason': reason, 'blocked_action': action,
        'checkpoints':c['checkpoints'], 'no_progress':c['no_progress'],
        'no_progress_total':c.get('no_progress_total', 0),
        'network_used': dict(state.get('research_budget_used', {})),
        'network_requests_remaining': max(0, 8-state.get('research_budget_used', {}).get('requests', 0)),
        'version': state.get('research_version'), 'object_count': state.get('research_object_count', 0),
        'hypotheses': focus.get('hypotheses', []), 'ordered_hypothesis':state.get('research_hypothesis'),
        'review':state.get('research_review'), 'recovery':state.get('research_recovery'),
        'last_outcome':state.get('research_last_outcome'), 'failures':state.get('research_failures', []),
        'unresolved':['screenshot correspondence', 'ordered traversal', 'unknown legs'],
        'next_local_action':next_action if continuing else None,
        'planner_repair':bool(continuing and repair),
        'continuation_scope':('bounded_planning_with_current_consent' if planning and planning.get('network_allowed_this_invocation') else 'local_only') if continuing else None,
        'planning_evidence':planning if continuing else None,
        'next_planner_task':('Choose one concrete nonduplicate action using the structured evidence. '
            'An existing focus is not a completed investigation. Inspect an actually seen candidate way/full or known road node/ways only if network_allowed_this_invocation; otherwise compare cached geometry or choose a DIFFERENT relevant screenshot region. '
            'Do not invent focus, coordinates, screenshot positions or fill unknown legs. No new evidence means stop at the next checkpoint.') if continuing and repair else None,
        'can_resume':plan['model_calls'] < MAX_MODEL_CALLS and plan['counts']['closure'] < LIMITS['closure'],
        'no_resume':plan['model_calls'] >= MAX_MODEL_CALLS or plan['counts']['closure'] >= LIMITS['closure'],
        'resume_requires_fresh_upload_consent':True, 'verified':False, 'original_gps':False,
        'evidence_summary':{'observed_regions':c['observations'], 'focus':state.get('research_focus'),
                            'ordered_hypothesis':state.get('research_hypothesis'),
                            'last_outcome':state.get('research_last_outcome')},
        'status':'continuing_local' if continuing else 'research_unconverged'}
    state['research_progress'] = progress
    if continuing:
        state.update(status='analyzing', verified=False, original_gps=False)
        state.pop('question', None)
    else:
        # 没有真正的用户决策问题，不假装等待用户填写道路；paused是显式未收敛。
        state.update(status='waiting_for_user' if reason == 'specific_user_question' else 'paused',
            verified=False, original_gps=False,
            question=f"调研未收敛，进度已保存；停止原因：{blocked}（保留动作/模型硬上限）。地图请求 {8-progress['network_requests_remaining']}/8（{'请求额度已用尽' if progress['network_requests_remaining'] == 0 else '请求仍有余量，非请求耗尽'}；搜索 {state.get('research_budget_used', {}).get('searches', 0)}/3）。未知路段保留，未导出完整路线；不会自动续授权或重放任务。")
    if not continuing and state.get('research_failures'):
        import json
        state['question'] += ' 局部失败记录：' + json.dumps([
            {'request':f.get('request'), 'reason_code':f.get('reason_code')}
            for f in state['research_failures']], ensure_ascii=False)
    return progress
