import pytest

from agentflow.common import DomainError
from agentflow.control.security import TokenAuthority
from agentflow.settings import Settings


def test_bootstrap_is_single_use_and_tokens_are_audience_bound():
    clock = [100.0]
    authority = TokenAuthority(clock=lambda: clock[0])
    code = authority.bootstrap_code
    owner = authority.exchange(code)
    assert authority.require(owner, "agentflow_owner", "owner:approve").subject == "owner"
    with pytest.raises(DomainError, match="invalid or expired"):
        authority.exchange(code)
    task = authority.issue("agentflow_attempt", {"llm:chat"}, "attempt-a", 60)
    for token, audience, scope, subject in [
        (task, "agentflow_owner", "owner:approve", None),
        (owner, "agentflow_attempt", "llm:chat", None),
        (task, "agentflow_attempt", "llm:responses", None),
        (task, "agentflow_attempt", "llm:chat", "attempt-b"),
    ]:
        with pytest.raises(DomainError) as failure:
            authority.require(token, audience, scope, subject)
        assert failure.value.status == 403
    clock[0] += 61
    with pytest.raises(DomainError) as failure:
        authority.require(task, "agentflow_attempt", "llm:chat")
    assert failure.value.status == 401


def test_revocation_and_bootstrap_expiration():
    clock = [0.0]
    authority = TokenAuthority(clock=lambda: clock[0])
    clock[0] = 301
    with pytest.raises(DomainError):
        authority.exchange(authority.bootstrap_code)
    token = authority.issue("agentflow_attempt", {"llm:chat"}, "attempt", 60)
    authority.revoke_subject("attempt")
    with pytest.raises(DomainError):
        authority.require(token, "agentflow_attempt", "llm:chat")


def test_tab_continuation_has_no_management_authority_and_survives_owner_expiry():
    clock = [0.0]
    authority = TokenAuthority(owner_seconds=10, clock=lambda: clock[0])
    owner = authority.exchange(authority.bootstrap_code)
    ticket = authority.browser_session(owner)
    with pytest.raises(DomainError) as forbidden:
        authority.require(ticket, 'agentflow_owner', 'owner:control')
    assert forbidden.value.status == 403
    with pytest.raises(DomainError):
        authority.resume_browser_session(owner)
    clock[0] = 11
    with pytest.raises(DomainError):
        authority.require(owner, 'agentflow_owner', 'owner:control')
    renewed = authority.resume_browser_session(ticket)
    assert authority.require(renewed, 'agentflow_owner', 'owner:control').subject == 'owner'
    authority.revoke(renewed)
    with pytest.raises(DomainError):
        authority.resume_browser_session(ticket)


def test_tab_tickets_expire_and_controller_or_subject_revocation_stops_continuation():
    clock = [0.0]
    authority = TokenAuthority(owner_seconds=10, clock=lambda: clock[0])
    owner = authority.exchange(authority.bootstrap_code)
    ticket = authority.browser_session(owner)
    with pytest.raises(DomainError):
        TokenAuthority().resume_browser_session(ticket)
    clock[0] = 7 * 86400 + 1
    with pytest.raises(DomainError):
        authority.resume_browser_session(ticket)
    owner = authority.issue('agentflow_owner', {'owner:*'}, 'owner', 10)
    ticket = authority.browser_session(owner)
    authority.revoke_subject('owner')
    with pytest.raises(DomainError):
        authority.resume_browser_session(ticket)


def test_limited_or_other_owner_subject_cannot_upgrade_through_browser_continuation():
    authority = TokenAuthority()
    for scopes, subject in [({'owner:control'}, 'owner'), ({'owner:*'}, 'different-owner')]:
        token = authority.issue('agentflow_owner', scopes, subject, 60)
        with pytest.raises(DomainError) as error:
            authority.browser_session(token)
        assert error.value.status == 403


def test_repeated_browser_continuation_cleans_expired_owner_credentials():
    clock = [0.0]
    authority = TokenAuthority(owner_seconds=10, clock=lambda: clock[0])
    ticket = authority.browser_session(authority.exchange(authority.bootstrap_code))
    for _ in range(200):
        clock[0] += 11
        current = authority.resume_browser_session(ticket)
    assert len(authority._tokens) == 2
    assert len(authority._browser_sessions[authority._hash(ticket)]) == 1
    authority.revoke(current)
    with pytest.raises(DomainError):
        authority.resume_browser_session(ticket)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "8.8.8.8", "localhost", "::"])
def test_owner_never_listens_outside_explicit_loopback(host):
    with pytest.raises(ValueError):
        Settings(host=host)


def test_executor_requires_private_interface_and_tls():
    with pytest.raises(ValueError):
        Settings(executor_host="192.168.1.10")
    with pytest.raises(ValueError):
        Settings(executor_host="0.0.0.0")
