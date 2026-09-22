"""Synthetic wire checks; receipt fixture is NOT a replay admission."""
import copy
import unittest
from observation_store import contracts as c
from observation_store.errors import StoreError
from adapters.health_observations import contracts as d

U = '00000000-0000-4000-8000-000000000001'
V = '00000000-0000-4000-8000-000000000002'
H = 'a' * 64


def fixtures():
    definition = dict(schema_version='health-observation-definition-v1', metric='selected_source_recorded_steps', unit='count',
        observation_schema='mesh-steps-report-v1', source_scope='app_stream', selected_group_ids=[H],
        catalogue_descriptor_version='mesh-health-catalogue-v1', descriptor_hash_version='sha256_canonical_family_tagged_descriptor_v1',
        timezone=dict(key='Etc/UTC', tzif_sha256=H), rules=dict(d.RULES))
    did = d.definition_id(definition)
    manifest = dict(schema_version='health-observation-store-v1', store_id=U, definition=definition, definition_id=did,
                    flush_profile='fsync-files-and-directories-v1')
    identities = dict(definition_id=did, archive_sha256=H, catalogue_sha256=H, binding_sha256=H, report_sha256=H)
    bid = d.digest(dict(kind='health-observation-batch-v1', payload=identities))
    receipt = dict(schema_version='health-observation-verification-receipt-v1', attempt_id=V, batch_id=bid, batch_sha256=H,
        identities=identities, outcome='PASS', verification_scope='selected_steps_report_exact_recomputation', checks=d.CHECKS[:],
        started_at='2026-01-01T00:00:00+00:00', finished_at='2026-01-01T00:00:00+00:00', input_mode='evaluated',
        code_sha256={k:H for k in d.CODE_FILES}, adapter_sha256=H,
        runtime=dict(implementation='synthetic', version='synthetic', executable_sha256=H, expat_version='synthetic', jsonschema_version='synthetic'),
        resource_envelope=dict(subprocess_timeout_seconds=1, value_token_characters='4096', exponent_magnitude='100000'))
    intent = dict(schema_version='health-observation-intent-v1', store_id=U, attempt_id=U, definition_id=did,
                  batch_id=bid, batch_sha256=H, identities=identities, verification=receipt)
    result = dict(schema_version='health-observation-result-v1', store_id=U, attempt_id=U, definition_id=did,
        outcome='INTERRUPTED', intent_sha256=c.digest(intent), batch_id=None, batch_sha256=None, verification=None,
        error='PREPARATION_FAILED', recorded_at='2026-01-01T00:00:00Z', storage_version='health-observation-storage-v1')
    return manifest, intent, result


