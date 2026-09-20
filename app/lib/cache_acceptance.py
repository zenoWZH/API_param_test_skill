"""Scenario-based cache acceptance and weighted metrics, without vendor benchmarks."""
from __future__ import annotations
import re
import json
from typing import Any


def is_pro_model(model: str) -> bool:
    return bool(re.search(r'(^|[-_/.\s])pro($|[-_/.\s])', str(model), re.I))


def cache_telemetry(usage: dict, transport: str = '') -> dict:
    """Normalize observed read tokens; missing reads never become zero."""
    usage = usage if isinstance(usage, dict) else {}
    integer = lambda x: type(x) is int and x >= 0
    native_claude = transport in ('claude_messages', 'anthropic_messages') or any(k in usage for k in ('cache_read_input_tokens','cache_creation_input_tokens'))
    hit = usage.get('prompt_cache_hit_tokens')
    if hit is None:
        for value in (usage.get('cached_tokens'), usage.get('cache_read_input_tokens'), usage.get('cachedContentTokenCount'), usage.get('cached_content_token_count'),
                      (usage.get('prompt_tokens_details') or {}).get('cached_tokens') if isinstance(usage.get('prompt_tokens_details'),dict) else None,
                      (usage.get('input_tokens_details') or {}).get('cached_tokens') if isinstance(usage.get('input_tokens_details'),dict) else None):
            if value is not None:hit=value;break
    total = next((usage[k] for k in ('prompt_tokens','promptTokenCount','prompt_token_count','input_tokens') if usage.get(k) is not None),None)
    if native_claude and 'prompt_tokens' not in usage:
        uncached=usage.get('input_tokens');write=usage.get('cache_creation_input_tokens',0)
        total=uncached+write+hit if all(integer(x) for x in (uncached,write,hit)) else None
    errors=[]
    if total is not None and not integer(total):errors.append('invalid_input_tokens')
    if hit is not None and not integer(hit):errors.append('invalid_cache_read_tokens')
    if integer(total) and integer(hit) and hit>total:errors.append('cache_read_exceeds_input')
    miss=usage.get('prompt_cache_miss_tokens')
    if miss is not None and (not integer(miss) or (integer(total) and integer(hit) and hit+miss!=total)):errors.append('inconsistent_hit_miss_input')
    status='invalid' if errors else 'missing' if total is None or hit is None else 'measured'
    return {'status':status,'input_tokens':total,'cached_input_tokens':hit,'errors':errors}


def rates(records: list[Any]) -> dict:
    """Same measured population in both sums; no mean-of-ratios or clipping."""
    successful=[r for r in records if r.success]
    values=[cache_telemetry(r.usage or {},str((r.extra or {}).get('transport') or '')) for r in successful]
    measured=[v for v in values if v['status']=='measured']
    inputs=sum(v['input_tokens'] for v in measured);cached=sum(v['cached_input_tokens'] for v in measured)
    rate=cached/inputs if inputs else None
    return {'request_count':len(records),'success_count':len(successful),'measurement_count':len(measured),
        'invalid_measurement_count':sum(v['status']=='invalid' for v in values),'missing_measurement_count':sum(v['status']=='missing' for v in values),
        'input_tokens':inputs,'cached_input_tokens':cached,'cached_input_token_ratio':rate,
        'cache_hit_request_ratio':sum(v['cached_input_tokens']>0 for v in measured)/len(measured) if measured else None,
        'measurement_coverage':len(measured)/len(successful) if successful else None}


