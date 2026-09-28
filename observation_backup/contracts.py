"""Closed public acknowledgements and backup contracts."""
from observation_store import contracts as d2
from adapters.health_observations import contracts as d1
import re
from datetime import datetime, timedelta
canonical, strict_load, digest = (d2.canonical, d2.strict_load, d2.digest)
ERROR_CODES = frozenset('INPUT_INVALID CLI_USAGE LOCATION_INVALID DESTINATION_EXISTS NOT_SEALED INTEGRITY_ERROR STORE_ID_MISMATCH WRITER_BUSY NESTED_OPERATION STORAGE_ERROR PLATFORM_UNSUPPORTED REPLAY_FAILED RECOVERY_INCOMPLETE PUBLICATION_UNKNOWN INTERNAL_ERROR'.split())

class BackupError(Exception):

    def __init__(self, code, operation='backup'):
        self.code = code if code in ERROR_CODES else 'INTERNAL_ERROR'
        self.failure_bytes = acknowledgement(operation, self.code)
        super().__init__(self.code)

def acknowledgement(operation, error=None):
    return canonical(dict(status='FAIL' if error else 'PASS', operation=operation, error=error))

def require(value):
    if not value:
        raise ValueError('INTEGRITY_ERROR')

def closed(value, keys):
    require(type(value) is dict and set(value) == set(keys.split()))

def hash_value(value):
    require(type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None)

def json_bytes(blob):
    value = strict_load(blob)
    require(canonical(value) == blob)
    return value

def portable(path):
    require(type(path) is str)
    require(path == '' or all((re.fullmatch('[A-Za-z0-9_.-]+', part) and part not in ('.', '..') for part in path.split('/'))))

def layout(directories, files):
    require(type(directories) is list and directories == sorted(set(directories)))
    require(type(files) is list)
    names = []
    for item in files:
        closed(item, 'path size sha256')
        portable(item['path'])
        require(item['path'] != '')
        require(type(item['size']) is int and item['size'] >= 0)
        hash_value(item['sha256'])
        names.append(item['path'])
    require(names == sorted(set(names)))
    required = {'', 'archives', 'attempts', 'batches', 'staging'}
    require(required <= set(directories) and (not set(names) & set(directories)))
    for path in directories:
        portable(path)
        require(path in required or (path.startswith('staging/') and path.count('/') == 1))
    require({'manifest.json', 'writer.lock'} <= set(names))
    for path in names:
        parts = path.split('/')
        require('/'.join(parts[:-1]) in directories)
        if len(parts) == 1:
            require(path in ('manifest.json', 'writer.lock'))
        elif parts[0] == 'archives':
            require(len(parts) == 2 and re.fullmatch('[0-9a-f]{64}\\.zip', parts[1]))
        elif parts[0] == 'batches':
            require(len(parts) == 2 and re.fullmatch('[0-9a-f]{64}\\.json', parts[1]))
        elif parts[0] == 'attempts':
            require(len(parts) == 2 and parts[1].endswith(('.intent.json', '.result.json')))
            d2.uuid4(parts[1][:-12])
        else:
            require(parts[0] == 'staging' and len(parts) in (2, 3))

def validate_manifest(value):
    closed(value, 'schema_version backup_id captured_at scope source_manifest directories files listing')
    require(value['schema_version'] == 'health-observation-backup-v1' and value['scope'] == 'complete-d2-managed-tree-v1')
    d2.uuid4(value['backup_id'])
    stamp = value['captured_at']
    require(type(stamp) is str and re.fullmatch('\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}(?:\\.\\d{1,6})?(?:Z|\\+00:00)', stamp))
    require(datetime.fromisoformat(stamp).utcoffset() == timedelta(0))
    d2.validate_manifest(value['source_manifest'])
    layout(value['directories'], value['files'])
    d2.validate_listing(value['listing'], manifest=value['source_manifest'])
    return value
