"""Argument-independent public acknowledgements."""
from adapters.health_observations.contracts import canonical
from observation_store.errors import ERROR_CODES


def acknowledgement(error=None):
    return canonical({'status': 'PASS' if error is None else 'FAIL',
                      'operation': 'view', 'error': error})


class ViewError(Exception):
    def __init__(self, code):
        self.code = code if code in (*ERROR_CODES, 'OUTPUT_PUBLICATION_UNKNOWN') else 'INTERNAL_ERROR'
        self.failure_bytes = acknowledgement(self.code)
        super().__init__(self.code)
