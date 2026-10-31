"""Hash-chained store. Expected values are worked out by hand from docs/PRD.md and
docs/plan.md sections 2.2-2.5, R6 and R7, using the v1 rules: foreign +30,
amount >= 1,000.00 AUD +25, more than 5 attempts in [t - 60 s, t] +40, new
merchant 00:00-05:59 local +10; under 30 approve, 30-69 step_up, 70+ decline.

Raw SQL in these tests stands in for someone with direct database access; the
application itself has no update or delete path."""

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app import engine, store
from app.chain import rule_set_sha256
from app.rules import active_rule_set, load_rule_sets
from app.store import RecordInput, Store, StoreWriteError
from tests.controls import control

V1 = active_rule_set(load_rule_sets())
ZEROS = "0" * 64
CARD_A = "a" * 32
CARD_B = "b" * 32
MELBOURNE = timezone(timedelta(hours=11))
FIXED_TIME = datetime(2026, 11, 1, 3, 0, 1, 123456, tzinfo=UTC)

# Domestic, small, daytime (14:00 local): scores 0 under v1 for a first attempt.
QUIET: dict[str, Any] = {
    "transaction_id": "T-0001",
    "card_masked": "****1111",
    "card_ref": CARD_A,
    "card_status": "active",
    "amount_minor": 5_000,
    "currency": "AUD",
    "merchant_id": "M-1000",
    "merchant_country": "AU",
    "timestamp": "2026-11-01T14:00:00+11:00",
}


def txn(**overrides: Any) -> RecordInput:
    return RecordInput.model_validate({**QUIET, **overrides})


def fixed_clock() -> datetime:
    return FIXED_TIME


def hourly_clock() -> Callable[[], datetime]:
    """Each call is an hour later, so received_at never lines up with txn timestamps."""
    calls = iter(range(1_000))
    return lambda: FIXED_TIME + timedelta(hours=next(calls))


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "decisions.db"


@pytest.fixture
def chain(db_path: Path) -> Store:
    return Store(db_path, clock=fixed_clock)


@pytest.fixture
def raw(db_path: Path, chain: Store) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path, isolation_level=None)
    yield connection
    connection.close()


def append_quiet(chain: Store, count: int) -> list[dict[str, Any]]:
    """Append `count` quiet records, 10 minutes apart so no velocity builds up."""
    start = datetime.fromisoformat(QUIET["timestamp"])
    return [
        chain.append(
            txn(transaction_id=f"T-{n:04d}", timestamp=start + timedelta(minutes=10 * n)), V1
        )
        for n in range(1, count + 1)
    ]


def rows(raw: sqlite3.Connection) -> list[tuple[int, str, str, str]]:
    return raw.execute("SELECT id, prev_hash, payload, hash FROM decisions ORDER BY id").fetchall()


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def stored_payload(raw: sqlite3.Connection, record_id: int) -> dict[str, Any]:
    (text,) = raw.execute("SELECT payload FROM decisions WHERE id = ?", (record_id,)).fetchone()
    return json.loads(text)


def overwrite(raw: sqlite3.Connection, record_id: int, text: str, *, rehash: bool) -> None:
    raw.execute("UPDATE decisions SET payload = ? WHERE id = ?", (text, record_id))
    if rehash:
        raw.execute("UPDATE decisions SET hash = ? WHERE id = ?", (sha256(text), record_id))


# Record format (plan 2.2, 2.3)


