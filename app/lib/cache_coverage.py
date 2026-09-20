"""User-directed source/family cache coverage; no per-model official thresholds."""
from __future__ import annotations
import copy
from collections import defaultdict
from .cache_acceptance import is_pro_model

POLICY = {'schema_version':1,'updated_at':'2026-09-09','group_by':['source_id','family_id'],
          'mechanism_subgroups':True,'exclude_pro_models':True,'exclude_reasoning_modes':['pro'],
          'official_numeric_reference_required':False,'acceptance':'positive_and_negative_hit_expectations',
          'model_family_interpretation':'existing MPDB family; retain necessary mechanism subgroups',
          'user_instruction':'Pro系列模型不需要管缓存；按每个云厂商来源和模型系列测试正负样例、是否应命中及命中率算法。'}


def current_cache_decision(row, legacy, policy):
    if policy != POLICY:raise ValueError('Unknown cache execution policy')
    if is_pro_model(row['model_slug']):
        return {'status':'excluded_by_user','reason':'Pro cache tests excluded by the current user instruction','evidence_refs':[],
                'live_proof':'not_evaluated_by_offline_plan','scope_policy':copy.deepcopy(policy),'new_requests_required':False}
    if legacy['status']=='not_applicable':
        return {**legacy,'official_numeric_reference_required':False,'coverage_unit':['source_id','family_id']}
    ready=bool(row.get('interface_id'))
    return {'status':'applicable' if ready else 'pending_evidence','reason':'design source/family cache controls; no official numeric benchmark required' if ready else 'source Interface/request shape still required',
            'evidence_refs':legacy.get('evidence_refs') or ['mpdb_default_contract'],'live_proof':'not_evaluated_by_offline_plan',
            'coverage_unit':['source_id','family_id'],'template_id':'cache_cold_repeat_negative','request_cap':3,
            'policy':{'minimum_prefix_tokens':None,'official_numeric_reference_required':False,'acceptance':'hit_expectations'},
            'scenarios':[{'case':'cold_unique_prefix','expected_hit':False},
                         {'case':'repeat_identical_complete_request','expected_hit':True},
                         {'case':'changed_initial_prefix','expected_hit':False}],
            'validator':'lib.cache_acceptance.validate_scenario',
            'blockers':[] if ready else ['source_interface_request_shape_unresolved'],
            'limits':'Identity, request fidelity and raw usage remain required; missing cache telemetry is unknown, not zero.'}


def mechanism(row):
    if row.get('modality')!='text':return 'media_input_cache'
    form=row.get('api_form')
    if form=='anthropic_messages':return 'explicit_message_prefix'
    if form=='gemini_generate_content':return 'gemini_native_prefix'
    if form=='gemini_interactions':return 'gemini_interactions_prefix'
    if row['source_id']=='openai' and (row['model_slug'].startswith('gpt-5.6') or row['model_slug'].startswith('gpt-6')):
        return 'message_breakpoints'
    return 'automatic_prefix'


def validated_trios(plan, index, reassessment):
    """Bind retained controls to the current source, Interface, contract and facts."""
    if reassessment is None:return defaultdict(list)
    if reassessment.get('policy')!=POLICY or reassessment.get('catalog_digest')!=plan['catalog_digest']:
        raise ValueError('Cache reassessment policy/catalog mismatch')
    facts={r['path']:r['sha256'] for r in index.get('fact_artifacts',[])}
    sources={r['path']:r['sha256'] for r in reassessment['source_facts']}
    if any(facts.get(p)!=digest for p,digest in sources.items()):raise ValueError('Cache reassessment fact hash mismatch')
    artifacts={r['path']:r['sha256'] for r in reassessment['raw_artifacts_checked']}
    rows={r['target_id']:r for r in plan['rows']};result=defaultdict(list)
    for trio in reassessment['trios']:
        row=rows.get(trio['target_id']);binding=trio['binding'];fact=trio['fact']
        keys=('source_id','profile_id','interface_id','api_form','default_contract_id')
        if row is None or any(binding.get(k)!=row['proof_binding'].get(k) for k in keys) or binding.get('request_model_id') not in row.get('interface_request_model_ids',[]):
            raise ValueError('Cache reassessment target binding mismatch')
        if is_pro_model(binding['request_model_id']):raise ValueError('Pro cache reassessment is outside current scope')
        if sources.get(fact['path'])!=fact['sha256'] or not fact['json_pointer'].startswith('/observations/'):
            raise ValueError('Cache reassessment fact link mismatch')
        if not trio['raw_evidence_paths'] or any(p not in artifacts for p in trio['raw_evidence_paths']):
            raise ValueError('Cache reassessment raw evidence link missing')
        result[trio['target_id']].append(trio)
    return result


