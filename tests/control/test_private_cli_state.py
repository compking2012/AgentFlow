"""Discovery and uncertain submission records fail closed without losing recovery."""
import json
import os
import stat
import subprocess
import sys
from uuid import UUID

import psutil
import pytest

from agentflow.common import DomainError, canonical_digest
from agentflow.control.instance import active_data_dir, clear_instance, instance_path, publish_instance
from agentflow.control.submissions import Submission


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    return home


def private_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def live_record(data, **updates):
    return {'pid': os.getpid(), 'created_at': psutil.Process().create_time(), 'data_dir': str(data.resolve()), **updates}


def intent(data, **updates):
    task = {'goal': 'Build a persistent reading list', 'name': None, 'output': None}
    payload = {'name': 'Reading list', 'goal': task['goal'], 'output_directory': None,
               'target': 'web', 'review_mode': 'auto', 'max_model_requests': 200, **updates}
    return Submission(data, task, payload)


def test_instance_discovery_is_private_and_requires_exact_process_lifetime(tmp_path):
    assert active_data_dir() is None
    assert not instance_path().parent.exists()
    data = tmp_path / 'data'
    publish_instance(data)
    path = instance_path()
    assert active_data_dir() == data.resolve()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    publish_instance(data)
    assert clear_instance() and not path.exists()
    assert not clear_instance()
    record = live_record(data)
    record['created_at'] -= 1
    private_json(path, record)
    assert active_data_dir() is None and not clear_instance()
    assert path.exists(), 'A reused PID must not remove another process lifetime record'
    publish_instance(data)
    assert active_data_dir() == data.resolve()


@pytest.mark.parametrize('record', [
    [], None, {'pid': 1}, {'pid': True, 'created_at': 1, 'data_dir': '/tmp'},
    {'pid': 1, 'created_at': float('nan'), 'data_dir': '/tmp'},
    {'pid': 1, 'created_at': float('inf'), 'data_dir': '/tmp'},
    {'pid': 1, 'created_at': 10 ** 1000, 'data_dir': '/tmp'},
    {'pid': 10 ** 1000, 'created_at': 1, 'data_dir': '/tmp'},
    {'pid': 1, 'created_at': 1, 'data_dir': 'relative'},
    {'pid': 1, 'created_at': 'private-sensitive-value', 'data_dir': '/tmp'},
])
def test_malformed_instance_record_is_preserved_without_disclosing_values(record, tmp_path):
    path = instance_path()
    private_json(path, record)
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        active_data_dir()
    assert 'private-sensitive-value' not in str(error.value)
    assert not clear_instance()
    with pytest.raises(DomainError):
        publish_instance(tmp_path / 'data')
    assert path.read_bytes() == before


@pytest.mark.parametrize('kind', ['public_file', 'public_directory', 'linked_file', 'linked_directory', 'hardlink', 'fifo'])
def test_unsafe_instance_records_never_get_followed_or_replaced(kind, tmp_path):
    path = instance_path()
    private_json(path, live_record(tmp_path / 'data'))
    if kind == 'public_file':
        path.chmod(0o644)
    elif kind == 'public_directory':
        path.parent.chmod(0o755)
    elif kind == 'linked_file':
        target = tmp_path / 'actual.json'
        path.rename(target)
        path.symlink_to(target)
    elif kind == 'linked_directory':
        target = tmp_path / 'actual-directory'
        path.parent.rename(target)
        path.parent.symlink_to(target)
    elif kind == 'hardlink':
        os.link(path, tmp_path / 'second-link')
    elif kind == 'fifo':
        path.unlink()
        os.mkfifo(path, mode=0o600)
        result = subprocess.run([sys.executable, '-c',
            'from agentflow.control.instance import active_data_dir\n'
            'from agentflow.common import DomainError\n'
            'try: active_data_dir()\n'
            'except DomainError: print("rejected")'], capture_output=True, text=True, timeout=3)
        assert result.returncode == 0 and result.stdout.strip() == 'rejected'
    with pytest.raises(DomainError):
        active_data_dir()
    with pytest.raises(DomainError):
        publish_instance(tmp_path / 'new-data')
    assert not clear_instance()


def test_live_instance_cannot_be_replaced_by_different_instance_or_directory(tmp_path):
    path = instance_path()
    private_json(path, live_record(tmp_path / 'old'))
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        publish_instance(tmp_path / 'different')
    assert error.value.code == 'active_instance_exists'
    assert path.read_bytes() == before


def test_fixed_temporary_symlink_cannot_redirect_private_writes(tmp_path):
    path = instance_path()
    path.parent.mkdir(parents=True, mode=0o700)
    target = tmp_path / 'keep-me'
    target.write_text('original')
    path.with_suffix('.json.tmp').symlink_to(target)
    publish_instance(tmp_path / 'data')
    with intent(tmp_path / 'data') as submission:
        submission.acknowledged()
    pending = intent(tmp_path / 'data')
    pending.path.with_suffix('.json.tmp').symlink_to(target)
    with pending:
        assert target.read_text() == 'original'
    assert target.read_text() == 'original'


def test_uncertain_submission_survives_restart_with_original_key_and_config_values(tmp_path):
    data = tmp_path / 'data'
    with intent(data) as first:
        path, original_key, original_payload = first.path, first.key, first.payload.copy()
        assert UUID(original_key).version == 4
        assert path.parent == instance_path().parent / 'cli_submissions'
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with intent(data, target='api', review_mode='milestones', max_model_requests=10) as replay:
        assert replay.key == original_key and replay.payload == original_payload
        replay.acknowledged()
        replay.acknowledged()
    assert not path.exists()
    with intent(data, target='api') as fresh:
        assert fresh.key != original_key and fresh.payload['target'] == 'api'


