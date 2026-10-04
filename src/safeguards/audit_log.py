# Corey Mathie, 2026
"""
Hash-chained append-only audit log.

Each entry carries the SHA-256 of the previous entry's payload + its own
canonicalized JSON. Tampering with any entry invalidates every subsequent
hash. Readable with `verify_chain()`.

Why this matters: regulated-industry voice agents (healthcare, legal,
fintech) need an audit trail that an outside reviewer can trust. A plain
log file doesn't give you that — it's trivially editable. A hash chain
makes tampering detectable, even if the attacker has write access to the
log file itself.

For the real deal: forward each entry to a WORM-backed store (S3 Object
Lock, Azure Blob immutable, GCS Bucket Lock). This file-based chain is the
local half of that story.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64


def _canonical(obj: dict[str, Any]) -> str:
    """Deterministic JSON for hashing — stable key order, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


@dataclass
class AuditEntry:
    ts: str
    event: str
    payload: dict[str, Any]
    prev_hash: str
    entry_hash: str = field(init=False)

    def __post_init__(self) -> None:
        core = {"ts": self.ts, "event": self.event, "payload": self.payload, "prev_hash": self.prev_hash}
        self.entry_hash = _sha256(_canonical(core))

    def to_jsonl(self) -> str:
        return json.dumps(
            {
                "ts": self.ts,
                "event": self.event,
                "payload": self.payload,
                "prev_hash": self.prev_hash,
                "entry_hash": self.entry_hash,
            },
            separators=(",", ":"),
        )


class AuditLog:
    """Thread-safe (via the OS: O_APPEND is atomic for small writes on POSIX)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_hash = self._tail_hash()

    def _tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)["entry_hash"]
        return last

    def append(self, event: str, payload: dict[str, Any]) -> AuditEntry:
        ts = datetime.now(UTC).isoformat()
        entry = AuditEntry(ts=ts, event=event, payload=payload, prev_hash=self._last_hash)
        with self.path.open("a") as f:
            f.write(entry.to_jsonl() + "\n")
        self._last_hash = entry.entry_hash
        return entry

    def verify_chain(self) -> tuple[bool, int | None]:
        """Return (ok, bad_line_number). Walk the chain and recompute hashes."""
        if not self.path.exists():
            return True, None
        prev = GENESIS_HASH
        with self.path.open() as f:
            for i, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                core = {
                    "ts": row["ts"],
                    "event": row["event"],
                    "payload": row["payload"],
                    "prev_hash": row["prev_hash"],
                }
                if row["prev_hash"] != prev or _sha256(_canonical(core)) != row["entry_hash"]:
                    return False, i
                prev = row["entry_hash"]
        return True, None