def build_coverage(plan, index, reassessment=None):
    trios=validated_trios(plan,index,reassessment)
    cache_records=defaultdict(list)
    for record in index.get('records',[]):
        if record['requirement']=='cache':cache_records[record['target_id']].append(record)
    groups=defaultdict(list);excluded=[]
    for row in plan['rows']:
        if row['decisions']['cache']['status']=='excluded_by_user':excluded.append(row['target_id']);continue
        groups[row['source_id'],row['family_id']].append(row)
    result=[]
    for (source,family),members in sorted(groups.items()):
        subgroups=defaultdict(list)
        for row in members:
            if row['decisions']['cache']['status']!='not_applicable':subgroups[mechanism(row)].append(row)
        mechanisms=[]
        for name,rows in sorted(subgroups.items()):
            def ranking(r):
                observed=cache_records[r['target_id']]
                return (not any(t['acceptance']['status']=='pass' for t in trios[r['target_id']]),
                        not bool(trios[r['target_id']]),not any('observed_pass' in o['states'] for o in observed),
                        not bool(observed),not r.get('interface_executable'),
                        r.get('api_form')!='openai_responses',r['target_id'])
            representative=sorted(rows,key=ranking)[0];records=cache_records[representative['target_id']]
            verified=[t for t in trios[representative['target_id']] if t['acceptance']['status']=='pass']
            states={state for record in records for state in record['states']}
            statuses=[t['acceptance']['status'] for t in trios[representative['target_id']]]
            action=('representative_controls_verified' if verified else
                    'resolve_recorded_account_access' if 'account_blocked' in states else
                    'restore_source_availability' if 'observed_supplier_error' in states else
                    'resolve_missing_cache_telemetry' if 'insufficient_evidence' in statuses else
                    'review_existing_positive_negative_controls' if records else 'prepare_source_specific_positive_negative_controls')
            mechanisms.append({'mechanism':name,'representative_target':representative['target_id'],
                'member_targets':sorted(r['target_id'] for r in rows),'existing_record_ids':[r['record_id'] for r in records],
                'existing_evidence_states':sorted(states),'retained_scenario_statuses':statuses,
                'next_action':action,
                'representative_scenario_verified':bool(verified),'new_requests_required':not bool(verified),
                'representative_evidence':[{'fact':t['fact'],'raw_evidence_paths':t['raw_evidence_paths'],'acceptance':t['acceptance']} for t in verified],
                'execution_gates':representative.get('execution_gates',[]),'family_coverage_verified':False,
                'other_models_or_forms_individually_verified':False})
        result.append({'source_id':source,'family_id':family,'mechanisms':mechanisms,
            'documented_not_applicable_targets':sorted(r['target_id'] for r in members if r['decisions']['cache']['status']=='not_applicable'),
            'official_numeric_reference_required':False})
    return {'schema_version':1,'policy':copy.deepcopy(POLICY),'catalog_digest':plan['catalog_digest'],
        'source_family_groups':result,'excluded_pro_targets':sorted(excluded),
        'summary':{'source_family_groups':len(result),'mechanism_groups':sum(len(g['mechanisms']) for g in result),'excluded_pro_targets':len(excluded),
            'verified_representative_groups':sum(m['representative_scenario_verified'] for g in result for m in g['mechanisms']),
            'pending_representative_groups':sum(m['new_requests_required'] for g in result for m in g['mechanisms'])},
        'new_api_requests':0,'note':'Passed trios satisfy representative coverage only; every member remains listed without individual certification.'}
