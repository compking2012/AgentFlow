"""Local transfer-copy retirement uses temporary node roots, never controller files."""
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from agentflow.common import canonical_digest
from node_agent.daemon import NodeDaemon
from node_agent.journal import NodeJournal
from node_agent.maintenance import NodeMaintenance


@pytest.fixture
def node(tmp_path):
    root = (tmp_path / 'node').resolve()
    journal = NodeJournal(root / 'journal.sqlite')
    value = SimpleNamespace(root=root, journal=journal, maintenance=NodeMaintenance(root, journal))
    yield value
    journal.close()


def completed_job(node, *, job_id='job', roles=('test',), ack=True, cleanup=True,
                  operation_complete=True, change_result=None, change_payload=None, receipt=None):
    assignment = {'job_id': job_id, 'attempt_id': 'attempt-' + job_id, 'fencing_token': 1,
        'input_fingerprint': canonical_digest('input-' + job_id), 'attempt_token': 'fixture-token', 'resource_leases': []}
    evidence = node.root / 'workspaces' / hashlib.sha256(job_id.encode()).hexdigest() / 'evidence'
    evidence.mkdir(parents=True)
    files = []
    for role in roles:
        path = evidence / f'built-{role}.tar'
        content = ('fixture transfer copy: ' + role).encode()
        path.write_bytes(content)
        files.append({'path': str(path), 'component_role': role, 'component_id': job_id + '-' + role,
            'digest': 'sha256:' + hashlib.sha256(content).hexdigest(), 'size': len(content)})
    (evidence / 'stdout.log').write_text('Keep the log')
    (evidence / 'report.json').write_text('{"status":"fixture"}')
    result = {'execution_status': 'completed', 'quality_result': 'passed', 'state': 'completed',
        'cleanup_verified': True, 'process_results': [], 'built_artifacts': files,
        'artifact_files': [{'path': item['path'], 'format': 'build_artifact'} for item in files]}
    if change_result:
        change_result(result)
    uploaded = [{field: item[field] for field in ('component_id', 'component_role', 'digest')}
        | {'artifact_version_id': 'uploaded-' + item['component_id']} for item in result['built_artifacts']]
    payload = {'operation_id': canonical_digest({'job': job_id, 'result': result})[7:39],
        'input_fingerprint': assignment['input_fingerprint'], 'fencing_token': assignment['fencing_token'],
        'built_artifacts': uploaded, 'artifact_version_ids': [item['artifact_version_id'] for item in uploaded]}
    if change_payload:
        change_payload(payload)
    node.journal.record_assignment(assignment)
    node.journal.mark_starting(job_id)
    node.journal.record_result(job_id, result)
    node.journal.pending_operation('result:' + job_id, payload)
    if ack:
        node.journal.record_delivery(job_id, receipt or {'job_id': job_id, 'result_id': 'result-' + job_id,
                                                       'assessment_state': 'validated'})
        if operation_complete:
            node.journal.finish_operation('result:' + job_id)
        if cleanup:
            node.journal.finish_cleanup(job_id)
    inputs = node.root / 'inputs'
    inputs.mkdir(exist_ok=True)
    cached = inputs / hashlib.sha256(('input-' + job_id).encode()).hexdigest()
    cached.write_bytes(b'downloaded input copy')
    return assignment, result, payload, evidence, cached


def test_retirement_removes_only_acknowledged_fixed_packages_and_idle_cache(node):
    _, result, _, evidence, cached = completed_job(node, roles=('product', 'test', 'service', 'data'))
    keep = [evidence / 'stdout.log', evidence / 'report.json', evidence / 'built-other.tar',
            node.root / 'node.key', node.root / 'node.json', node.root / 'inputs' / 'notes.txt']
    for path in keep[2:]:
        path.write_text('Keep this file')
    unrelated = node.root.parent / 'controller-cas'
    unrelated.write_bytes(b'controller evidence')
    expected_bytes = sum(item['size'] for item in result['built_artifacts']) + cached.stat().st_size
    before = node.journal.get('job')
    summary = node.maintenance.sweep()
    assert summary['removed_files'] == 5 and summary['removed_bytes'] == expected_bytes
    assert all(not Path(item['path']).exists() for item in result['built_artifacts'])
    assert not cached.exists() and all(path.is_file() for path in keep)
    assert unrelated.read_bytes() == b'controller evidence'
    assert node.journal.get('job') == before
    again = NodeMaintenance(node.root, node.journal)
    assert again.sweep()['removed_files'] == 0
    assert again.statistics()['removed_files'] == 5
    assert again.statistics()['removed_bytes'] == expected_bytes


@pytest.mark.parametrize('options,reason', [({'ack': False}, 'result_ack_missing'),
    ({'cleanup': False}, 'cleanup_unconfirmed'), ({'operation_complete': False}, 'result_ack_missing'),
    ({'change_result': lambda value: value.update(cleanup_verified=False)}, 'cleanup_unconfirmed'),
    ({'change_result': lambda value: value.update(execution_status='execution_unknown')}, 'job_not_terminal'),
    ({'receipt': {'job_id': 'other-job', 'result_id': 'result', 'assessment_state': 'validated'}}, 'result_ack_missing'),
    ({'change_payload': lambda value: value.update(operation_id='different-operation')}, 'result_ack_identity_mismatch')])