@control("AUD-01")
def test_stored_record_holds_exactly_the_hashed_payload(
    chain: Store, raw: sqlite3.Connection
) -> None:
    # The PRD example: 2,000 AUD in GB at 03:00 local, new merchant: 30 + 25 + 10 = 65.
    prd_example = txn(
        transaction_id="T-0007",
        amount_minor=200_000,
        merchant_id="M-1234",
        merchant_country="GB",
        timestamp="2026-11-01T03:00:00+00:00",
    )
    expected = {
        "record_version": 1,
        "id": 1,
        "prev_hash": ZEROS,
        "received_at": "2026-11-01T03:00:01.123456+00:00",
        "rule_version": 1,
        "rules_sha256": rule_set_sha256(V1),
        "input": {
            "transaction_id": "T-0007",
            "card_masked": "****1111",
            "card_ref": CARD_A,
            "card_status": "active",
            "amount_minor": 200_000,
            "currency": "AUD",
            "merchant_id": "M-1234",
            "merchant_country": "GB",
            "timestamp": "2026-11-01T03:00:00+00:00",
        },
        "features": {"attempts_in_window": 1, "merchant_seen_before": False},
        "score": 65,
        "reasons": [
            {"code": "FOREIGN_COUNTRY", "points": 30},
            {"code": "UNUSUAL_AMOUNT", "points": 25},
            {"code": "NEW_MERCHANT_AT_NIGHT", "points": 10},
        ],
        "outcome": "step_up",
    }

    assert chain.append(prd_example, V1) == expected
    columns = [row[1] for row in raw.execute("PRAGMA table_info(decisions)")]
    assert columns == ["id", "prev_hash", "payload", "hash"]
    [(record_id, prev_hash, payload, record_hash)] = rows(raw)
    assert (record_id, prev_hash) == (1, ZEROS)
    assert payload == canonical(expected)
    assert record_hash == sha256(payload)


