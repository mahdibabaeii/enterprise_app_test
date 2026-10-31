"""Tamper simulation run by make verify (plan T10, control AUD-01).

On a throwaway chain, alter record k directly in SQLite, as someone with
database access could, and require verify to name the expected record:

- edit: flip record k's outcome, leaving its hash; verify must name k
- edit and rehash: flip it and recompute its hash; verify must name k + 1
- delete: remove record k; verify must name k + 1

The last two are not tried on the latest record, because nothing after it can
reveal them (plan R16). Exits 1 if any alteration is not located. Never
touches data/demo.db.
"""

import hashlib
import json
import sqlite3
import sys
import tempfile
from collections.abc import Callable
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


def flip_outcome(connection: sqlite3.Connection, record_id: int, *, rehash: bool) -> None:
    (text,) = connection.execute(
        "SELECT payload FROM decisions WHERE id = ?", (record_id,)
    ).fetchone()
    payload = json.loads(text)
    payload["outcome"] = FLIPPED[payload["outcome"]]
    data = canonical_json(payload)
    connection.execute("UPDATE decisions SET payload = ? WHERE id = ?", (data.decode(), record_id))
    if rehash:
        connection.execute(
            "UPDATE decisions SET hash = ? WHERE id = ?",
            (hashlib.sha256(data).hexdigest(), record_id),
        )


def delete(connection: sqlite3.Connection, record_id: int) -> None:
    connection.execute("DELETE FROM decisions WHERE id = ?", (record_id,))


# name -> (alteration, offset from k of the record verify must name)
MUTATIONS: dict[str, tuple[Callable[[sqlite3.Connection, int], None], int]] = {
    "edit": (lambda c, k: flip_outcome(c, k, rehash=False), 0),
    "edit and rehash": (lambda c, k: flip_outcome(c, k, rehash=True), 1),
    "delete": (delete, 1),
}


def main() -> int:
    tried = failures = 0
    with tempfile.TemporaryDirectory() as directory:
        for name, (alter, offset) in MUTATIONS.items():
            for k in range(1, RECORDS + 1 - offset):
                path = Path(directory) / f"{name.replace(' ', '-')}-{k}.db"
                chain = build_chain(path)
                before = chain.verify()
                connection = sqlite3.connect(path, isolation_level=None)
                alter(connection, k)
                connection.close()
                after = chain.verify()
                expected = k + offset
                located = before.intact and not after.intact and after.first_broken_id == expected
                tried += 1
                failures += not located
                print(
                    f"{name} record {k}: {'located' if located else 'NOT LOCATED'}"
                    f" (expected {expected}, first_broken_id={after.first_broken_id},"
                    f" reason={after.reason!r})"
                )
    if failures:
        print(f"tamper simulation FAILED: {failures} of {tried} alterations not located")
        return 1
    print(f"tamper simulation passed: {tried} of {tried} alterations located")
    return 0


if __name__ == "__main__":
    sys.exit(main())