def apply_summary(summary: dict, records: list[Any]) -> None:
    customer=[r for r in records if not r.is_warmup and not (r.extra or {}).get('cache_control') and not (r.extra or {}).get('cache_structure_probe')]
    value=rates(customer)
    summary.update(cache_evaluation_policy='scenario_expectations_v1',cache_hit_rate=value['cached_input_token_ratio'],
        cache_hit_rate_semantics='cached_input_tokens/input_tokens',
        cache_hit_rate_formula='sum(cache_read_input_tokens)/sum(total_input_tokens)',
        actual_cache_hit_rate=value['cached_input_token_ratio'],cached_input_token_ratio=value['cached_input_token_ratio'],
        cached_input_tokens=value['cached_input_tokens'],cache_hit_tokens=value['cached_input_tokens'],
        cache_miss_tokens=value['input_tokens']-value['cached_input_tokens'],customer_input_tokens=value['input_tokens'],cache_hit_request_ratio=value['cache_hit_request_ratio'],
        cache_measurement_coverage=value['measurement_coverage'],cache_usage_fields_seen=value['measurement_count'],
        cache_eligible_record_count=value['measurement_count'],cache_invalid_measurement_count=value['invalid_measurement_count'],
        cache_missing_measurement_count=value['missing_measurement_count'],cache_official_numeric_reference_required=False,
        cache_measurement_population='successful customer requests with valid input and cache-read telemetry; controls and structure probes excluded')
    controls={}
    for kind in {(r.extra or {}).get('cache_control') for r in records if (r.extra or {}).get('cache_control')}:
        selected=[r for r in records if (r.extra or {}).get('cache_control')==kind and (r.extra or {}).get('control_role')!='cold']
        controls[kind]={**(summary.get('cache_control_metrics',{}).get(kind) or {}),**rates(selected)}
        controls[kind]['total_control_request_count']=sum((r.extra or {}).get('cache_control')==kind for r in records)
    summary['cache_control_metrics']=controls
    control_rows=[r for r in records if (r.extra or {}).get('cache_control')]
    roles={(r.extra or {}).get('control_role') for r in control_rows}
    complete={'cold','warm','unique'} <= roles
    mismatch=False
    for record in control_rows:
        observed=cache_telemetry(record.usage or {}, str((record.extra or {}).get('transport') or ''))
        if not record.success or observed['status']!='measured':
            complete=False;continue
        expected=(record.extra or {}).get('control_role')=='warm'
        mismatch=mismatch or ((observed['cached_input_tokens']>0)!=expected)
    summary['cache_expectations_pass']=complete and not mismatch
    summary['cache_expectation_status']='unverified' if not complete else 'expectation_failed' if mismatch else 'pass'
    for stage,old in (summary.get('cache_stage_metrics') or {}).items():
        v=rates([r for r in customer if (r.extra or {}).get('cache_stage')==stage]);old.update(input_tokens=v['input_tokens'],cached_input_tokens=v['cached_input_tokens'],
            cached_input_token_ratio=v['cached_input_token_ratio'],actual_cache_hit_rate=v['cached_input_token_ratio'],cache_hit_request_ratio=v['cache_hit_request_ratio'],measurement_coverage=v['measurement_coverage'])
    prefix_values=[]
    for record in customer:
        if not record.success:continue
        observed=cache_telemetry(record.usage or {}, str((record.extra or {}).get('transport') or ''))
        if observed['status']!='measured':continue
        extra=record.extra or {}
        prefix=extra.get('reusable_prefix_tokens',extra.get('cacheable_prefix_tokens'))
        if type(prefix) is not int or prefix<0 or prefix>observed['input_tokens']:continue
        prefix_values.append((prefix,observed['cached_input_tokens']))
    complete_prefix=len(prefix_values)==value['measurement_count'] and bool(prefix_values)
    prefix_total=sum(p for p,_ in prefix_values)
    summary['prefix_cache_hit_rate']=(sum(c for _,c in prefix_values)/prefix_total if complete_prefix and prefix_total else None)
    summary['prefix_cache_hit_rate_semantics']='cache_read_tokens/reusable_prefix_tokens; optional structural diagnostic'
    summary['prefix_cache_hit_tokens']=sum(c for _,c in prefix_values) if complete_prefix else None
    summary['prefix_cache_miss_tokens']=prefix_total-sum(c for _,c in prefix_values) if complete_prefix else None
    if complete_prefix and value['input_tokens']:
        summary['structural_hit_rate_ceiling']=prefix_total/value['input_tokens']
    if value['invalid_measurement_count']:
        summary['structural_hit_rate_ceiling']=None
    ceiling=summary.get('structural_hit_rate_ceiling')
    ratio=value['cached_input_token_ratio']
    summary['cache_efficiency']=ratio/ceiling if ratio is not None and isinstance(ceiling,(int,float)) and ceiling>0 else None
    summary['cache_efficiency_role']='optional structural estimate, not an official acceptance target'
    efficiency=summary['cache_efficiency']
    summary['cache_efficiency_status']='unavailable' if efficiency is None else 'exceeds_structure' if efficiency>1 else 'measured'


def validate_scenario(*, cache_reads: list, input_tokens: list, prefixes: list[str], settings: list[dict],
                      bindings: list[dict], expected_binding: dict, expected_hits=(False,True,False)) -> dict:
    """Validate cold/repeat/changed-prefix controls with no minimum-token input."""
    if any(len(v)!=3 for v in (cache_reads,input_tokens,prefixes,settings,bindings)) or len(expected_hits)!=3:
        return {'status':'control_failed','reason':'three complete controls required'}
    if any(type(value) is not bool for value in expected_hits):return {'status':'control_failed','reason':'expected hits must be booleans'}
    try:
        canonical=lambda value:json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
        same_binding=bool(expected_binding) and all(canonical(b)==canonical(expected_binding) for b in bindings)
        same_settings=all(isinstance(s,dict) for s in settings) and canonical(settings[0])==canonical(settings[1])==canonical(settings[2])
    except (TypeError,ValueError):return {'status':'control_failed','reason':'non-JSON control settings or binding'}
    if not same_binding:return {'status':'control_failed','reason':'source/interface binding differs'}
    if not all(isinstance(p,str) and p for p in prefixes) or prefixes[0]!=prefixes[1] or prefixes[0][:64]==prefixes[2][:64] or not same_settings:
        return {'status':'control_failed','reason':'prefix or settings isolation failed'}
    if not all(type(v) is int and v>=0 for v in input_tokens) or not all(type(v) is int and v>=0 for v in cache_reads):
        return {'status':'insufficient_evidence','reason':'input/cache-read telemetry missing or invalid'}
    if input_tokens[0]!=input_tokens[1] or any(c>n for c,n in zip(cache_reads,input_tokens)):
        return {'status':'control_failed','reason':'inconsistent input or cache-read counters'}
    actual=[c>0 for c in cache_reads];mismatches=[n for n,(a,e) in enumerate(zip(actual,expected_hits)) if a!=e]
    return {'status':'pass' if not mismatches else 'expectation_failed','expected_hits':list(expected_hits),'observed_hits':actual,
        'mismatched_controls':mismatches,'cache_reads':cache_reads,'official_numeric_reference_required':False,
        'token_hit_rate':sum(cache_reads)/sum(input_tokens) if sum(input_tokens) else None,
        'request_hit_rate':sum(actual)/3,'metric_population':'three controls only; not customer hit rate'}
