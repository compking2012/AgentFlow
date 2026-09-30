from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class CodexEventNormalizer:
    """Preserves unfamiliar events as unverified evidence, not successful completion."""

    KNOWN = {
        "thread.started", "turn.started", "turn.completed", "turn.failed", "error",
        "item.started", "item.updated", "item.completed", "thread.name_updated",
    }

    def read(self, path: Path, maximum: int = 16 * 1024 * 1024) -> dict[str, Any]:
        if not path.is_file():
            return {"valid": False, "errors": ["events_missing"], "events": [], "usage": None, "final_text": None}
        if path.stat().st_size > maximum:
            return {"valid": False, "errors": ["event_log_too_large"], "events": [], "usage": None, "final_text": None}
        return self.parse_bytes(path.read_bytes(), maximum)

    def parse_bytes(self, raw: bytes, maximum: int = 16 * 1024 * 1024) -> dict[str, Any]:
        """Parse an already captured log without reopening a mutable path."""
        if len(raw) > maximum:
            return {"valid": False, "errors": ["event_log_too_large"], "events": [], "usage": None, "final_text": None}
        errors = []
        events = []
        usage = None
        final_text = None
        completed = False
        for number, line in enumerate(raw.decode('utf-8', errors='replace').splitlines(), start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
                if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                    raise ValueError("event object/type missing")
            except (ValueError, json.JSONDecodeError):
                errors.append(f"invalid_event_line:{number}")
                continue
            kind = value["type"]
            if kind not in self.KNOWN:
                errors.append(f"unrecognized_event:{kind}")
            if kind in {"turn.failed", "error"}:
                errors.append(kind)
            if kind == "turn.completed":
                completed = True
                usage = value.get("usage")
            item = value.get("item", {})
            if kind == "item.completed" and isinstance(item, dict) and item.get("type") == "agent_message":
                final_text = item.get("text")
            events.append({"sequence": number, "type": kind, "raw": value})
        if not completed:
            errors.append("terminal_event_missing")
        return {"valid": completed and not errors, "errors": errors, "events": events,
                "usage": usage, "final_text": final_text}
