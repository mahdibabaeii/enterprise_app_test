"""Seed the demo database with synthetic decisions (make start, make demo).

Replaces the database file and appends each sample through the real request
model, engine and store (plan R28). No record is updated or deleted in place.
All card numbers are synthetic test numbers.

Usage: python -m scripts.seed [path]   (default: data/demo.db)
"""

import sys
from pathlib import Path
from typing import Any

from app.main import DEFAULT_DB, DEMO_CARD_REF_KEY
from app.models import TransactionRequest
from app.rules import active_rule_set, load_rule_sets
from app.store import Store

CARD_A = "4539578763621486"
CARD_B = "4111111111111111"
CARD_C = "5555555555554444"


def _sample(transaction_id: str, card: str, **fields: Any) -> dict[str, Any]:
    return {
        "transaction_id": transaction_id,
        "card_number": card,
        "card_status": "active",
        "currency": "AUD",
        **fields,
    }


# Expected v1 outcomes are noted for the demo walkthrough; tests check them.
SAMPLES: list[dict[str, Any]] = [
    # Domestic, small, daytime: 0, approve.
    _sample(
        "DEMO-0001",
        CARD_A,
        amount_minor=4_250,
        merchant_id="M-CAFE-01",
        merchant_country="AU",
        timestamp="2026-11-01T08:15:00+11:00",
    ),
    # PRD example: 2,000 AUD in GB at 03:00 local, new merchant: 30 + 25 + 10 = 65, step-up.
    _sample(
        "DEMO-0002",
        CARD_A,
        amount_minor=200_000,
        merchant_id="M-LDN-STORE",
        merchant_country="GB",
        timestamp="2026-11-01T03:00:00+00:00",
    ),
    # Stolen card: hard rule, decline. make tamper edits this record by default.
    _sample(
        "DEMO-0003",
        CARD_C,
        amount_minor=1_999,
        merchant_id="M-CAFE-01",
        merchant_country="AU",
        timestamp="2026-11-01T09:00:00+11:00",
        card_status="reported_stolen",
    ),
    # Blocklisted merchant: hard rule, decline.
    _sample(
        "DEMO-0004",
        CARD_B,
        amount_minor=3_000,
        merchant_id="M-BLOCK-001",
        merchant_country="AU",
        timestamp="2026-11-01T10:00:00+11:00",
    ),
    # Large domestic purchase: 25, approve (just under the step-up threshold).
    _sample(
        "DEMO-0005",
        CARD_B,
        amount_minor=150_000,
        merchant_id="M-ELECTRO",
        merchant_country="AU",
        timestamp="2026-11-01T11:30:00+11:00",
    ),
    # Overseas, small: 30, step-up (exactly on the threshold).
    _sample(
        "DEMO-0006",
        CARD_B,
        amount_minor=2_500,
        merchant_id="M-AKL-BOOKS",
        merchant_country="NZ",
        timestamp="2026-11-01T12:00:00+13:00",
    ),
    # Card-testing burst on card A: six attempts in 40 seconds at a US merchant.
    *[
        _sample(
            f"DEMO-{7 + n:04d}",
            CARD_A,
            amount_minor=100 + n,
            merchant_id="M-ONLINE-77",
            merchant_country="US",
            timestamp=f"2026-11-01T14:00:{n * 8:02d}-05:00",
        )
        for n in range(6)
    ],
    # The 6th burst attempt above scores 30 + 40 = 70, decline. Then a quiet day purchase.
    _sample(
        "DEMO-0013",
        CARD_B,
        amount_minor=6_800,
        merchant_id="M-GROCER",
        merchant_country="AU",
        timestamp="2026-11-01T17:45:00+11:00",
    ),
    # New merchant at night, domestic: 10, approve.
    _sample(
        "DEMO-0014",
        CARD_B,
        amount_minor=1_200,
        merchant_id="M-LATE-TAXI",
        merchant_country="AU",
        timestamp="2026-11-02T01:30:00+11:00",
    ),
]


def seed(path: Path = DEFAULT_DB) -> int:
    """Replace the database at `path` with the demo samples. Return the record count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for stale in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        stale.unlink(missing_ok=True)
    rule_set = active_rule_set(load_rule_sets())
    chain = Store(path)
    for sample in SAMPLES:
        chain.append(
            TransactionRequest.model_validate(sample).to_record(DEMO_CARD_REF_KEY), rule_set
        )
    return len(SAMPLES)


def main(argv: list[str]) -> int:
    path = Path(argv[0]) if argv else DEFAULT_DB
    count = seed(path)
    print(f"seeded {count} demo decisions into {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
