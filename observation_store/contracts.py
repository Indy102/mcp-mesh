"""Closed D2 wire contracts. Validation does not attest fresh replay."""
from datetime import datetime, timedelta
from functools import wraps
import re
import uuid
from adapters.health_observations import contracts as d1
from .errors import ERROR_CODES

canonical, digest, strict_load = d1.canonical, d1.digest, d1.strict_load
H = {'type': 'string', 'pattern': '^[0-9a-f]{64}$', 'minLength': 64, 'maxLength': 64}
S, NULL = {'type': 'string'}, {'type': 'null'}

def obj(**fields):
    return {'type': 'object', 'properties': fields, 'required': list(fields), 'additionalProperties': False}

def const(value):
    return {'type': 'string', 'const': value}

def nullable(schema):
    return {'oneOf': [NULL, schema]}

ERROR = {'type': 'string', 'enum': list(ERROR_CODES)}
CONFLICT = obj(actual_definition_id=H, expected_definition_id=H, differing_fields={
    'type': 'array', 'minItems': 1, 'uniqueItems': True,
    'items': {'type': 'string', 'enum': list(d1.DEFINITION_SCHEMA['properties'])}})
MANIFEST_SCHEMA = obj(schema_version=const('health-observation-store-v1'), store_id=S,
    definition=d1.DEFINITION_SCHEMA, definition_id=H, flush_profile=const('fsync-files-and-directories-v1'))
INTENT_SCHEMA = obj(schema_version=const('health-observation-intent-v1'), store_id=S, attempt_id=S,
    definition_id=H, batch_id=H, batch_sha256=H, identities=d1.IDENTITIES, verification=d1.RECEIPT_SCHEMA)
RESULT_SCHEMA = obj(schema_version=const('health-observation-result-v1'), store_id=S, attempt_id=S,
    definition_id=H, outcome={'type': 'string', 'enum': ['COMMITTED', 'FAILED', 'INTERRUPTED']},
    intent_sha256=nullable(H), batch_id=nullable(H), batch_sha256=nullable(H),
    verification=nullable(d1.RECEIPT_SCHEMA), error=nullable(ERROR), recorded_at=S,
    storage_version=const('health-observation-storage-v1'))
LISTING_SCHEMA = obj(schema_version=const('health-observation-list-v1'), status=const('PASS'), operation=const('list'),
    store_id=S, definition_id=H, batches={'type': 'array', 'items': obj(batch_id=H, definition_id=H,
    archive_sha256=H, report_sha256=H, committed_attempt_ids={'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': S})})
RECOVERY_SCHEMA = obj(schema_version=const('health-observation-recovery-v1'), status=const('PASS'), operation=const('recover'),
    store_id=S, attempt_id=S, outcome={'type': 'string', 'enum': ['COMMITTED', 'FAILED', 'INTERRUPTED', 'NOT_RECORDED']},
    batch_id=nullable(H), error=nullable(ERROR))
FAILURE_SCHEMA = obj(schema_version=const('health-observation-store-failure-v1'), status=const('FAIL'),
    error=ERROR, store_id=nullable(S), attempt_id=nullable(S), conflict=nullable(CONFLICT))
STATUS_SCHEMA = obj(schema_version=const('health-observation-status-v1'), status=const('PASS'),
    operation={'type': 'string', 'enum': ['init', 'status']}, store_id=S, definition_id=H,
    initialized={'type': 'boolean', 'const': True}, flush_profile=const('fsync-files-and-directories-v1'))
OPERATION_SCHEMA = obj(schema_version=const('health-observation-operation-v1'), status=const('PASS'),
    operation={'type': 'string', 'enum': ['import', 'read', 'list', 'recover']},
    store_id=S, attempt_id=nullable(S), batch_id=nullable(H))
SCHEMAS = dict(manifest=MANIFEST_SCHEMA, intent=INTENT_SCHEMA, result=RESULT_SCHEMA,
               listing=LISTING_SCHEMA, recovery=RECOVERY_SCHEMA, failure=FAILURE_SCHEMA,
               status=STATUS_SCHEMA, operation=OPERATION_SCHEMA)

def require(test):
    if not test:
        raise ValueError('INTEGRITY_ERROR')

def uuid4(value):
    require(type(value) is str)
    parsed = uuid.UUID(value)
    require(parsed.version == 4 and str(parsed) == value)
    return value

def checked(fn):
    @wraps(fn)
    def call(*a, **kw):
        try:
            return fn(*a, **kw)
        except Exception:
            # ValueError avoids recursive StoreError construction for its own schema.
            raise ValueError('INTEGRITY_ERROR') from None
    return call

