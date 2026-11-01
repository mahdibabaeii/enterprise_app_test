"""Determinism replay run by make verify (plan T04, control REP-01, criterion A1).

Generates a seeded batch of synthetic transactions: velocity bursts, night
purchases at new merchants, foreign and large amounts, and hard-rule hits.
Appends the batch through the real request model and store into two fresh
databases with the same fixed clock. Requires:

- identical head hashes, so every record is byte-for-byte the same
- every replay to report match, rules_match and features_match (or null
  features_match for failed-safe records)
- every outcome to appear, so the batch exercises all paths

Exits 1 on any failure. Never touches data/demo.db.
"""

import random
import sqlite3
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.models import TransactionRequest
from app.rules import active_rule_set, load_rule_sets
from app.store import Store

SEED = 20261101
COUNT = 200
CARD_REF_KEY = "replay-check-key"
FIXED_CLOCK = datetime(2026, 11, 1, tzinfo=UTC)
OFFSETS = [timezone(timedelta(hours=h)) for h in (11, 10, 0, -5, 8)]


def luhn_complete(prefix: str) -> str:
    """Append the Luhn check digit to a synthetic card number prefix."""
    total = 0
    for position, char in enumerate(reversed(prefix)):
        digit = int(char)
        if position % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return prefix + str((10 - total % 10) % 10)


def synthetic_batch(seed: int = SEED, count: int = COUNT) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    cards = [luhn_complete(f"400000{n:09d}") for n in range(12)]
    start = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
    batch: list[dict[str, Any]] = []
    while len(batch) < count:
        offset = rng.choice(OFFSETS)
        moment = (start + timedelta(minutes=rng.randrange(0, 3 * 24 * 60))).astimezone(offset)
        card = rng.choice(cards)
        burst = rng.random() < 0.15
        for step in range(rng.randint(6, 8) if burst else 1):
            n = len(batch) + 1
            batch.append(
                {
                    "transaction_id": f"RC-{n:04d}",
                    "card_number": card,
                    "card_status": "reported_stolen" if rng.random() < 0.04 else "active",
                    "amount_minor": rng.choice([1_500, 25_000, 99_999, 100_000, 350_000]),
                    "currency": "AUD",
                    "merchant_id": rng.choice(["M-0001", "M-0002", "M-0003", "M-BLOCK-001"])
                    if rng.random() < 0.6
                    else f"M-{rng.randrange(10_000):04d}",
                    "merchant_country": rng.choice(["AU", "AU", "AU", "GB", "US", "NZ"]),
                    "timestamp": (moment + timedelta(seconds=8 * step)).isoformat(),
                }
            )
    return batch[:count]


def run(path: Path, batch: list[dict[str, Any]]) -> Store:
    rule_set = active_rule_set(load_rule_sets())
    chain = Store(path, clock=lambda: FIXED_CLOCK)
    for body in batch:
        record = TransactionRequest.model_validate(body).to_record(CARD_REF_KEY)
        chain.append(record, rule_set)
    return chain


def head_hash(path: Path) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[
            0
        ]
    finally:
        connection.close()


def main() -> int:
    batch = synthetic_batch()
    rule_sets = load_rule_sets()
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as directory:
        first, second = Path(directory) / "first.db", Path(directory) / "second.db"
        chain = run(first, batch)
        run(second, batch)
        if head_hash(first) != head_hash(second):
            failures.append("head hashes differ between two runs of the same batch")
        outcomes: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for record_id in range(1, len(batch) + 1):
            result = chain.replay(record_id, rule_sets)
            assert result is not None
            outcomes[result.stored.outcome] += 1
            reasons.update(reason.code for reason in result.stored.reasons)
            if not (result.match and result.rules_match and result.features_match is not False):
                failures.append(f"record {record_id} does not replay: {result.model_dump()}")
        if not chain.verify().intact:
            failures.append("chain does not verify")
    for outcome in ("approve", "step_up", "decline"):
        if not outcomes[outcome]:
            failures.append(f"batch produced no {outcome} decision")
    for code in ("HIGH_VELOCITY", "NEW_MERCHANT_AT_NIGHT", "CARD_REPORTED_STOLEN"):
        if not reasons[code]:
            failures.append(f"batch never fired {code}")
    print(f"replay check: {len(batch)} transactions, outcomes {dict(sorted(outcomes.items()))}")
    for failure in failures:
        print(f"replay check FAILED: {failure}")
    if failures:
        return 1
    print("replay check passed: identical head hashes, every replay matches")
    return 0


if __name__ == "__main__":
    sys.exit(main())
