"""Tamper simulation run by make verify (plan T10, control AUD-01).

For each record k of a throwaway chain, flip the outcome stored in record k
directly in SQLite, as someone with database access could, and require verify
to name record k as the first broken record. Exits 1 if any edit goes
undetected. Never touches data/demo.db.
"""

import json
import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.chain import canonical_json
from app.rules import active_rule_set, load_rule_sets
from app.store import RecordInput, Store

RECORDS = 5
FLIPPED = {"approve": "decline", "step_up": "approve", "decline": "approve"}


def build_chain(path: Path) -> Store:
    rule_set = active_rule_set(load_rule_sets())
    chain = Store(path, clock=lambda: datetime(2026, 11, 1, tzinfo=UTC))
    start = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)
    for n in range(1, RECORDS + 1):
        record = RecordInput(
            transaction_id=f"SIM-{n:04d}",
            card_masked="****1111",
            card_ref=f"{n:032x}",
            card_status="active",
            amount_minor=60_000 * n,
            currency="AUD",
            merchant_id=f"M-SIM-{n}",
            merchant_country="AU" if n % 2 else "GB",
            timestamp=start + timedelta(minutes=n),
        )
        chain.append(record, rule_set)
    return chain


def flip_outcome(path: Path, record_id: int) -> None:
    with sqlite3.connect(path) as connection:
        (text,) = connection.execute(
            "SELECT payload FROM decisions WHERE id = ?", (record_id,)
        ).fetchone()
        payload = json.loads(text)
        payload["outcome"] = FLIPPED[payload["outcome"]]
        connection.execute(
            "UPDATE decisions SET payload = ? WHERE id = ?",
            (canonical_json(payload).decode(), record_id),
        )
    connection.close()


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory() as directory:
        for k in range(1, RECORDS + 1):
            path = Path(directory) / f"tamper-{k}.db"
            chain = build_chain(path)
            before = chain.verify()
            flip_outcome(path, k)
            after = chain.verify()
            detected = before.intact and not after.intact and after.first_broken_id == k
            failures += not detected
            print(
                f"tamper record {k}: {'detected' if detected else 'NOT DETECTED'}"
                f" (first_broken_id={after.first_broken_id}, reason={after.reason!r})"
            )
    if failures:
        print(f"tamper simulation FAILED: {failures} of {RECORDS} edits not located")
        return 1
    print(f"tamper simulation passed: {RECORDS} of {RECORDS} edits located")
    return 0


if __name__ == "__main__":
    sys.exit(main())
