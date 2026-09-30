"""Shared wire-safe errors, canonical identities, and timestamps."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any


class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 409, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

