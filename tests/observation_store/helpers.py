"""Invented source fixture using the public catalogue commands and real D1."""
import json
import shutil
from pathlib import Path
import subprocess
import sys
import zipfile
from adapters.health_observations import InputSet, inspect_definition
from observation_store.contracts import canonical

ROOT = Path(__file__).resolve().parents[2]


def fixture(folder, repeated=False):
    archive, catalogue, binding, request = (folder / n for n in ('archive.zip', 'catalogue.json', 'binding.json', 'request.json'))
    with zipfile.ZipFile(archive, 'w') as z:
        record = '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Invented Wrist" sourceVersion="1" device="Invented hardware" unit="count" value="14" startDate="2026-01-01T12:00:00Z" endDate="2026-01-01T12:00:00Z" />'
        z.writestr('export.xml', '<HealthData>' + record * (2 if repeated else 1) + '</HealthData>')
    def command(*arguments):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'spikes/health_catalogue/catalogue.py'), *map(str, arguments)], capture_output=True, timeout=90, cwd=ROOT)
        if result.returncode != 0:
            raise AssertionError('synthetic catalogue command failed')
    command('build', '--archive', archive, '--output', catalogue)
    cat = json.loads(catalogue.read_bytes())
    request.write_bytes(canonical(dict(selection_schema='mesh-health-selection-request-v1', archive_sha256=cat['catalogue']['archive_sha256'], catalogue_sha256=cat['catalogue_sha256'], selections={'steps': dict(selection_handle='00000000-0000-4000-8000-000000000010', group_ids=sorted(g['group_id'] for g in cat['catalogue']['families']['steps']), source_scope='app_stream', user_choice='Invented choice.', identity_evidence='Invented source.')})))
    command('bind', '--archive', archive, '--catalogue', catalogue, '--request', request, '--output', binding)
    inputs = InputSet(archive, catalogue, binding, 'Etc/UTC', '2026-01-02', 1)
    return inputs, inspect_definition(inputs)


def tree(path):
    return {str(p.relative_to(path)): (p.stat().st_mode, p.read_bytes() if p.is_file() else None) for p in path.rglob('*')}


def source_copy(folder):
    """Execute actual packages from a marker-free distribution, without caches."""
    source = folder.resolve() / 'source'
    source.mkdir(mode=0o700)
    for package in ('observation_store', 'adapters'):
        shutil.copytree(ROOT / package, source / package, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (source / 'private').mkdir(mode=0o700)
    alias = folder.resolve() / 'source-alias'
    alias.symlink_to(source, target_is_directory=True)
    return source, alias