@control("EXP-01")
def test_hard_rule_and_rule_error_reasons_are_stored_as_specified(
    chain: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    stolen = chain.append(txn(card_status="reported_stolen"), V1)
    monkeypatch.setitem(engine.SCORED_RULE_CHECKS, "amount_at_least", engine_error)
    rule_error = chain.append(txn(transaction_id="T-0002"), V1)

    assert (stolen["score"], stolen["reasons"], stolen["outcome"]) == (
        None,
        [{"code": "CARD_REPORTED_STOLEN", "points": None}],
        "decline",
    )
    assert (rule_error["score"], rule_error["reasons"], rule_error["outcome"]) == (
        0,
        [{"code": "RULE_ERROR", "points": None, "rule": "UNUSUAL_AMOUNT"}],
        "step_up",
    )


def engine_error(*args: Any) -> bool:
    raise RuntimeError("rule blew up")


# A2: verify (plan 2.5)


@control("AUD-01")
def test_t06_fresh_chain_and_empty_chain_verify_intact(
    chain: Store, raw: sqlite3.Connection
) -> None:
    assert chain.verify().model_dump(exclude_none=True) == {"intact": True, "records_checked": 0}

    append_quiet(chain, 5)

    assert chain.verify().model_dump(exclude_none=True) == {"intact": True, "records_checked": 5}
    stored = rows(raw)
    assert [row[0] for row in stored] == [1, 2, 3, 4, 5]
    assert [row[1] for row in stored] == [ZEROS] + [row[3] for row in stored[:-1]]


@control("AUD-01")
def test_t07_edited_payload_fails_verify_at_that_record(
    chain: Store, raw: sqlite3.Connection
) -> None:
    append_quiet(chain, 5)
    payload = stored_payload(raw, 3)
    assert payload["outcome"] == "approve"
    payload["outcome"] = "decline"

    overwrite(raw, 3, canonical(payload), rehash=False)
    result = chain.verify()

    assert (result.intact, result.records_checked, result.first_broken_id) == (False, 3, 3)
    assert "record 3" in result.reason
    assert "hash" in result.reason


@control("AUD-01")
def test_t08_edit_with_recomputed_hash_fails_at_the_next_record_naming_both(
    chain: Store, raw: sqlite3.Connection
) -> None:
    append_quiet(chain, 5)
    payload = stored_payload(raw, 3)
    payload["outcome"] = "decline"

    overwrite(raw, 3, canonical(payload), rehash=True)
    result = chain.verify()

    assert (result.intact, result.records_checked, result.first_broken_id) == (False, 4, 4)
    assert "record 3" in result.reason
    assert "record 4" in result.reason


@control("AUD-01")
@pytest.mark.parametrize("field", ["prev_hash column", "payload id"])
def test_record_that_disagrees_with_its_chain_position_fails_at_that_record(
    chain: Store, raw: sqlite3.Connection, field: str
) -> None:
    # The prev_hash column is outside the hash, so an edit to it alone must still
    # be caught; it is what UNIQUE relies on to refuse a second extension.
    append_quiet(chain, 5)
    if field == "prev_hash column":
        raw.execute("UPDATE decisions SET prev_hash = ? WHERE id = 3", ("e" * 64,))
    else:
        payload = stored_payload(raw, 3)
        payload["id"] = 4
        overwrite(raw, 3, canonical(payload), rehash=True)

    result = chain.verify()

    assert (result.intact, result.first_broken_id) == (False, 3)
    assert "record 3" in result.reason


@control("AUD-01")
def test_t09_deleted_record_fails_verify_at_the_next_record_as_a_gap(
    chain: Store, raw: sqlite3.Connection
) -> None:
    append_quiet(chain, 5)

    raw.execute("DELETE FROM decisions WHERE id = 3")
    result = chain.verify()

    assert (result.intact, result.first_broken_id) == (False, 4)
    assert "record 3" in result.reason
    assert "missing" in result.reason


@control("AUD-01")
def test_t39_non_canonical_rewrite_of_the_same_values_fails_at_that_record(
    chain: Store, raw: sqlite3.Connection
) -> None:
    append_quiet(chain, 5)
    payload = stored_payload(raw, 3)
    reordered = json.dumps(dict(reversed(payload.items())), indent=1)
    assert json.loads(reordered) == payload

    overwrite(raw, 3, reordered, rehash=True)
    result = chain.verify()

    assert (result.intact, result.first_broken_id) == (False, 3)
    assert "record 3" in result.reason
    assert "canonical" in result.reason


@control("AUD-01")
def test_t40_duplicate_card_ref_key_fails_at_that_record(
    chain: Store, raw: sqlite3.Connection
) -> None:
    append_quiet(chain, 5)
    (text,) = raw.execute("SELECT payload FROM decisions WHERE id = 3").fetchone()
    forged = text.replace('"input":{', '"input":{"card_ref":"' + "0" * 32 + '",', 1)
    assert forged != text

    overwrite(raw, 3, forged, rehash=True)
    result = chain.verify()

    assert (result.intact, result.first_broken_id) == (False, 3)
    assert "record 3" in result.reason
    assert "duplicate key" in result.reason


@control("AUD-01")
def test_payload_stored_as_a_blob_fails_verify(tmp_path: Path) -> None:
    # A table rebuilt without type enforcement lets the same bytes be stored as
    # a BLOB, which SQLite's JSON functions would read as binary JSON.
    db_path = tmp_path / "loose.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE decisions (id INTEGER PRIMARY KEY, prev_hash TEXT NOT NULL UNIQUE,"
            " payload, hash TEXT NOT NULL UNIQUE)"
        )
    chain = Store(db_path, clock=fixed_clock)
    append_quiet(chain, 3)
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE decisions SET payload = CAST(payload AS BLOB) WHERE id = 2")

    result = chain.verify()

    assert (result.intact, result.first_broken_id) == (False, 2)
    assert "record 2" in result.reason


# A7: serialised appends (plan 2.4)


@control("AUD-02")
def test_t26_concurrent_appends_form_one_linked_chain(
    chain: Store, raw: sqlite3.Connection
) -> None:
    # Same card, same timestamp: if features are read under the same lock as the
    # head, record n sees exactly n attempts.
    barrier = threading.Barrier(20)
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        barrier.wait()
        try:
            chain.append(txn(transaction_id=f"T-{n:04d}"), V1)
        except BaseException as error:  # reported by the assertion below
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stored = rows(raw)
    assert [row[0] for row in stored] == list(range(1, 21))
    assert len({row[1] for row in stored}) == 20
    assert [row[1] for row in stored] == [ZEROS] + [row[3] for row in stored[:-1]]
    assert [json.loads(row[2])["features"]["attempts_in_window"] for row in stored] == list(
        range(1, 21)
    )
    assert chain.verify().intact


