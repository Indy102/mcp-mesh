"""Fixed, path-free failures shared by all storage layers."""
ERROR_CODES = ('STORE_EXISTS', 'UNINITIALIZED', 'INITIALIZATION_UNKNOWN', 'STORE_ID_MISMATCH',
 'DEFINITION_INVALID', 'DEFINITION_CONFLICT', 'WRITER_BUSY', 'NESTED_OPERATION',
 'ATTEMPT_EXISTS', 'BATCH_NOT_COMMITTED', 'INPUT_CHANGED', 'PREPARATION_FAILED',
 'INTEGRITY_ERROR', 'STORAGE_ERROR', 'PUBLICATION_UNKNOWN', 'INPUT_INVALID',
 'OUTPUT_INVALID', 'OUTPUT_EXISTS', 'OUTPUT_NOT_PRIVATE', 'CLI_USAGE',
 'INTERNAL_ERROR', 'PLATFORM_UNSUPPORTED')


class StoreError(Exception):
    def __init__(self, code, *, store_id=None, attempt_id=None, conflict=None):
        from .contracts import validate_failure, canonical
        value = {'schema_version': 'health-observation-store-failure-v1', 'status': 'FAIL',
                 'error': code if code in ERROR_CODES else 'INTERNAL_ERROR',
                 'store_id': store_id, 'attempt_id': attempt_id, 'conflict': conflict}
        # Invalid exception metadata must itself remain a safe error.
        try:
            validate_failure(value)
        except (ValueError, StoreError):
            value.update(error='INTERNAL_ERROR', store_id=None, attempt_id=None, conflict=None)
        self.code = value['error']
        self.store_id, self.attempt_id, self.conflict = value['store_id'], value['attempt_id'], value['conflict']
        self.failure_bytes = canonical(value)
        super().__init__(self.code)