def structure(kind, value):
    require(d1.Validator(SCHEMAS[kind]).is_valid(value))
    canonical(value)

def linkage(value, manifest=None, attempt_id=None):
    uuid4(value['store_id'])
    if 'attempt_id' in value:
        uuid4(value['attempt_id'])
        if attempt_id is not None:
            uuid4(attempt_id)
            require(value['attempt_id'] == attempt_id)
    if manifest is not None:
        validate_manifest(manifest)
        require(value['store_id'] == manifest['store_id'])
        if 'definition_id' in value:
            require(value['definition_id'] == manifest['definition_id'])

@checked
def validate_manifest(value, *, store_id=None):
    structure('manifest', value)
    linkage(value)
    d1.validate_definition(value['definition'])
    require(d1.definition_id(value['definition']) == value['definition_id'])
    if store_id is not None:
        uuid4(store_id)
        require(store_id == value['store_id'])
    return value

@checked
def validate_intent(value, *, manifest=None, attempt_id=None, batch=None):
    structure('intent', value)
    linkage(value, manifest, attempt_id)
    receipt = value['verification']
    d1.validate_receipt(receipt, batch)
    require(receipt['attempt_id'] != value['attempt_id'])
    require(value['identities']['definition_id'] == value['definition_id'])
    for field in ('batch_id', 'batch_sha256', 'identities'):
        require(value[field] == receipt[field])
    if batch is not None:
        require(value['identities'] == d1.identities(batch))
        if manifest is not None:
            require(canonical(batch['definition']) == canonical(manifest['definition']))
    return value

@checked
def validate_result(value, *, manifest=None, attempt_id=None, intent=None, batch=None):
    structure('result', value)
    linkage(value, manifest, attempt_id)
    stamp = value['recorded_at']
    require(re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', stamp) is not None)
    require(datetime.fromisoformat(stamp).utcoffset() == timedelta(0))
    if intent is not None:
        validate_intent(intent, manifest=manifest, attempt_id=value['attempt_id'], batch=batch if value['outcome'] == 'COMMITTED' else None)
        require(intent['store_id'] == value['store_id'] and intent['definition_id'] == value['definition_id'])
        require(digest(intent) == value['intent_sha256'])
    else:
        require(value['intent_sha256'] is None)
    if value['outcome'] == 'COMMITTED':
        require(intent is not None and batch is not None and value['error'] is None)
        d1.validate_receipt(value['verification'], batch)
        for key in ('batch_id', 'batch_sha256', 'verification'):
            require(value[key] == intent[key])
    else:
        require(all(value[k] is None for k in ('batch_id', 'batch_sha256', 'verification')))
        require(value['error'] is not None)
        if value['outcome'] == 'INTERRUPTED':
            require(intent is not None)
    return value

@checked
def validate_listing(value, *, manifest=None):
    structure('listing', value)
    linkage(value, manifest)
    ids = [b['batch_id'] for b in value['batches']]
    require(ids == sorted(set(ids)))
    for batch in value['batches']:
        require(batch['definition_id'] == value['definition_id'])
        attempts = batch['committed_attempt_ids']
        require(attempts == sorted(set(attempts)))
        for attempt in attempts:
            uuid4(attempt)
    return value

@checked
def validate_recovery(value, *, store_id=None, attempt_id=None):
    structure('recovery', value)
    linkage(value, attempt_id=attempt_id)
    if store_id is not None:
        require(uuid4(store_id) == value['store_id'])
    committed = value['outcome'] == 'COMMITTED'
    require((value['batch_id'] is not None) == committed)
    require((value['error'] is not None) == (value['outcome'] in ('FAILED', 'INTERRUPTED')))
    return value

@checked
def validate_failure(value):
    structure('failure', value)
    for key in ('store_id', 'attempt_id'):
        if value[key] is not None:
            uuid4(value[key])
    require((value['error'] == 'DEFINITION_CONFLICT') == (value['conflict'] is not None))
    if value['conflict'] is not None:
        c = value['conflict']
        require(c['actual_definition_id'] != c['expected_definition_id'])
        require(c['differing_fields'] == sorted(set(c['differing_fields'])))
    return value

@checked
def validate_status(value, *, manifest=None):
    structure('status', value)
    linkage(value, manifest)
    return value

@checked
def validate_operation(value):
    structure('operation', value)
    uuid4(value['store_id'])
    if value['attempt_id'] is not None:
        uuid4(value['attempt_id'])
    require((value['attempt_id'] is not None) == (value['operation'] in ('import', 'recover')))
    if value['operation'] in ('import', 'read'):
        require(value['batch_id'] is not None)
    if value['operation'] == 'list':
        require(value['batch_id'] is None)
    return value