@control("AUD-02")
@pytest.mark.parametrize("column", ["prev_hash", "hash"])
def test_t28_database_refuses_a_second_record_with_an_existing_hash(
    chain: Store, raw: sqlite3.Connection, column: str
) -> None:
    append_quiet(chain, 2)
    [(_, _, _, hash_1), (_, _, _, hash_2)] = rows(raw)
    # Either a second record extending record 1, or a record repeating record 2's hash.
    values = (3, hash_1, "{}", "f" * 64) if column == "prev_hash" else (3, hash_2, "{}", hash_2)

    with pytest.raises(sqlite3.IntegrityError, match=f"decisions.{column}"):
        raw.execute(
            "INSERT INTO decisions (id, prev_hash, payload, hash) VALUES (?, ?, ?, ?)", values
        )


@control("AUD-02")
def test_failed_write_raises_store_write_error_and_adds_nothing(
    chain: Store, raw: sqlite3.Connection
) -> None:
    append_quiet(chain, 2)
    raw.execute(
        "CREATE TRIGGER refuse BEFORE INSERT ON decisions BEGIN SELECT RAISE(ABORT, 'no'); END"
    )

    with pytest.raises(StoreWriteError):
        chain.append(txn(transaction_id="T-0099"), V1)

    raw.execute("DROP TRIGGER refuse")
    assert len(rows(raw)) == 2
    assert chain.append(txn(transaction_id="T-0100"), V1)["id"] == 3
    assert chain.verify().intact


@control("AUD-02")
def test_open_reader_does_not_block_an_append(chain: Store, raw: sqlite3.Connection) -> None:
    # WAL mode: a long verify-style read keeps its snapshot while a writer commits.
    append_quiet(chain, 1)
    raw.execute("BEGIN")
    assert raw.execute("SELECT count(*) FROM decisions").fetchone() == (1,)

    chain.append(txn(transaction_id="T-0002"), V1)

    assert raw.execute("SELECT count(*) FROM decisions").fetchone() == (1,)
    raw.execute("COMMIT")
    assert raw.execute("SELECT count(*) FROM decisions").fetchone() == (2,)


# Fail safe on history (plan 2.3, R13)