def test_missing_ack_cleanup_or_identity_keeps_packages_and_inputs(node, options, reason):
    _, result, _, _, cached = completed_job(node, **options)
    summary = node.maintenance.sweep()
    assert summary['retained_jobs'] == {'job': reason}
    assert summary['inputs_blocked'] and summary['removed_files'] == 0
    assert cached.exists() and Path(result['built_artifacts'][0]['path']).exists()


@pytest.mark.parametrize('state', ['received', 'starting', 'running', 'execution_unknown'])
def test_any_recoverable_job_prevents_removal_of_shared_download_cache(node, state):
    _, result, _, _, cached = completed_job(node)
    assignment = {'job_id': 'reader', 'attempt_id': 'reader-attempt', 'fencing_token': 1, 'input_fingerprint': 'reader-input'}
    node.journal.record_assignment(assignment)
    with node.journal.connection:
        node.journal.connection.execute('UPDATE jobs SET state=? WHERE job_id=?', (state, 'reader'))
    summary = node.maintenance.sweep()
    assert not Path(result['built_artifacts'][0]['path']).exists()
    assert cached.exists() and summary['inputs_blocked']


@pytest.mark.parametrize('damage', ['digest', 'size', 'path', 'upload'])
def test_fixed_package_must_match_its_recorded_upload_and_actual_bytes(node, damage):
    def alter_result(value):
        item = value['built_artifacts'][0]
        if damage == 'digest':
            item['digest'] = canonical_digest('other bytes')
        elif damage == 'size':
            item['size'] += 1
        elif damage == 'path':
            item['path'] = str(node.root.parent / 'outside.tar')
    def alter_payload(value):
        if damage == 'upload':
            value['artifact_version_ids'] = []
    _, _, _, evidence, _ = completed_job(node, change_result=alter_result, change_payload=alter_payload)
    summary = node.maintenance.sweep()
    assert (evidence / 'built-test.tar').is_file()
    assert summary['failed'] == 1
    assert node.maintenance.statistics()['retained'][0]['status'] == 'retained'


@pytest.mark.parametrize('where', ['archive', 'evidence', 'job_directory', 'workspaces', 'input', 'input_directory'])
def test_links_are_never_followed_during_deletion(node, where):
    _, result, _, evidence, cached = completed_job(node)
    archive = Path(result['built_artifacts'][0]['path'])
    if where == 'input':
        target = cached
    elif where == 'input_directory':
        target = cached.parent
    elif where == 'archive':
        target = archive
    elif where == 'evidence':
        target = evidence
    elif where == 'job_directory':
        target = evidence.parent
    else:
        target = evidence.parent.parent
    outside = node.root.parent / 'linked-target'
    target.rename(outside)
    target.symlink_to(outside, target_is_directory=outside.is_dir())
    before = sorted((str(path.relative_to(outside)), path.read_bytes()) for path in outside.rglob('*') if path.is_file()) if outside.is_dir() else outside.read_bytes()
    summary = node.maintenance.sweep()
    after = sorted((str(path.relative_to(outside)), path.read_bytes()) for path in outside.rglob('*') if path.is_file()) if outside.is_dir() else outside.read_bytes()
    assert before == after and target.is_symlink()
    assert summary['failed'] >= 1


@pytest.mark.parametrize('mode', ['alive', 'access_denied', 'missing_created', 'unknown_result', 'journal_mismatch'])
def test_alive_unknown_or_unmatched_processes_keep_every_local_copy(node, monkeypatch, mode):
    observed = {'pid': os.getpid(), 'process_created': psutil.Process().create_time(),
        'process_fingerprint': canonical_digest('process'), 'execution_status': 'completed', 'cleanup_verified': True}
    if mode == 'missing_created':
        observed['process_created'] = None
    elif mode == 'unknown_result':
        observed['execution_status'] = 'execution_unknown'
    def changes(result):
        if mode != 'journal_mismatch':
            result.update(process_results=[observed])
    _, result, _, _, cached = completed_job(node, change_result=changes)
    if mode == 'journal_mismatch':
        with node.journal.connection:
            node.journal.connection.execute('UPDATE jobs SET pid=?,process_created=?,process_fingerprint=? WHERE job_id=?',
                (observed['pid'], observed['process_created'], observed['process_fingerprint'], 'job'))
    if mode == 'access_denied':
        def denied(_):
            raise psutil.AccessDenied()
        monkeypatch.setattr('node_agent.maintenance.psutil.Process', denied)
    summary = node.maintenance.sweep()
    assert summary['removed_files'] == 0 and summary['inputs_blocked']
    assert Path(result['built_artifacts'][0]['path']).exists() and cached.exists()


def test_known_stopped_process_can_retire_its_uploaded_copy(node):
    observed = {'pid': 1073741824, 'process_created': 1.0, 'process_fingerprint': canonical_digest('stopped'),
        'execution_status': 'completed', 'cleanup_verified': True}
    _, result, _, _, _ = completed_job(node, change_result=lambda value: value.update(process_results=[observed]))
    assert node.maintenance.sweep()['removed_files'] == 2
    assert not Path(result['built_artifacts'][0]['path']).exists()


