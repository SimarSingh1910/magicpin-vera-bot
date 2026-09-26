"""
In-memory, idempotent context store.

Keyed by (scope, context_id). A push with version <= current is a no-op that
reports the current version (spec: idempotent by (context_id, version), higher
version replaces atomically). Single dict + no async gaps -> no race between
"read current version" and "write new version" within one process.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

VALID_SCOPES = {"category", "merchant", "customer", "trigger"}


class ContextStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str], dict[str, Any]] = {}

    def push(self, scope: str, context_id: str, version: int, payload: dict) -> tuple[bool, Optional[int]]:
        """Returns (accepted, current_version_if_rejected)."""
        key = (scope, context_id)
        with self._lock:
            cur = self._data.get(key)
            if cur is not None and cur["version"] >= version:
                return False, cur["version"]
            self._data[key] = {"version": version, "payload": payload}
            return True, None

    def get(self, scope: str, context_id: str) -> Optional[dict]:
        entry = self._data.get((scope, context_id))
        return entry["payload"] if entry else None

    def get_version(self, scope: str, context_id: str) -> Optional[int]:
        entry = self._data.get((scope, context_id))
        return entry["version"] if entry else None

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in VALID_SCOPES}
        for (scope, _cid) in self._data.keys():
            out[scope] = out.get(scope, 0) + 1
        return out

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