def test_data_directory_change_cannot_replay_uncertain_submit_into_a_different_database(tmp_path):
    with intent(tmp_path / 'original') as first:
        path, key = first.path, first.key
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        with intent(tmp_path / 'new'):
            pytest.fail('A new controller must not receive the unresolved request')
    assert error.value.code == 'submission_controller_changed'
    assert path.read_bytes() == before
    with intent(tmp_path / 'original') as replay:
        assert replay.key == key


@pytest.mark.parametrize('change', [
    {'key': ''}, {'key': 'not-a-uuid'}, {'key': '00000000-0000-1000-8000-000000000000'},
    {'key': 1}, {'key': None}, {'version': True}, {'payload': []},
    {'payload_fingerprint': 'sha256:bad'}, {'input_fingerprint': 'sha256:bad'},
    {'origin_data_dir': 'relative'}, {'extra': 'private-sensitive-value'},
])
def test_malformed_submission_record_cannot_be_replayed_or_overwritten(tmp_path, change):
    data = tmp_path / 'data'
    with intent(data) as first:
        path = first.path
    value = json.loads(path.read_text())
    value.update(change)
    private_json(path, value)
    before = path.read_bytes()
    with pytest.raises(DomainError) as error:
        with intent(data):
            pass
    assert error.value.code == 'invalid_cli_submission'
    assert 'private-sensitive-value' not in str(error.value)
    assert path.read_bytes() == before


def test_replay_checks_product_schema_and_original_goal_even_if_payload_digest_is_rewritten(tmp_path):
    data = tmp_path / 'data'
    with intent(data) as first:
        path = first.path
    record = json.loads(path.read_text())
    original = record['payload'].copy()
    for changed in ({**original, 'unexpected': True}, {**original, 'goal': 'Run a different task'}):
        record['payload'] = changed
        record['payload_fingerprint'] = canonical_digest(changed)
        private_json(path, record)
        with pytest.raises(DomainError) as error:
            with intent(data):
                pass
        assert error.value.code == 'invalid_cli_submission'


@pytest.mark.parametrize('part,kind', [
    ('record', 'public'), ('record', 'symlink'), ('record', 'hardlink'), ('record', 'fifo'),
    ('lock', 'public'), ('lock', 'symlink'), ('lock', 'fifo'), ('root', 'public'), ('root', 'symlink'),
])
def test_submission_private_files_and_lock_are_verified_before_replay(tmp_path, part, kind):
    data = tmp_path / 'data'
    with intent(data) as first:
        path = first.path
    target = path if part == 'record' else path.with_suffix('.lock') if part == 'lock' else path.parent
    if kind == 'public':
        target.chmod(0o755 if part == 'root' else 0o644)
    elif kind == 'symlink':
        other = tmp_path / 'target'
        target.rename(other)
        target.symlink_to(other)
    elif kind == 'hardlink':
        os.link(target, tmp_path / 'hardlink')
    elif kind == 'fifo':
        target.unlink()
        os.mkfifo(target, mode=0o600)
    with pytest.raises(DomainError) as error:
        with intent(data):
            pass
    assert error.value.code == 'unsafe_cli_state'


def test_concurrent_processes_cannot_submit_same_intent_and_failed_read_releases_lock(tmp_path):
    data = tmp_path / 'data'
    with intent(data) as first:
        script = '''
import json, sys
from pathlib import Path
from agentflow.common import DomainError
from agentflow.control.submissions import Submission
try:
    with Submission(Path(sys.argv[1]), json.loads(sys.argv[2]), json.loads(sys.argv[3])):
        print('unexpectedly acquired')
except DomainError as error:
    print(error.code)
'''
        task = {'goal': 'Build a persistent reading list', 'name': None, 'output': None}
        result = subprocess.run([sys.executable, '-c', script, str(data), json.dumps(task), json.dumps(first.payload)],
                                capture_output=True, text=True, timeout=5)
        assert result.returncode == 0 and result.stdout.strip() == 'submission_in_progress'
        path, good = first.path, first.path.read_bytes()
    path.write_text('invalid-json')
    with pytest.raises(DomainError):
        with intent(data):
            pass
    path.write_bytes(good)
    with intent(data) as recovered:
        recovered.acknowledged()


def test_acknowledgement_cannot_delete_replaced_or_changed_recovery_record(tmp_path):
    with intent(tmp_path / 'data') as submission:
        value = json.loads(submission.path.read_text())
        value['payload']['max_model_requests'] = 10
        value['payload_fingerprint'] = canonical_digest(value['payload'])
        private_json(submission.path, value)
        with pytest.raises(DomainError) as error:
            submission.acknowledged()
        assert error.value.code == 'submission_record_changed'
        assert submission.path.exists()
    with pytest.raises(DomainError) as error:
        submission.acknowledged()
    assert error.value.code == 'submission_not_locked'


def test_duplicate_fields_and_oversized_state_records_are_rejected(tmp_path):
    with intent(tmp_path / 'data') as submission:
        path = submission.path
    for content in ('{"key":"a","key":"b"}', '{"value":"' + 'x' * (128 * 1024) + '"}'):
        path.write_text(content)
        with pytest.raises(DomainError) as error:
            with intent(tmp_path / 'data'):
                pass
        assert error.value.code == 'unsafe_cli_state'