def test_cleanup_failure_is_persisted_and_retried(node, monkeypatch):
    _, result, _, _, _ = completed_job(node)
    original = NodeMaintenance._unlink
    def deny_archive(name, directory):
        if name == 'built-test.tar':
            raise PermissionError('fixture failure')
        original(name, directory)
    monkeypatch.setattr(NodeMaintenance, '_unlink', staticmethod(deny_archive))
    assert node.maintenance.sweep()['failed'] == 1
    assert Path(result['built_artifacts'][0]['path']).exists()
    retained = node.maintenance.statistics()['retained']
    assert len(retained) == 1 and retained[0]['last_error'] == 'PermissionError'
    monkeypatch.setattr(NodeMaintenance, '_unlink', staticmethod(original))
    assert NodeMaintenance(node.root, node.journal).sweep()['removed_files'] == 1
    assert node.maintenance.statistics()['removed_files'] == 2
    assert node.maintenance.statistics()['retained'] == []


def test_file_replaced_while_saving_intent_is_not_removed(node, monkeypatch):
    _, result, _, _, _ = completed_job(node)
    package = Path(result['built_artifacts'][0]['path'])
    original = NodeMaintenance._record
    def replace_after_intent(connection, relative, status, **kwargs):
        original(connection, relative, status, **kwargs)
        if relative.endswith('/built-test.tar') and status == 'pending':
            package.unlink()
            package.write_bytes(b'unverified replacement')
    monkeypatch.setattr(NodeMaintenance, '_record', staticmethod(replace_after_intent))
    assert node.maintenance.sweep()['failed'] == 1
    assert package.read_bytes() == b'unverified replacement'


@pytest.mark.parametrize('target', ['built-test.tar', 'input'])
def test_crash_after_unlink_reconciles_the_durable_intent_without_double_counting(node, monkeypatch, target):
    _, _, _, _, cached = completed_job(node, roles=() if target == 'input' else ('test',))
    original = NodeMaintenance._unlink
    class SimulatedCrash(BaseException):
        pass
    def crash(name, directory):
        original(name, directory)
        if name == (cached.name if target == 'input' else target):
            raise SimulatedCrash()
    monkeypatch.setattr(NodeMaintenance, '_unlink', staticmethod(crash))
    with pytest.raises(SimulatedCrash):
        node.maintenance.sweep()
    monkeypatch.setattr(NodeMaintenance, '_unlink', staticmethod(original))
    fresh = NodeMaintenance(node.root, node.journal)
    fresh.sweep()
    total = 1 if target == 'input' else 2
    assert fresh.statistics()['removed_files'] == total
    fresh.sweep()
    assert fresh.statistics()['removed_files'] == total


async def test_lost_result_ack_keeps_packages_for_retransmission_until_delivery_is_durable(node):
    assignment, result, payload, evidence, cached = completed_job(node, ack=False)
    class Client:
        config = {'node_id': 'node'}
        def __init__(self):
            self.requests = []
        async def json_request(self, method, path, **kwargs):
            assert method == 'POST' and path.endswith('/results')
            self.requests.append(json.loads(json.dumps(kwargs['payload'])))
            if len(self.requests) == 1:
                raise ConnectionError('response lost after controller acceptance')
            return {'job_id': 'job', 'result_id': 'result', 'assessment_state': 'validated'}
        async def upload(self, *args):
            raise AssertionError('Acknowledgement retry must not upload again')
    daemon = object.__new__(NodeDaemon)
    daemon.directory, daemon.journal, daemon.client = node.root, node.journal, Client()
    with pytest.raises(ConnectionError):
        await daemon._execute(assignment, saved_result=result)
    assert node.maintenance.sweep()['removed_files'] == 0
    assert (evidence / 'built-test.tar').exists() and cached.exists()
    accepted = await daemon._execute(assignment, saved_result=result)
    assert accepted['state'] == 'completed' and not accepted['cleanup_required']
    assert daemon.client.requests == [payload, payload]
    assert not (evidence / 'built-test.tar').exists() and not cached.exists()
    assert (evidence / 'stdout.log').exists() and (evidence / 'report.json').exists()


async def test_daemon_start_and_completed_cleanup_invoke_maintenance_without_changing_success(node, monkeypatch):
    assignment, result, _, _, _ = completed_job(node, cleanup=False)
    daemon = object.__new__(NodeDaemon)
    daemon.directory, daemon.journal = node.root, node.journal
    calls = []
    def failed_sweep(_):
        calls.append('maintenance')
        raise OSError('temporary local failure')
    async def refresh():
        return ['capability']
    daemon.refresh_capabilities = refresh
    monkeypatch.setattr(NodeMaintenance, 'sweep', failed_sweep)
    assert await daemon.initialize() == ['capability']
    assert await daemon._cleanup(assignment, result)
    assert calls == ['maintenance', 'maintenance']
    assert node.journal.pending_cleanups() == []
