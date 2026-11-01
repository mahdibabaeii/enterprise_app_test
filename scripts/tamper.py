"""Corrupt one record in the demo database (make tamper).

Plays someone with direct database access: drops any triggers on the decisions
table, flips one record's outcome inside its payload without touching its hash,
and restores the triggers (plan 2.4). GET /audit/verify then names that record.

Only data/demo.db can be tampered with; any other path is refused. A record that
is already altered is refused too, because flipping it again could restore it.

Usage: python -m scripts.tamper [record_id]   (default: record 3)
"""

import json
import sqlite3
import sys
from pathlib import Path

from app.chain import sha256_hex
from app.main import DEFAULT_DB

DEMO_DB = DEFAULT_DB
DEFAULT_RECORD = 3
FLIPPED = {"approve": "decline", "step_up": "approve", "decline": "approve"}


class TamperRefused(Exception):
    """The target is not the demo database, or the record is already altered."""


def tamper(path: Path, record_id: int) -> tuple[str, str]:
    """Flip the outcome of `record_id`. Return (old outcome, new outcome)."""
    if path.resolve() != DEMO_DB.resolve():
        raise TamperRefused(f"refusing to tamper with {path}: only {DEMO_DB} may be altered")
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload, hash FROM decisions WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"record {record_id} does not exist in {path}")
        # A second flip could write back the original bytes and hide the first tamper.
        if sha256_hex(row[0].encode()) != row[1]:
            raise TamperRefused(
                f"record {record_id} is already altered; run make demo to reset the demo database"
            )
        triggers = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'decisions'"
        ).fetchall()
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        payload = json.loads(row[0])
        old = payload["outcome"]
        payload["outcome"] = FLIPPED[old]
        edited = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        connection.execute("UPDATE decisions SET payload = ? WHERE id = ?", (edited, record_id))
        for _, sql in triggers:
            connection.execute(sql)
        connection.execute("COMMIT")
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
    return old, payload["outcome"]


def main(argv: list[str]) -> int:
    if not DEMO_DB.exists():
        print(f"no demo database at {DEMO_DB}; run make demo first", file=sys.stderr)
        return 1
    try:
        record_id = int(argv[0]) if argv else DEFAULT_RECORD
        old, new = tamper(DEMO_DB, record_id)
    except (ValueError, LookupError, TamperRefused) as error:
        print(f"tamper failed: {error}", file=sys.stderr)
        return 1
    print(
        f"tampered record {record_id} in {DEMO_DB}: outcome {old} -> {new}, hash left unchanged."
        " Verify the chain as auditor to see it named."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
