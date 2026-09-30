import os
import stat

import pytest

from agentflow.common import DomainError
from agentflow.models.secrets import LocalSecretStore


def test_local_secret_is_private_immutable_and_environment_references_stay_explicit(tmp_path, monkeypatch):
    store = LocalSecretStore(tmp_path)
    reference = store.put('one', 'fixture-api-key')
    assert store.read(reference) == 'fixture-api-key'
    assert store.put('one', 'fixture-api-key') == reference
    assert stat.S_IMODE((tmp_path / 'secrets/one').stat().st_mode) == 0o600
    with pytest.raises(DomainError, match='different credential'):
        store.put('one', 'different-key')
    monkeypatch.setenv('AGENTFLOW_UNIT_TEST_KEY', 'environment-fixture')
    assert store.read('env:AGENTFLOW_UNIT_TEST_KEY') == 'environment-fixture'
    with pytest.raises(DomainError):
        store.read('local:../elsewhere')


def test_linked_or_public_secret_is_rejected(tmp_path):
    store = LocalSecretStore(tmp_path)
    store.put('real', 'fixture-only')
    (tmp_path / 'secrets/linked').symlink_to('real')
    with pytest.raises(DomainError):
        store.read('local:linked')
    os.chmod(tmp_path / 'secrets/real', 0o644)
    with pytest.raises(DomainError, match='permissions'):
        store.read('local:real')
