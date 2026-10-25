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


def checkpoint(state, reason, action=None):
    """Local progress is not a route export and cannot promote unknown to deliverable."""
    plan = ledger(state)
    focus = state.get('research_focus', {})
    region = focus.get('region') or state.get('review_region', {}).get('region_original_xyxy')
    next_action = ({'action':'observe_region', 'region':region, 'hypothesis':'uncertain',
                    'reason':'Compare existing object connections and competing label readings locally; no new map request.'}
                   if region and plan['counts']['observe'] < LIMITS['observe'] else {'action':'research_checkpoint', 'reason':'Summarize existing evidence without new network requests.'})
    progress = {'blocked_reason': reason, 'blocked_action': action,
        'network_used': dict(state.get('research_budget_used', {})),
        'network_requests_remaining': max(0, 8-state.get('research_budget_used', {}).get('requests', 0)),
        'version': state.get('research_version'), 'object_count': state.get('research_object_count', 0),
        'hypotheses': focus.get('hypotheses', []), 'ordered_hypothesis':state.get('research_hypothesis'),
        'review':state.get('research_review'), 'recovery':state.get('research_recovery'),
        'last_outcome':state.get('research_last_outcome'), 'failures':state.get('research_failures', []),
        'unresolved':['screenshot correspondence', 'ordered traversal', 'unknown legs'],
        'next_local_action':next_action, 'can_resume':plan['model_calls'] < MAX_MODEL_CALLS and plan['counts']['closure'] < LIMITS['closure'],
        'resume_requires_fresh_upload_consent':True, 'verified':False, 'original_gps':False}
    state['research_progress'] = progress
    state.update(status='waiting_for_user', verified=False, original_gps=False,
        question=f"调研已保存（动作/模型均有硬上限），停止原因：{reason}；地图请求 {8-progress['network_requests_remaining']}/8（不等于地图预算耗尽）。下一本地动作：{next_action}。未决假说和未知路段保留，未导出完整路线。")
    if state.get('research_failures'):
        state['question'] += ' 局部失败记录：'+str([{k:f.get(k) for k in ('request','reason_code')} for f in state['research_failures']])
    return progress
