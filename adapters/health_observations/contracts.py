"""Private D1 wire contracts. Validation is not archive replay or admission."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import uuid
from functools import wraps
from datetime import date, timedelta
from jsonschema import Draft202012Validator, validators


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('ascii')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict_load(blob):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('INVALID_JSON')
            result[key] = value
        return result
    def reject(_):
        raise ValueError('INVALID_JSON')
    def floating(token):
        value = float(token)
        if not math.isfinite(value):
            reject(token)
        return value
    return json.loads(blob, object_pairs_hook=pairs, parse_constant=reject, parse_float=floating)


def obj(**fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}


def const(value):
    return {'const': value, 'type': {str: 'string', bool: 'boolean', type(None): 'null'}[type(value)]}


S = {'type': 'string'}
H = {'type': 'string', 'pattern': '^[0-9a-f]{64}$', 'minLength': 64, 'maxLength': 64}
N = {'type': 'integer', 'minimum': 0}
NULL = {'type': 'null'}
FALSE = const(False)
GROUPS = {'type': 'array', 'items': H, 'minItems': 1, 'uniqueItems': True}
# Stable names describe the complete supported contract in HEALTH_ADAPTER_D1.md.
RULES = {
    'policy_version': 'mesh-selected-steps-v1',
    'timestamp_grammar': 'ascii_offset_required_microseconds_v1',
    'civil_day': 'pinned_tzif_contiguous_utc_preimage_v1',
    'point_interval': 'half_open_intervals_points_at_start_v1',
    'day_allocation': 'whole_interval_single_day_no_proration_v1',
    'unresolved_source': 'archive_family_block_v1',
    'invalid_record': 'unattributable_family_bad_quantity_assigned_days_v1',
    'repeat_key': 'family_descriptor_utc_endpoints_tagged_raw_unit_value_v1',
    'repeat_policy': 'retain_all_block_daily_extras_v1',
    'collision_policy': 'all_selected_intervals_points_block_no_precedence_v1',
    'estimator': 'sum_selected_allocatable_valid_quantities_v1',
    'numeric_rule': 'exact_rational_sum_binary64_nearest_even_positive_zero_range_guard_v1',
}
DEFINITION_SCHEMA = obj(
    schema_version=const('health-observation-definition-v1'),
    metric=const('selected_source_recorded_steps'), unit=const('count'),
    observation_schema=const('mesh-steps-report-v1'),
    source_scope={'enum': ['app_stream', 'device'], 'type': 'string'},
    selected_group_ids=GROUPS,
    catalogue_descriptor_version=const('mesh-health-catalogue-v1'),
    descriptor_hash_version=const('sha256_canonical_family_tagged_descriptor_v1'),
    timezone=obj(key=S, tzif_sha256=H),
    rules=obj(**{k: const(v) for k, v in RULES.items()}),
)
SLOT = {'oneOf': [{'const': ['missing']}, {'type': 'array', 'prefixItems': [const('present'), S], 'minItems': 2, 'maxItems': 2}]}
GROUP = obj(group_id=H, descriptor={'type': 'array', 'items': SLOT, 'minItems': 3, 'maxItems': 3}, record_count={'type': 'integer', 'minimum': 1})
FAMILIES = ('steps', 'hrv_sdnn', 'sleep')
CATALOGUE = obj(catalogue_schema=const('mesh-health-catalogue-v1'), archive_sha256=H, archive_bytes=N, xml_member=S, record_count_all_types=N,
                families=obj(**{f: {'type': 'array', 'items': GROUP} for f in FAMILIES}))
CATALOGUE_ENVELOPE = obj(catalogue=CATALOGUE, catalogue_sha256=H)
SELECTION = obj(selection_handle=S, group_ids=GROUPS, source_scope={'type': 'string', 'enum': ['app_stream', 'device']}, user_choice=S, identity_evidence=S)
REQUEST = obj(selection_schema=const('mesh-health-selection-request-v1'), archive_sha256=H, catalogue_sha256=H,
              selections={'type': 'object', 'properties': {f: SELECTION for f in FAMILIES}, 'required': ['steps'], 'additionalProperties': False})
BINDING = obj(binding_schema=const('mesh-health-selection-binding-v1'), catalogue=CATALOGUE, catalogue_sha256=H, selection_request=REQUEST,
              validation_scope=const('archive_integrity_and_selection_membership'), device_identity_verified=FALSE, user_intent_verified=FALSE, health_source_priority_verified=FALSE)
BINDING_ENVELOPE = obj(binding=BINDING, binding_sha256=H)
ARCHIVE = obj(sha256=H, bytes=N, xml_member=S)
PARTITIONS = ('selected', 'excluded', 'unresolved')
COUNT = obj(**{k: N for k in ('occurrences', 'invalid_occurrences', 'invalid_value', 'invalid_unit')})
ARCHIVE_COUNT = obj(**{k: N for k in ('occurrences', 'time_attributable', 'time_unattributable', 'invalid_occurrences', 'invalid_time', 'invalid_value', 'invalid_unit')})
REASONS = {'type': 'array', 'items': {'type': 'string', 'enum': ['selection_unresolved', 'selected_time_unattributable', 'invalid_selected_record', 'cross_day_quantity_unallocated', 'selected_quantity_overlap', 'selected_repeat_candidate', 'numeric_result_out_of_range', 'no_civil_day']}, 'uniqueItems': True}
DAY = obj(date=S, utc_start={'type': ['string', 'null']}, utc_end={'type': ['string', 'null']}, utc_duration_seconds=N,
          calculation_state={'type': 'string', 'enum': ['computed', 'no_records', 'blocked', 'no_civil_day']}, value={'type': ['number', 'null']}, reasons=REASONS,
          observation_completeness=const('unknown'), valid_metric_day=NULL, inference_eligible=FALSE,
          counts=obj(**{p: COUNT for p in PARTITIONS}), repeat_candidate_extras=N,
          diagnostics=obj(**{k: {'type': ['boolean', 'null']} for k in ('selected_quantity_overlap', 'selected_excluded_overlap')}))
REPORT = obj(schema_version=const('mesh-steps-report-v1'), policy_version=const('mesh-selected-steps-v1'), producer_version=const('mesh-steps-evaluator-v1'), metric=const('selected_source_recorded_steps'), unit=const('count'),
             archive=ARCHIVE, binding_sha256=H, selection_handle=S,
             window=obj(as_of=S, days={'type': 'integer', 'minimum': 1}, start=S, end=S), timezone=obj(key=S, tzif_base64=S, tzif_sha256=H),
             family_reasons=REASONS, archive_counts=obj(**{p: ARCHIVE_COUNT for p in PARTITIONS}), window_counts=obj(**{p: COUNT for p in PARTITIONS}),
             archive_repeat_candidate_extras=N, window_repeat_candidate_extras=N, days={'type': 'array', 'items': DAY})
REPORT_ENVELOPE = obj(report=REPORT, report_sha256=H)
BATCH_SCHEMA = obj(schema_version=const('health-observation-batch-v1'), batch_id=H, definition_id=H, definition=DEFINITION_SCHEMA,
                   archive=ARCHIVE, catalogue=CATALOGUE_ENVELOPE, binding=BINDING_ENVELOPE, report=REPORT_ENVELOPE)
CHECKS = ['binding_and_archive', 'pinned_timezone_boundaries', 'daily_steps_and_diagnostics', 'report_identity']
IDENTITIES = obj(definition_id=H, archive_sha256=H, catalogue_sha256=H, binding_sha256=H, report_sha256=H)
CODE_FILES = ('health_catalogue/catalogue.py', 'health_catalogue/verify_catalogue.py', 'health_steps/evaluate.py', 'health_steps/steps_core.py', 'health_steps/steps_time.py', 'health_steps/verify_steps.py', 'health_steps/oracle_core.py', 'health_steps/oracle_time.py')
RECEIPT_SCHEMA = obj(schema_version=const('health-observation-verification-receipt-v1'), attempt_id=S, batch_id=H, batch_sha256=H, identities=IDENTITIES,
                     outcome=const('PASS'), verification_scope=const('selected_steps_report_exact_recomputation'), checks={'const': CHECKS, 'type': 'array'},
                     started_at=S, finished_at=S, input_mode={'type': 'string', 'enum': ['evaluated', 'supplied_report']},
                     code_sha256=obj(**{k: H for k in CODE_FILES}),
                     adapter_sha256=H, runtime=obj(implementation=S, version=S, executable_sha256=H, expat_version=S, jsonschema_version=S),
                     resource_envelope=obj(subprocess_timeout_seconds={'type': 'integer', 'minimum': 1}, value_token_characters=const('4096'), exponent_magnitude=const('100000')))
ERROR_CODES = ('INPUT_INVALID', 'INPUT_CHANGED', 'EVALUATION_FAILED', 'VERIFICATION_FAILED', 'OPERATING_INPUT_MISMATCH', 'DEFINITION_INVALID', 'DEFINITION_CONFLICT', 'CONTRACT_INVALID', 'CODE_CHANGED', 'INPUT_RESOURCE_LIMIT', 'OUTPUT_INVALID', 'OUTPUT_EXISTS', 'OUTPUT_NOT_PRIVATE', 'CLI_USAGE', 'INTERNAL_ERROR')
FAILURE_SCHEMA = obj(schema_version=const('health-observation-failure-v1'), status=const('FAIL'), error={'type': 'string', 'enum': list(ERROR_CODES)},
                     conflict={'oneOf': [NULL, obj(actual_definition_id=H, expected_definition_id=H, differing_fields={'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'enum': list(DEFINITION_SCHEMA['properties'])}})]})
PREPARED_SCHEMA = obj(schema_version=const('health-observation-prepared-v1'), batch=BATCH_SCHEMA, receipt=RECEIPT_SCHEMA)
# JSON Schema's default integer accepts 1.0. Wire counts intentionally do not.
_TYPES = Draft202012Validator.TYPE_CHECKER.redefine('integer', lambda _, v: type(v) is int).redefine('number', lambda _, v: type(v) in (int, float) and math.isfinite(v)).redefine('object', lambda _, v: type(v) is dict).redefine('array', lambda _, v: type(v) is list)
Validator = validators.extend(Draft202012Validator, type_checker=_TYPES)
SCHEMAS = {'definition': DEFINITION_SCHEMA, 'batch': BATCH_SCHEMA, 'receipt': RECEIPT_SCHEMA, 'failure': FAILURE_SCHEMA, 'prepared': PREPARED_SCHEMA}


def require(test):
    if not test:
        raise ValueError('CONTRACT_INVALID')


def structural(kind, value):
    if not Validator(SCHEMAS[kind]).is_valid(value):
        raise ValueError('CONTRACT_INVALID')
    canonical(value)


def zone_key(key):
    require(bool(key) and not key.startswith('/') and all(p not in ('', '.', '..') for p in key.split('/')) and '\\' not in key)


def safe_validation(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            raise ValueError('CONTRACT_INVALID') from None
    return checked


@safe_validation
def validate_definition(value):
    structural('definition', value)
    require(value['selected_group_ids'] == sorted(set(value['selected_group_ids'])))
    zone_key(value['timezone']['key'])
    return value


def definition_id(value):
    validate_definition(value)
    return digest({'kind': 'health-observation-definition-v1', 'payload': value})


def derive_definition(binding, report):
    selected = binding['binding']['selection_request']['selections']['steps']
    result = {'schema_version': 'health-observation-definition-v1', 'metric': report['metric'], 'unit': report['unit'],
              'observation_schema': report['schema_version'], 'source_scope': selected['source_scope'], 'selected_group_ids': selected['group_ids'][:],
              'catalogue_descriptor_version': binding['binding']['catalogue']['catalogue_schema'], 'descriptor_hash_version': 'sha256_canonical_family_tagged_descriptor_v1',
              'timezone': {k: report['timezone'][k] for k in ('key', 'tzif_sha256')}, 'rules': dict(RULES)}
    require(report['policy_version'] == RULES['policy_version'])
    validate_definition(result)
    return result


def identities(batch):
    return {'definition_id': batch['definition_id'], 'archive_sha256': batch['archive']['sha256'], 'catalogue_sha256': batch['catalogue']['catalogue_sha256'],
            'binding_sha256': batch['binding']['binding_sha256'], 'report_sha256': batch['report']['report_sha256']}


def batch_id(batch):
    return digest({'kind': 'health-observation-batch-v1', 'payload': identities(batch)})


def _uuid(value):
    parsed = uuid.UUID(value)
    require(parsed.version == 4 and str(parsed) == value)


def _counts(count, archive=False):
    total = count['occurrences']
    require(max(count.values()) <= total)
    defects = [count['invalid_value'], count['invalid_unit']]
    if archive:
        require(count['invalid_time'] == count['time_unattributable'])
        require(count['time_attributable'] + count['time_unattributable'] == total)
        defects.append(count['invalid_time'])
    require(max(defects) <= count['invalid_occurrences'] <= sum(defects))


@safe_validation
def validate_batch(value):
    """Structure, hashes and envelope relationships; NEVER attests archive replay."""
    structural('batch', value)
    require(definition_id(value['definition']) == value['definition_id'])
    require(batch_id(value) == value['batch_id'])
    for key in ('catalogue', 'binding', 'report'):
        require(digest(value[key][key]) == value[key][key + '_sha256'])
    cat, binding, report = (value[k][k] for k in ('catalogue', 'binding', 'report'))
    require(canonical(binding['catalogue']) == canonical(cat))
    require(binding['catalogue_sha256'] == value['catalogue']['catalogue_sha256'])
    require(value['archive'] == report['archive'] == {'sha256': cat['archive_sha256'], 'bytes': cat['archive_bytes'], 'xml_member': cat['xml_member']})
    require(cat['xml_member'].split('/')[-1] == 'export.xml')
    request = binding['selection_request']
    require(request['archive_sha256'] == cat['archive_sha256'] and request['catalogue_sha256'] == binding['catalogue_sha256'])
    require(report['binding_sha256'] == value['binding']['binding_sha256'])
    handles, classified = set(), 0
    for family in FAMILIES:
        groups = cat['families'][family]
        descriptors = [canonical(g['descriptor']) for g in groups]
        require(descriptors == sorted(set(descriptors)))
        for group in groups:
            require(group['group_id'] == digest({'family': family, 'descriptor': group['descriptor']}))
            classified += group['record_count']
        if family in request['selections']:
            selection = request['selections'][family]
            _uuid(selection['selection_handle'])
            require(selection['selection_handle'] not in handles)
            handles.add(selection['selection_handle'])
            require(selection['group_ids'] == sorted(set(selection['group_ids'])))
            available = {g['group_id']: g for g in groups}
            for gid in selection['group_ids']:
                require(gid in available)
                slot = available[gid]['descriptor'][0]
                require(len(slot) == 2 and bool(slot[1].strip()))
            require(bool(selection['user_choice'].strip()) and bool(selection['identity_evidence'].strip()))
    require(classified <= cat['record_count_all_types'])
    require(report['selection_handle'] == request['selections']['steps']['selection_handle'])
    require(canonical(derive_definition(value['binding'], report)) == canonical(value['definition']))
    zone_key(report['timezone']['key'])
    tzif = base64.b64decode(report['timezone']['tzif_base64'], validate=True)
    require(base64.b64encode(tzif).decode('ascii') == report['timezone']['tzif_base64'])
    require(hashlib.sha256(tzif).hexdigest() == report['timezone']['tzif_sha256'])
    window = report['window']
    end = date.fromisoformat(window['as_of'])
    require(end.isoformat() == window['as_of'])
    require(window['start'] == (end - timedelta(days=window['days'])).isoformat() and window['end'] == (end - timedelta(days=1)).isoformat())
    require(len(report['days']) == window['days'])
    require(report['family_reasons'] == sorted(set(report['family_reasons'])) and set(report['family_reasons']) <= {'selection_unresolved', 'selected_time_unattributable'})
    require(sum(c['occurrences'] for c in report['archive_counts'].values()) == sum(g['record_count'] for g in cat['families']['steps']))
    for p in PARTITIONS:
        _counts(report['archive_counts'][p], True)
        _counts(report['window_counts'][p])
        require(report['window_counts'][p]['occurrences'] <= report['archive_counts'][p]['time_attributable'])
    for i, row in enumerate(report['days']):
        require(row['date'] == (end - timedelta(days=window['days'] - i)).isoformat())
        require(row['reasons'] == sorted(set(row['reasons'])))
        for p in PARTITIONS:
            _counts(row['counts'][p])
            require(all(row['counts'][p][k] <= report['window_counts'][p][k] for k in row['counts'][p]))
        state = row['calculation_state']
        if state == 'computed':
            require(type(row['value']) is float and row['value'] >= 0 and math.copysign(1, row['value']) == 1)
            require(not row['reasons'] and row['counts']['selected']['occurrences'] > 0)
        else:
            require(row['value'] is None)
        if state == 'no_civil_day':
            require(row['utc_start'] is None and row['utc_end'] is None and row['utc_duration_seconds'] == 0)
            require(row['reasons'] == ['no_civil_day'] and row['repeat_candidate_extras'] == 0)
            require(all(c == 0 for p in row['counts'].values() for c in p.values()) and all(v is False for v in row['diagnostics'].values()))
        else:
            require(row['utc_duration_seconds'] > 0 and type(row['utc_start']) is str and type(row['utc_end']) is str)
            require(set(report['family_reasons']) <= set(row['reasons']))
            if state == 'blocked':
                require(bool(row['reasons']))
            elif state == 'no_records':
                require(not row['reasons'] and row['counts']['selected']['occurrences'] == 0)
    return value


@safe_validation
def validate_receipt(value, batch=None):
    structural('receipt', value)
    _uuid(value['attempt_id'])
    from datetime import datetime
    started, finished = (datetime.fromisoformat(value[k]) for k in ('started_at', 'finished_at'))
    require(started.utcoffset() == timedelta(0) and finished.utcoffset() == timedelta(0) and finished >= started)
    require(value['batch_id'] == digest({'kind': 'health-observation-batch-v1', 'payload': value['identities']}))
    if batch is not None:
        validate_batch(batch)
        require(value['batch_id'] == batch['batch_id'] and value['batch_sha256'] == digest(batch) and value['identities'] == identities(batch))
    return value


@safe_validation
def validate_failure(value):
    structural('failure', value)
    require((value['error'] == 'DEFINITION_CONFLICT') == (value['conflict'] is not None))
    if value['conflict']:
        fields = value['conflict']['differing_fields']
        require(fields == sorted(set(fields)))
        require(value['conflict']['actual_definition_id'] != value['conflict']['expected_definition_id'])
    return value


@safe_validation
def validate_prepared(value):
    structural('prepared', value)
    validate_receipt(value['receipt'], value['batch'])
    return value