class Contracts(unittest.TestCase):
    def setUp(self):
        self.m, self.i, self.r = fixtures()

    def test_manifest_intent_interrupted(self):
        self.assertEqual(c.validate_manifest(self.m), self.m)
        c.validate_intent(self.i, manifest=self.m, attempt_id=U)
        c.validate_result(self.r, manifest=self.m, intent=self.i, attempt_id=U)

    def test_closed_version_types(self):
        for change in ({'extra': 1}, {'schema_version': 'future'}, {'store_id': True}, {'definition_id': H.upper()}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.validate_manifest(dict(self.m, **change))
        for value in (True, 1.0, float('nan'), float('inf')):
            intent = copy.deepcopy(self.i)
            intent['verification']['resource_envelope']['subprocess_timeout_seconds'] = value
            with self.assertRaises(ValueError):
                c.validate_intent(intent)

    def test_uuid_and_linkage(self):
        for key, value in [('attempt_id', V), ('store_id', V), ('definition_id', H), ('batch_sha256', 'b'*64)]:
            bad = dict(self.i, **{key:value})
            with self.assertRaises(ValueError):
                c.validate_intent(bad, manifest=self.m, attempt_id=U)
        with self.assertRaises(ValueError):
            c.validate_manifest(dict(self.m, store_id='00000000-0000-1000-8000-000000000001'))

    def test_nullability_time_digest(self):
        for change in ({'intent_sha256': H}, {'batch_id': H}, {'verification': self.i['verification']},
                       {'error': None}, {'recorded_at':'2026-01-01'}, {'recorded_at':'2026-02-30T00:00:00Z'},
                       {'outcome':'COMMITTED'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                c.validate_result(dict(self.r, **change), intent=self.i)
        with self.assertRaises(ValueError):
            c.validate_result(self.r)
        failed = dict(self.r, outcome='FAILED', intent_sha256=None)
        c.validate_result(failed)

    def test_json(self):
        for blob in (b'{"a":1,"a":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.assertRaises(ValueError):
                c.strict_load(blob)

    def test_failure_is_safe(self):
        for code in ('STORAGE_ERROR', '/secret/personal/path', 'DEFINITION_CONFLICT'):
            e = StoreError(code)
            c.validate_failure(c.strict_load(e.failure_bytes))
            self.assertNotIn('/secret', str(e))
        conflict = dict(actual_definition_id=H, expected_definition_id='b'*64, differing_fields=['timezone'])
        e = StoreError('DEFINITION_CONFLICT', store_id=U, conflict=conflict)
        self.assertEqual(e.code, 'DEFINITION_CONFLICT')
        conflict['differing_fields'] = ['secret']
        self.assertEqual(StoreError('DEFINITION_CONFLICT', conflict=conflict).code, 'INTERNAL_ERROR')

    def test_listing_recovery_status(self):
        entry = dict(batch_id=H, definition_id=self.m['definition_id'], archive_sha256=H, report_sha256=H, committed_attempt_ids=[U,V])
        listing = dict(schema_version='health-observation-list-v1', status='PASS', operation='list', store_id=U,
                       definition_id=self.m['definition_id'], batches=[entry])
        c.validate_listing(listing, manifest=self.m)
        with self.assertRaises(ValueError):
            c.validate_listing(dict(listing, batches=[entry,entry]))
        entry['committed_attempt_ids'] = [V,U]
        with self.assertRaises(ValueError):
            c.validate_listing(listing)
        recovery = dict(schema_version='health-observation-recovery-v1', status='PASS', operation='recover',
                        store_id=U, attempt_id=U, outcome='NOT_RECORDED', batch_id=None, error=None)
        c.validate_recovery(recovery)
        for change in ({'batch_id':H}, {'error':'INPUT_CHANGED'}, {'outcome':'COMMITTED'}):
            with self.assertRaises(ValueError):
                c.validate_recovery(dict(recovery, **change))
        c.validate_status(dict(schema_version='health-observation-status-v1', status='PASS', operation='status',
            store_id=U, definition_id=self.m['definition_id'], initialized=True, flush_profile=self.m['flush_profile']), manifest=self.m)

    def test_complete_committed_chain(self):
        import base64
        import hashlib
        descriptor = [['present','Invented'], ['missing'], ['missing']]
        gid = d.digest({'family':'steps','descriptor':descriptor})
        definition = copy.deepcopy(self.m['definition'])
        definition['selected_group_ids'] = [gid]
        definition['timezone']['tzif_sha256'] = hashlib.sha256(b'synthetic').hexdigest()
        did = d.definition_id(definition)
        cat = dict(catalogue_schema='mesh-health-catalogue-v1',archive_sha256=H,archive_bytes=3,xml_member='export.xml',record_count_all_types=1,
                   families=dict(steps=[dict(group_id=gid,descriptor=descriptor,record_count=1)],hrv_sdnn=[],sleep=[]))
        selection = dict(selection_handle=V,group_ids=[gid],source_scope='app_stream',user_choice='Invented',identity_evidence='Invented')
        binding = dict(binding_schema='mesh-health-selection-binding-v1',catalogue=cat,catalogue_sha256=d.digest(cat),
            selection_request=dict(selection_schema='mesh-health-selection-request-v1',archive_sha256=H,catalogue_sha256=d.digest(cat),selections=dict(steps=selection)),
            validation_scope='archive_integrity_and_selection_membership',device_identity_verified=False,user_intent_verified=False,health_source_priority_verified=False)
        counts = {p:{k:0 for k in ('occurrences','invalid_occurrences','invalid_value','invalid_unit')} for p in d.PARTITIONS}
        ac = {p:{k:0 for k in ('occurrences','time_attributable','time_unattributable','invalid_occurrences','invalid_time','invalid_value','invalid_unit')} for p in d.PARTITIONS}
        ac['selected'].update(occurrences=1,time_attributable=1)
        archive=dict(sha256=H,bytes=3,xml_member='export.xml')
        report=dict(schema_version='mesh-steps-report-v1',policy_version='mesh-selected-steps-v1',producer_version='mesh-steps-evaluator-v1',metric='selected_source_recorded_steps',unit='count',archive=archive,
            binding_sha256=d.digest(binding),selection_handle=V,window=dict(as_of='2026-01-02',days=1,start='2026-01-01',end='2026-01-01'),
            timezone=dict(key='Etc/UTC',tzif_base64=base64.b64encode(b'synthetic').decode(),tzif_sha256=definition['timezone']['tzif_sha256']),
            family_reasons=[],archive_counts=ac,window_counts=counts,archive_repeat_candidate_extras=0,window_repeat_candidate_extras=0,
            days=[dict(date='2026-01-01',utc_start='2026-01-01T00:00:00Z',utc_end='2026-01-02T00:00:00Z',utc_duration_seconds=86400,
                calculation_state='no_records',value=None,reasons=[],observation_completeness='unknown',valid_metric_day=None,inference_eligible=False,
                counts=counts,repeat_candidate_extras=0,diagnostics=dict(selected_quantity_overlap=False,selected_excluded_overlap=False))])
        batch=dict(schema_version='health-observation-batch-v1',batch_id=H,definition_id=did,definition=definition,archive=archive,
            catalogue=dict(catalogue=cat,catalogue_sha256=d.digest(cat)),binding=dict(binding=binding,binding_sha256=d.digest(binding)),report=dict(report=report,report_sha256=d.digest(report)))
        batch['batch_id']=d.batch_id(batch)
        d.validate_batch(batch)
        manifest=dict(self.m,definition=definition,definition_id=did)
        receipt=dict(self.i['verification'],batch_id=batch['batch_id'],batch_sha256=d.digest(batch),identities=d.identities(batch))
        intent=dict(self.i,definition_id=did,batch_id=batch['batch_id'],batch_sha256=d.digest(batch),identities=d.identities(batch),verification=receipt)
        result=dict(self.r,definition_id=did,outcome='COMMITTED',intent_sha256=d.digest(intent),batch_id=batch['batch_id'],batch_sha256=d.digest(batch),verification=receipt,error=None)
        c.validate_result(result,manifest=manifest,intent=intent,batch=batch,attempt_id=U)
        bad=copy.deepcopy(batch); bad['archive']['bytes']=4
        with self.assertRaises(ValueError): c.validate_result(result,manifest=manifest,intent=intent,batch=bad)
        with self.assertRaises(ValueError): c.validate_result(result,manifest=manifest,intent=intent)
