"""Test-only process worker: checkpoint synchronization over stdout/stdin."""
import json
from pathlib import Path
import sys
from adapters.health_observations import InputSet
from observation_store import store

operation, phase, path, store_id, attempt, fixture_dir = sys.argv[1:]
folder = Path(fixture_dir)
def checkpoint(current):
    if current == phase:
        print('READY', flush=True)
        sys.stdin.buffer.read(1)
store._checkpoint = checkpoint
definition = json.loads((folder / 'definition.json').read_bytes())
if operation == 'init':
    store.initialize(path, store_id, definition)
else:
    inputs = InputSet(folder / 'archive.zip', folder / 'catalogue.json', folder / 'binding.json', 'Etc/UTC', '2026-01-02', 1)
    store.import_observations(path, store_id, inputs, definition, attempt)
