"""No ambient cookies: audience-bound, short-lived bearer credentials."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

from agentflow.common import DomainError


@dataclass(frozen=True)
class Principal:
    audience: str
    scopes: frozenset[str]
    subject: str
    expires_at: float


class TokenAuthority:
    def __init__(self, bootstrap_seconds: int = 300, owner_seconds: int = 28800,
                 clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.owner_seconds = owner_seconds
        self.bootstrap_seconds = bootstrap_seconds
        self._tokens: dict[str, Principal] = {}
        self._browser_sessions: dict[str, set[str]] = {}
        self.new_bootstrap()

    def new_bootstrap(self) -> str:
        self.bootstrap_code = secrets.token_urlsafe(32)
        self._bootstrap_hash = self._hash(self.bootstrap_code)
        self._bootstrap_expires = self._clock() + self.bootstrap_seconds
        return self.bootstrap_code

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def exchange(self, code: str) -> str:
        if (self._clock() >= self._bootstrap_expires or not self._bootstrap_hash
                or not hmac.compare_digest(self._hash(code), self._bootstrap_hash)):
            raise DomainError("invalid_bootstrap", "Bootstrap code is invalid or expired", 401)
        self._bootstrap_hash = ""
        self.bootstrap_code = ""
        return self.issue("agentflow_owner", {"owner:*"}, "owner", self.owner_seconds)

    def issue(self, audience: str, scopes: set[str], subject: str, ttl: int) -> str:
        self._prune()
        token = secrets.token_urlsafe(48)
        self._tokens[self._hash(token)] = Principal(audience, frozenset(scopes), subject, self._clock() + ttl)
        return token

    def _prune(self):
        now = self._clock()
        for digest, principal in list(self._tokens.items()):
            if principal.expires_at <= now:
                self._tokens.pop(digest, None)
        for ticket, owners in list(self._browser_sessions.items()):
            if ticket not in self._tokens:
                self._browser_sessions.pop(ticket, None)
            else:
                owners.intersection_update(self._tokens)

    def require(self, token: str, audience: str, scope: str, subject: str | None = None) -> Principal:
        digest = self._hash(token)
        principal = self._tokens.get(digest)
        if not principal or principal.expires_at <= self._clock():
            self._tokens.pop(digest, None)
            raise DomainError("unauthorized", "A valid bearer credential is required", 401)
        if (principal.audience != audience or (scope not in principal.scopes
                and not (audience == "agentflow_owner" and "owner:*" in principal.scopes))
                or (subject is not None and principal.subject != subject)):
            raise DomainError("forbidden", "Credential audience, scope or subject does not match", 403)
        return principal

    def revoke(self, token: str) -> None:
        digest = self._hash(token)
        self._tokens.pop(digest, None)
        for ticket, owners in list(self._browser_sessions.items()):
            if digest == ticket or digest in owners:
                self._tokens.pop(ticket, None)
                for owner in owners:
                    self._tokens.pop(owner, None)
                self._browser_sessions.pop(ticket, None)

    def revoke_subject(self, subject: str) -> None:
        self._tokens = {k: p for k, p in self._tokens.items() if p.subject != subject}
        self._browser_sessions = {key: value for key, value in self._browser_sessions.items() if key in self._tokens}

    def browser_session(self, owner_token: str) -> str:
        """A per-tab continuation ticket cannot authorize management commands."""
        self.require(owner_token, 'agentflow_owner', 'owner:*', 'owner')
        ticket = self.issue('agentflow_browser_session', {'session:renew'}, 'owner', 7 * 86400)
        self._browser_sessions[self._hash(ticket)] = {self._hash(owner_token)}
        return ticket

    def resume_browser_session(self, ticket: str) -> str:
        self.require(ticket, 'agentflow_browser_session', 'session:renew')
        digest = self._hash(ticket)
        owners = self._browser_sessions.get(digest)
        if owners is None:
            raise DomainError('unauthorized', 'Browser continuation is unavailable', 401)
        # Tokens are process-local. A controller restart requires its local launch
        # link; no ticket grants authority to a different controller process.
        principal = self._tokens[digest]
        self._tokens[digest] = Principal(principal.audience, principal.scopes, principal.subject,
                                         self._clock() + 7 * 86400)
        token = self.issue('agentflow_owner', {'owner:*'}, 'owner', self.owner_seconds)
        owners.add(self._hash(token))
        return token