@control("FS-01")
def test_history_failure_is_recorded_with_null_features(
    chain: Store, raw: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_history(*args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "read_features", broken_history)

    payload = chain.append(txn(), V1)

    assert (payload["features"], payload["score"], payload["reasons"], payload["outcome"]) == (
        None,
        0,
        [{"code": "HISTORY_UNAVAILABLE", "points": None}],
        "step_up",
    )
    assert stored_payload(raw, 1) == payload
    assert chain.verify().intact


# Velocity and new-merchant features (plan R6, R7)


def decide_after(earlier: list[RecordInput], current: RecordInput, db_path: Path) -> dict[str, Any]:
    chain = Store(db_path, clock=hourly_clock())
    for n, record in enumerate(earlier):
        chain.append(record.model_copy(update={"transaction_id": f"T-E{n:03d}"}), V1)
    return chain.append(current, V1)


@control("EXP-01")
def test_fifth_attempt_in_a_minute_does_not_fire_and_sixth_does(db_path: Path) -> None:
    t = datetime.fromisoformat(QUIET["timestamp"])
    earlier = [txn(timestamp=t - timedelta(seconds=s)) for s in (40, 30, 20, 10)]

    fifth = decide_after(earlier, txn(transaction_id="T-0005", timestamp=t), db_path)
    sixth = Store(db_path, clock=fixed_clock).append(
        txn(transaction_id="T-0006", timestamp=t + timedelta(seconds=5)), V1
    )

    assert fifth["features"]["attempts_in_window"] == 5
    assert (fifth["reasons"], fifth["outcome"]) == ([], "approve")
    assert sixth["features"]["attempts_in_window"] == 6
    assert (sixth["reasons"], sixth["outcome"]) == (
        [{"code": "HIGH_VELOCITY", "points": 40}],
        "step_up",
    )


@control("EXP-01")
@pytest.mark.parametrize(
    ("earlier_timestamp", "attempts"),
    [
        ("2026-11-01T02:59:00+00:00", 2),  # exactly 60 s before 14:00:00+11:00, other offset
        ("2026-11-01T02:58:59+00:00", 1),  # 61 s before
        ("2026-11-01T14:00:01+11:00", 1),  # recorded earlier but timestamped after
    ],
)
def test_velocity_window_is_inclusive_by_transaction_timestamp(
    db_path: Path, earlier_timestamp: str, attempts: int
) -> None:
    payload = decide_after([txn(timestamp=earlier_timestamp)], txn(), db_path)

    assert payload["features"]["attempts_in_window"] == attempts


@control("EXP-01")
def test_another_cards_history_is_ignored(db_path: Path) -> None:
    night = "2026-11-01T03:00:00+11:00"
    other_card = [txn(card_ref=CARD_B, merchant_id="M-2000", timestamp=night)] * 5

    payload = decide_after(other_card, txn(merchant_id="M-2000", timestamp=night), db_path)

    assert payload["features"] == {"attempts_in_window": 1, "merchant_seen_before": False}
    assert payload["reasons"] == [{"code": "NEW_MERCHANT_AT_NIGHT", "points": 10}]


@control("EXP-01")
def test_merchant_used_before_by_the_same_card_is_not_new(db_path: Path) -> None:
    three_days_earlier = txn(merchant_id="M-2000", timestamp="2026-10-29T13:00:00+11:00")

    payload = decide_after(
        [three_days_earlier],
        txn(merchant_id="M-2000", timestamp="2026-11-01T03:00:00+11:00"),
        db_path,
    )

    assert payload["features"] == {"attempts_in_window": 1, "merchant_seen_before": True}
    assert payload["reasons"] == []


# Versioned change and evidence-only clock


@control("VER-01")
def test_t36_record_carries_the_rule_version_and_rule_set_hash(chain: Store) -> None:
    v7 = V1.model_copy(update={"version": 7})

    first = chain.append(txn(), V1)
    second = chain.append(txn(transaction_id="T-0002"), v7)

    assert (first["rule_version"], first["rules_sha256"]) == (1, rule_set_sha256(V1))
    assert (second["rule_version"], second["rules_sha256"]) == (7, rule_set_sha256(v7))
    assert rule_set_sha256(v7) != rule_set_sha256(V1)


@control("REP-01")
def test_t42_received_at_is_evidence_only(tmp_path: Path) -> None:
    # Clock A reads 03:00 UTC (night), clock B 23:00 at +11:00. The transaction
    # is 14:00 local at a new merchant, so only a rule reading the clock would
    # see night.
    clocks = {
        "a": lambda: datetime(2026, 11, 1, 3, 0, tzinfo=UTC),
        "b": lambda: datetime(2026, 11, 1, 23, 0, tzinfo=MELBOURNE),
    }
    payloads = {}
    for name, clock in clocks.items():
        chain = Store(tmp_path / f"{name}.db", clock=clock)
        chain.append(txn(merchant_id="M-0001", timestamp="2026-11-01T13:59:30+11:00"), V1)
        payloads[name] = chain.append(txn(transaction_id="T-0002", merchant_id="M-2000"), V1)

    expected = {
        "features": {"attempts_in_window": 2, "merchant_seen_before": False},
        "score": 0,
        "reasons": [],
        "outcome": "approve",
    }
    for payload in payloads.values():
        assert {key: payload[key] for key in expected} == expected
    assert payloads["a"]["received_at"] == "2026-11-01T03:00:00+00:00"
    assert payloads["b"]["received_at"] == "2026-11-01T12:00:00+00:00"


@control("AUD-02")
def test_received_at_is_read_inside_the_write_lock(db_path: Path) -> None:
    lock_held: list[bool] = []

    def probing_clock() -> datetime:
        probe = sqlite3.connect(db_path, timeout=0, isolation_level=None)
        try:
            probe.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            lock_held.append(True)
        else:
            probe.execute("ROLLBACK")
            lock_held.append(False)
        finally:
            probe.close()
        return FIXED_TIME

    Store(db_path, clock=probing_clock).append(txn(), V1)

    assert lock_held == [True]
