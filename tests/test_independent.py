"""Independent black-box tests written from the spec alone.

Sources: docs/PRD.md (A1 to A8), docs/plan.md (sections 2 to 4 and 7),
docs/challenges/01-plan-challenge.md (author decisions), rules/v1.yaml and
README.md. The implementation and the builder's tests were not read. The tests
use only the HTTP API and, where a test plays someone with database access,
sqlite3 on the database file.

v1 points (rules/v1.yaml): FOREIGN_COUNTRY 30 (country != AU), UNUSUAL_AMOUNT 25
(amount_minor >= 100000), HIGH_VELOCITY 40 (more than 5 attempts in
[t - 60 s, t]), NEW_MERCHANT_AT_NIGHT 10 (no earlier record for this card and
merchant, local hour 0 to 5). Thresholds: < 30 approve, 30 to 69 step_up,
>= 70 decline.
"""

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rules import RULES_DIR
from tests.controls import control

ANALYST = {"X-Role": "analyst"}
AUDITOR = {"X-Role": "auditor"}
ZERO_HASH = "0" * 64

CARD_A = "4111111111111111"
CARD_B = "5555555555554444"
CARD_C = "4000056655665556"

# Daytime in Melbourne (+11:00 in November), so the night rule never fires.
DAY = "2026-11-01T12:00:00+11:00"


def txn(**overrides: Any) -> dict[str, Any]:
    """A transaction that scores 0 under v1: home country, 50.00 AUD, daytime."""
    body: dict[str, Any] = {
        "transaction_id": "T-0001",
        "card_number": CARD_A,
        "card_status": "active",
        "amount_minor": 5000,
        "currency": "AUD",
        "merchant_id": "M-HOME-1",
        "merchant_country": "AU",
        "timestamp": DAY,
    }
    body.update(overrides)
    return body


def decide(client: TestClient, **overrides: Any) -> dict[str, Any]:
    response = client.post("/decisions", json=txn(**overrides), headers=ANALYST)
    assert response.status_code == 201, response.text
    return response.json()


def reasons(decision: dict[str, Any]) -> list[tuple[str, int | None]]:
    return [(r["code"], r["points"]) for r in decision["reasons"]]


def verify(client: TestClient) -> dict[str, Any]:
    response = client.get("/audit/verify", headers=AUDITOR)
    assert response.status_code == 200, response.text
    return response.json()


def db_rows(db_path: Path) -> list[tuple[int, str, str, str]]:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(
            "SELECT id, prev_hash, payload, hash FROM decisions ORDER BY id"
        ).fetchall()
    finally:
        con.close()


def db_count(db_path: Path) -> int:
    return len(db_rows(db_path))


def tamper(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> None:
    """Change the table the way someone with database access could: drop any
    triggers on `decisions`, run the statement, then restore the triggers."""
    con = sqlite3.connect(db_path)
    try:
        triggers = con.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'decisions'"
        ).fetchall()
        for name, _ in triggers:
            con.execute(f'DROP TRIGGER "{name}"')
        con.execute(sql, params)
        for _, trigger_sql in triggers:
            con.execute(trigger_sql)
        con.commit()
    finally:
        con.close()


def payload_of(db_path: Path, record_id: int) -> str:
    return next(row[2] for row in db_rows(db_path) if row[0] == record_id)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def luhn_complete(prefix: str) -> str:
    """Append the Luhn check digit to a digit string."""
    total = 0
    for i, ch in enumerate(reversed(prefix)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return prefix + str((10 - total % 10) % 10)


def write_rules(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text)
    return directory


def v1_text() -> str:
    return (Path(RULES_DIR) / "v1.yaml").read_text()


def fixed(hour: int) -> Callable[[], datetime]:
    return lambda: datetime(2026, 11, 1, hour, 0, 0, tzinfo=UTC)


def seed_five(client: TestClient) -> None:
    """Five records with distinct amounts so each payload is easy to find."""
    for i in range(1, 6):
        decide(client, transaction_id=f"T-{i}", amount_minor=1000 + i, merchant_id=f"M-{i}")


# ---------------------------------------------------------------- A1, REP-01


SEQUENCE: list[dict[str, Any]] = [
    # PRD example: GB, 2,000 AUD, 03:00 local, new merchant -> 30 + 25 + 10 = 65.
    txn(
        transaction_id="S-1",
        amount_minor=200000,
        merchant_id="M-UK-1",
        merchant_country="GB",
        timestamp="2026-11-01T03:00:00+00:00",
    ),
    txn(transaction_id="S-2", card_status="reported_stolen"),
    txn(transaction_id="S-3", merchant_id="M-BLOCK-001"),
    *[txn(transaction_id=f"S-B{i}", card_number=CARD_B) for i in range(6)],
    txn(transaction_id="S-4", card_number=CARD_C, timestamp="2026-11-01T02:00:00+11:00"),
]


def run_sequence(db: Path, reverse_keys: bool) -> None:
    app = create_app(db, rules_dir=RULES_DIR, clock=fixed(3), card_ref_key="independent")
    with TestClient(app) as c:
        for body in SEQUENCE:
            sent = dict(reversed(list(body.items()))) if reverse_keys else body
            assert c.post("/decisions", json=sent, headers=ANALYST).status_code == 201


@control("REP-01")
def test_same_inputs_on_two_fresh_databases_give_identical_records(tmp_path: Path) -> None:
    # A1 and T04: same inputs, rule version, clock and key give byte-identical
    # payloads and hashes, even when the request keys arrive in another order.
    run_sequence(tmp_path / "a.db", reverse_keys=False)
    run_sequence(tmp_path / "b.db", reverse_keys=True)
    rows_a = db_rows(tmp_path / "a.db")
    rows_b = db_rows(tmp_path / "b.db")
    assert len(rows_a) == len(SEQUENCE)
    assert rows_a == rows_b


@control("REP-01")
def test_every_stored_decision_replays_as_a_match(client: TestClient) -> None:
    for body in SEQUENCE:
        assert client.post("/decisions", json=body, headers=ANALYST).status_code == 201
    for record_id in range(1, len(SEQUENCE) + 1):
        for role in (ANALYST, AUDITOR):
            replay = client.get(f"/decisions/{record_id}/replay", headers=role)
            assert replay.status_code == 200, replay.text
            body = replay.json()
            assert body["id"] == record_id
            assert body["match"] is True
            assert body["rules_match"] is True
            assert body["stored"] == body["replayed"]
            stored = client.get(f"/decisions/{record_id}", headers=role).json()
            # features_match is null only when stored features are null.
            assert body["features_match"] is (None if stored["features"] is None else True)


@control("REP-01")
def test_decision_ignores_server_clock_and_process_timezone(
    make_client: Callable[..., TestClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    # 03:00+11:00 is night in its own offset (16:00 UTC the day before): +10.
    # 14:00-10:00 is day in its own offset (00:00 UTC next day, 11:00 in
    # Melbourne): no night points. A rule that converts to server local time or
    # to UTC gets one of the two wrong.
    cases = [
        ("2026-11-01T03:00:00+11:00", [("NEW_MERCHANT_AT_NIGHT", 10)], 10),
        ("2026-11-05T14:00:00-10:00", [], 0),
    ]
    results = []
    try:
        for i, (tz, clock_hour) in enumerate([("UTC", 3), ("Australia/Melbourne", 14)]):
            monkeypatch.setenv("TZ", tz)
            time.tzset()
            c = make_client(clock=fixed(clock_hour))
            for j, (ts, expected_reasons, expected_score) in enumerate(cases):
                d = decide(c, transaction_id=f"Z-{i}-{j}", merchant_id=f"M-Z-{i}-{j}", timestamp=ts)
                assert reasons(d) == expected_reasons, (tz, ts)
                assert d["score"] == expected_score
                results.append((d["score"], d["reasons"], d["outcome"]))
    finally:
        monkeypatch.undo()
        time.tzset()
    assert results[:2] == results[2:]


# ---------------------------------------------------------------- A2, AUD-01


@control("AUD-01")
def test_empty_and_fresh_chains_verify_intact(client: TestClient) -> None:
    assert verify(client)["intact"] is True
    seed_five(client)
    result = verify(client)
    assert result["intact"] is True
    assert result["records_checked"] == 5


@control("AUD-01")
@pytest.mark.parametrize("record_id", [1, 2, 3, 4, 5])
def test_editing_any_record_names_that_record(
    client: TestClient, db_path: Path, record_id: int
) -> None:
    # Record k was stored with amount 1000 + k. Changing it without fixing the
    # hash breaks record k itself, including the first and the latest record.
    seed_five(client)
    old = payload_of(db_path, record_id)
    new = old.replace(f'"amount_minor":{1000 + record_id}', '"amount_minor":1')
    assert new != old
    tamper(db_path, "UPDATE decisions SET payload = ? WHERE id = ?", (new, record_id))
    result = verify(client)
    assert result["intact"] is False
    assert result["first_broken_id"] == record_id


@control("AUD-01")
def test_outcome_flip_in_middle_record_is_named(client: TestClient, db_path: Path) -> None:
    # The make tamper scenario: record 3 is a stolen-card decline, flip it to approve.
    decide(client, transaction_id="T-1")
    decide(client, transaction_id="T-2", merchant_id="M-2")
    decide(client, transaction_id="T-3", card_status="reported_stolen")
    decide(client, transaction_id="T-4", merchant_id="M-4")
    old = payload_of(db_path, 3)
    assert '"outcome":"decline"' in old
    tamper(
        db_path,
        "UPDATE decisions SET payload = ? WHERE id = 3",
        (old.replace('"outcome":"decline"', '"outcome":"approve"'),),
    )
    result = verify(client)
    assert (result["intact"], result["first_broken_id"]) == (False, 3)


@control("AUD-01")
def test_edit_with_recomputed_hash_breaks_at_next_record(client: TestClient, db_path: Path) -> None:
    # Plan 2.5: the edited record 3 is self-consistent, so the break shows at 4
    # as a link mismatch, and the reason names both records.
    seed_five(client)
    new = payload_of(db_path, 3).replace('"amount_minor":1003', '"amount_minor":1')
    tamper(
        db_path, "UPDATE decisions SET payload = ?, hash = ? WHERE id = 3", (new, sha256_hex(new))
    )
    result = verify(client)
    assert (result["intact"], result["first_broken_id"]) == (False, 4)
    assert re.search(r"\b3\b", result["reason"]) and re.search(r"\b4\b", result["reason"])


@control("AUD-01")
@pytest.mark.parametrize(
    ("kind", "first_broken"),
    [
        ("delete", 4),  # id 4 follows id 2, which is not previous + 1
        ("prev_hash_column", 3),  # column no longer equals record 2's hash
        ("reformatted", 3),  # T39: same values, non-canonical bytes, hash recomputed
        ("duplicate_key", 3),  # T40: duplicate card_ref key first, hash recomputed
    ],
)
def test_structural_tampering_is_named(
    client: TestClient, db_path: Path, kind: str, first_broken: int
) -> None:
    # Plan 2.5 failure conditions. For the two non-canonical cases the hash
    # matches the new bytes, so a hash-only verify would wrongly say 4.
    seed_five(client)
    old = payload_of(db_path, 3)
    if kind == "delete":
        tamper(db_path, "DELETE FROM decisions WHERE id = 3")
    elif kind == "prev_hash_column":
        tamper(db_path, "UPDATE decisions SET prev_hash = ? WHERE id = 3", ("f" * 64,))
    else:
        if kind == "reformatted":
            new = json.dumps(json.loads(old), indent=2)
        else:
            new = old.replace('"card_ref":"', f'"card_ref":"{"0" * 32}","card_ref":"', 1)
        assert new != old
        tamper(
            db_path,
            "UPDATE decisions SET payload = ?, hash = ? WHERE id = 3",
            (new, sha256_hex(new)),
        )
    result = verify(client)
    assert (result["intact"], result["first_broken_id"]) == (False, first_broken)


@control("AUD-01")
def test_stored_record_hash_covers_payload_and_previous_hash(
    client: TestClient, db_path: Path
) -> None:
    # Plan 2.2 and 2.3: hash = sha256(stored payload bytes); the payload holds
    # id and prev_hash; record 1 links to 64 zeros; the payload is canonical.
    seed_five(client)
    previous = ZERO_HASH
    for record_id, prev_hash, payload, digest in db_rows(db_path):
        body = json.loads(payload)
        assert prev_hash == previous == body["prev_hash"]
        assert body["id"] == record_id
        assert digest == sha256_hex(payload)
        assert payload == json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        previous = digest


# ---------------------------------------------------------------- A3, ACC-01


@control("ACC-01")
def test_auditor_cannot_create_a_decision(client: TestClient, db_path: Path) -> None:
    decide(client)
    response = client.post("/decisions", json=txn(transaction_id="T-AUD"), headers=AUDITOR)
    assert response.status_code == 403
    assert "outcome" not in response.json()
    assert db_count(db_path) == 1
    assert len(client.get("/decisions", headers=AUDITOR).json()) == 1


@control("ACC-01")
@pytest.mark.parametrize(
    "headers", [{}, {"X-Role": ""}, {"X-Role": "admin"}, {"X-Role": "analyst,auditor"}]
)
def test_missing_or_unknown_role_gets_401_and_adds_nothing(
    client: TestClient, db_path: Path, headers: dict[str, str]
) -> None:
    response = client.post("/decisions", json=txn(), headers=headers)
    assert response.status_code == 401
    assert db_count(db_path) == 0


@control("ACC-01")
def test_role_matrix(client: TestClient) -> None:
    # Plan section 3 endpoint table: deny by default, 401 without a valid role.
    decide(client)
    expected = {
        ("GET", "/decisions"): {"analyst": 200, "auditor": 200, None: 401},
        ("GET", "/decisions/1"): {"analyst": 200, "auditor": 200, None: 401},
        ("GET", "/decisions/1/replay"): {"analyst": 200, "auditor": 200, None: 401},
        ("GET", "/audit/verify"): {"analyst": 403, "auditor": 200, None: 401},
        ("GET", "/"): {None: 200},
        ("GET", "/docs"): {None: 200},
    }
    for (method, path), by_role in expected.items():
        for role, status in by_role.items():
            headers = {"X-Role": role} if role else {}
            assert client.request(method, path, headers=headers).status_code == status, (path, role)


# ---------------------------------------------------------------- A4, FS-01


@control("FS-01")
def test_unreadable_history_gives_step_up_and_replays(client: TestClient, db_path: Path) -> None:
    # Someone breaks an earlier record so the history can no longer be read.
    # The new transaction would score 0 (approve) with history; without it the
    # plan (section 3 step 2, T18, T45) says step_up with HISTORY_UNAVAILABLE,
    # recorded with features null, and replay matches with features_match null.
    decide(client, transaction_id="T-1")
    tamper(db_path, "UPDATE decisions SET payload = ? WHERE id = 1", ("{not json",))
    response = client.post("/decisions", json=txn(transaction_id="T-2"), headers=ANALYST)
    assert response.status_code == 201, response.text
    d = response.json()
    assert d["outcome"] == "step_up"
    assert d["features"] is None
    assert "HISTORY_UNAVAILABLE" in [code for code, _ in reasons(d)]
    replay = client.get(f"/decisions/{d['id']}/replay", headers=AUDITOR).json()
    assert replay["match"] is True
    assert replay["features_match"] is None


@control("FS-01")
def test_write_failure_returns_503_and_no_decision(client: TestClient, db_path: Path) -> None:
    # Plan R13 and T43: if the record cannot be written, no decision is returned.
    decide(client, transaction_id="T-1")
    tamper(
        db_path,
        "CREATE TRIGGER independent_block_insert BEFORE INSERT ON decisions "
        "BEGIN SELECT RAISE(ABORT, 'blocked'); END",
    )
    response = client.post("/decisions", json=txn(transaction_id="T-2"), headers=ANALYST)
    assert response.status_code == 503
    assert "outcome" not in response.text
    assert db_count(db_path) == 1


@control("FS-01")
@pytest.mark.parametrize(
    "rule_line",
    [
        "  - {code: X_RULE, type: no_such_type, points: 5}\n",
        "  - {code: FOREIGN_COUNTRY, type: foreign_country}\n",
    ],
)
def test_invalid_rule_file_stops_startup(
    make_client: Callable[..., TestClient], tmp_path: Path, rule_line: str
) -> None:
    # Plan section 3: an unknown rule type or a missing field fails closed.
    text = v1_text().replace(
        "  - {code: FOREIGN_COUNTRY,       type: foreign_country,       points: 30}\n", rule_line
    )
    assert text != v1_text()
    rules = write_rules(tmp_path / "bad_rules", "v1.yaml", text)
    with pytest.raises(Exception):  # noqa: B017 - any startup failure is fail closed
        make_client(rules_dir=rules)


# ---------------------------------------------------------------- A5, EXP-01


EDGE_RULES = """\
version: 1
thresholds: {step_up: 30, decline: 70}
home_country: AU
hard_rules:
  - {code: CARD_REPORTED_STOLEN, type: card_status_is, status: reported_stolen}
  - {code: MERCHANT_BLOCKLISTED, type: merchant_in_list, merchants: [M-BLOCK-001]}
scored_rules:
  - {code: FOREIGN_COUNTRY, type: foreign_country, points: 29}
  - {code: UNUSUAL_AMOUNT, type: amount_at_least, amount_minor: 100000, points: 1}
  - {code: HIGH_VELOCITY, type: attempts_over, window_seconds: 60, max_attempts: 5, points: 5}
  - {code: NEW_MERCHANT_AT_NIGHT, type: new_merchant_at_night, night_start_hour: 0,
      night_end_hour: 6, points: 40}
"""


@control("EXP-01")
@pytest.mark.parametrize(
    ("country", "amount", "ts", "score", "outcome"),
    [
        ("GB", 5000, DAY, 29, "approve"),  # 29
        ("GB", 100000, DAY, 30, "step_up"),  # 29 + 1
        ("GB", 5000, "2026-11-01T03:00:00+11:00", 69, "step_up"),  # 29 + 40
        ("GB", 100000, "2026-11-01T03:00:00+11:00", 70, "decline"),  # 29 + 1 + 40
        ("GB", 99999, "2026-11-01T03:00:00+11:00", 69, "step_up"),  # 99999 < 100000
    ],
)
def test_threshold_edges_with_reachable_scores(
    make_client: Callable[..., TestClient],
    tmp_path: Path,
    country: str,
    amount: int,
    ts: str,
    score: int,
    outcome: str,
) -> None:
    # A5 and plan R2: v1 cannot reach 29 or 69, so a test rule set does.
    c = make_client(rules_dir=write_rules(tmp_path / "edge_rules", "v1.yaml", EDGE_RULES))
    d = decide(c, merchant_country=country, amount_minor=amount, timestamp=ts)
    assert (d["score"], d["outcome"]) == (score, outcome)
    assert sum(points for _, points in reasons(d)) == score
    assert d["thresholds"] == {"step_up": 30, "decline": 70}


@control("EXP-01")
@pytest.mark.parametrize(
    ("overrides", "expected_reasons", "score", "outcome"),
    [
        ({}, [], 0, "approve"),
        ({"amount_minor": 100000}, [("UNUSUAL_AMOUNT", 25)], 25, "approve"),  # >= is inclusive
        ({"amount_minor": 99999}, [], 0, "approve"),
        # M-BLOCK-0011 is not in [M-BLOCK-001, M-BLOCK-002]; a prefix match is wrong.
        ({"merchant_id": "M-BLOCK-0011"}, [], 0, "approve"),
        ({"merchant_country": "GB"}, [("FOREIGN_COUNTRY", 30)], 30, "step_up"),
        (
            # PRD example: 30 + 25 + 10 = 65, reasons in YAML order.
            {
                "amount_minor": 200000,
                "merchant_country": "GB",
                "timestamp": "2026-11-01T03:00:00+00:00",
            },
            [("FOREIGN_COUNTRY", 30), ("UNUSUAL_AMOUNT", 25), ("NEW_MERCHANT_AT_NIGHT", 10)],
            65,
            "step_up",
        ),
    ],
)
def test_v1_scores_and_reasons(
    client: TestClient,
    overrides: dict[str, Any],
    expected_reasons: list[tuple[str, int]],
    score: int,
    outcome: str,
) -> None:
    d = decide(client, **overrides)
    assert reasons(d) == expected_reasons
    assert (d["score"], d["outcome"]) == (score, outcome)
    assert d["rule_version"] == 1


@control("EXP-01")
def test_v1_score_of_exactly_70_declines(client: TestClient) -> None:
    # Five earlier attempts, then a GB attempt: 30 (foreign) + 40 (6 > 5) = 70.
    for i in range(5):
        decide(client, transaction_id=f"T-{i}")
    d = decide(client, transaction_id="T-6", merchant_country="GB", merchant_id="M-GB")
    assert reasons(d) == [("FOREIGN_COUNTRY", 30), ("HIGH_VELOCITY", 40)]
    assert (d["score"], d["outcome"]) == (70, "decline")


@control("VER-01")
def test_thresholds_come_from_the_rule_file(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    # Same points, thresholds 20 and 50: 25 -> step_up (v1: approve),
    # 30 + 25 = 55 -> decline (v1: step_up).
    text = v1_text().replace("{step_up: 30, decline: 70}", "{step_up: 20, decline: 50}")
    assert text != v1_text()
    c = make_client(rules_dir=write_rules(tmp_path / "t_rules", "v1.yaml", text))
    d = decide(c, amount_minor=100000)
    assert (d["score"], d["outcome"]) == (25, "step_up")
    d = decide(
        c, transaction_id="T-2", amount_minor=100000, merchant_country="GB", merchant_id="M2"
    )
    assert (d["score"], d["outcome"]) == (55, "decline")
    assert d["thresholds"] == {"step_up": 20, "decline": 50}


# ---------------------------------------------------------------- A6, EXP-01


@control("EXP-01")
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"card_status": "reported_stolen"}, "CARD_REPORTED_STOLEN"),
        ({"merchant_id": "M-BLOCK-002"}, "MERCHANT_BLOCKLISTED"),
        # Hard rule on a transaction that would otherwise score 65.
        (
            {
                "card_status": "reported_stolen",
                "amount_minor": 200000,
                "merchant_country": "GB",
                "timestamp": "2026-11-01T03:00:00+00:00",
            },
            "CARD_REPORTED_STOLEN",
        ),
        # Both hard rules: the first in YAML order stops everything.
        ({"card_status": "reported_stolen", "merchant_id": "M-BLOCK-001"}, "CARD_REPORTED_STOLEN"),
    ],
)
def test_hard_rule_declines_whatever_the_score(
    client: TestClient, overrides: dict[str, Any], code: str
) -> None:
    # Plan 2.3 and R11: score null, only the hard rule's code with points null.
    d = decide(client, **overrides)
    assert d["outcome"] == "decline"
    assert d["score"] is None
    assert reasons(d) == [(code, None)]


# ---------------------------------------------------------------- velocity (R7) and night (R5, R6)


@control("EXP-01")
def test_sixth_attempt_in_a_minute_fires_and_fifth_does_not(client: TestClient) -> None:
    # Attempts = earlier same-card decisions in [t - 60 s, t] plus this one.
    outcomes = [decide(client, transaction_id=f"T-{i}") for i in range(1, 7)]
    fifth, sixth = outcomes[4], outcomes[5]
    assert fifth["features"]["attempts_in_window"] == 5
    assert (fifth["score"], fifth["outcome"], reasons(fifth)) == (0, "approve", [])
    assert sixth["features"]["attempts_in_window"] == 6
    assert (sixth["score"], sixth["outcome"]) == (40, "step_up")
    assert reasons(sixth) == [("HIGH_VELOCITY", 40)]


@control("EXP-01")
@pytest.mark.parametrize(
    ("first_ts", "attempts", "score"),
    [
        ("2026-11-01T11:59:00+11:00", 6, 40),  # exactly t - 60 s: counts, 6 > 5
        ("2026-11-01T11:58:59+11:00", 5, 0),  # t - 61 s: outside, 5 is not > 5
        ("2026-11-01T00:59:00+00:00", 6, 40),  # t - 60 s written in UTC: still counts
    ],
)
def test_velocity_window_edges(
    client: TestClient, first_ts: str, attempts: int, score: int
) -> None:
    # One attempt at first_ts, four at t - 30 s, then the attempt at t = 12:00+11:00.
    decide(client, transaction_id="T-0", timestamp=first_ts)
    for i in range(4):
        decide(client, transaction_id=f"T-{i + 1}", timestamp="2026-11-01T11:59:30+11:00")
    d = decide(client, transaction_id="T-5", timestamp="2026-11-01T12:00:00+11:00")
    assert d["features"]["attempts_in_window"] == attempts
    assert d["score"] == score


@control("EXP-01")
def test_velocity_uses_transaction_time_and_card(client: TestClient) -> None:
    # Five attempts stamped 60 s after t are outside [t - 60 s, t] even though
    # they arrived first; another card's attempts never count.
    for i in range(5):
        decide(client, transaction_id=f"T-{i}", timestamp="2026-11-01T12:01:00+11:00")
    d = decide(client, transaction_id="T-5", timestamp=DAY)
    assert (d["features"]["attempts_in_window"], d["score"]) == (1, 0)
    d = decide(
        client, transaction_id="T-6", card_number=CARD_B, timestamp="2026-11-01T12:01:00+11:00"
    )
    assert (d["features"]["attempts_in_window"], d["score"]) == (1, 0)


@control("EXP-01")
@pytest.mark.parametrize(
    ("ts", "night"),
    [
        ("2026-11-01T00:00:00+11:00", True),  # first second of the window
        ("2026-11-01T05:59:59+11:00", True),  # last second of the window
        ("2026-11-01T06:00:00+11:00", False),  # night_end_hour is exclusive
        ("2026-11-01T23:59:59+11:00", False),
        ("2026-11-01T02:30:00+05:30", True),  # 21:00 UTC the day before
        ("2026-11-01T22:00:00-05:00", False),  # 03:00 UTC next day, but 22:00 local
    ],
)
def test_night_uses_the_transaction_offset(client: TestClient, ts: str, night: bool) -> None:
    d = decide(client, timestamp=ts)
    assert reasons(d) == ([("NEW_MERCHANT_AT_NIGHT", 10)] if night else [])
    assert d["score"] == (10 if night else 0)


@control("EXP-01")
def test_merchant_is_new_per_card(client: TestClient) -> None:
    # R6: new means no earlier record for the same card and merchant.
    night = "2026-11-01T03:00:00+11:00"
    first = decide(client, transaction_id="T-1", timestamp=night)
    assert reasons(first) == [("NEW_MERCHANT_AT_NIGHT", 10)]
    again = decide(client, transaction_id="T-2", timestamp="2026-11-02T03:00:00+11:00")
    assert again["features"]["merchant_seen_before"] is True
    assert reasons(again) == []
    other_card = decide(client, transaction_id="T-3", card_number=CARD_B, timestamp=night)
    assert reasons(other_card) == [("NEW_MERCHANT_AT_NIGHT", 10)]


# ---------------------------------------------------------------- A7, AUD-02


@control("AUD-02")
def test_concurrent_posts_build_one_linked_chain(
    make_client: Callable[..., TestClient], db_path: Path
) -> None:
    # 20 concurrent POSTs through two app instances on the same database file.
    # All use the same card and timestamp, so record k must see k - 1 earlier
    # attempts: features computed outside the write lock would repeat a count.
    clients = [make_client(), make_client()]
    barrier = threading.Barrier(20)

    def post(i: int) -> int:
        barrier.wait()
        body = txn(transaction_id=f"C-{i}", merchant_id=f"M-C-{i}")
        return clients[i % 2].post("/decisions", json=body, headers=ANALYST).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        statuses = list(pool.map(post, range(20)))
    assert statuses == [201] * 20

    rows = db_rows(db_path)
    assert [row[0] for row in rows] == list(range(1, 21))
    prev_hashes = [row[1] for row in rows]
    assert len(set(prev_hashes)) == 20
    assert prev_hashes == [ZERO_HASH] + [row[3] for row in rows[:-1]]
    for record_id, _, payload, _ in rows:
        assert json.loads(payload)["features"]["attempts_in_window"] == record_id
    result = verify(clients[0])
    assert (result["intact"], result["records_checked"]) == (True, 20)


@control("AUD-02")
def test_database_refuses_a_second_record_on_the_same_prev_hash(
    client: TestClient, db_path: Path
) -> None:
    # Plan 2.2: prev_hash is UNIQUE, a second guard behind the write lock.
    decide(client)
    _, prev_hash, payload, _ = db_rows(db_path)[0]
    con = sqlite3.connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO decisions (id, prev_hash, payload, hash) VALUES (2, ?, ?, ?)",
                (prev_hash, payload, "e" * 64),
            )
    finally:
        con.close()


# ---------------------------------------------------------------- A8, DAT-01


def assert_no_card(text: str, card: str) -> None:
    # Plan T29: no 12+ digit run of the card number anywhere.
    for start in range(len(card) - 11):
        assert card[start : start + 12] not in text


@control("DAT-01")
def test_full_card_number_never_leaves_the_boundary(
    client: TestClient,
    db_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    bodies = []
    created = client.post("/decisions", json=txn(card_number=CARD_C), headers=ANALYST)
    assert created.status_code == 201
    assert created.json()["input"]["card_masked"] == "****5556"
    bodies.append(created.text)
    # A 422 for another field: the error must not echo the body with the card.
    bodies.append(
        client.post(
            "/decisions", json=txn(card_number=CARD_C, currency="USD"), headers=ANALYST
        ).text
    )
    missing = txn(card_number=CARD_C)
    del missing["merchant_id"]
    bodies.append(client.post("/decisions", json=missing, headers=ANALYST).text)
    bodies.append(
        client.post("/decisions", json=txn(card_number=CARD_C, extra=CARD_C), headers=ANALYST).text
    )
    bodies.append(client.post("/decisions", json=txn(card_number=CARD_C), headers=AUDITOR).text)
    for path in ("/decisions", "/decisions/1", "/decisions/1/replay", "/audit/verify"):
        bodies.append(client.get(path, headers=AUDITOR).text)

    for body in bodies:
        assert_no_card(body, CARD_C)
    logged = caplog.text + "".join(repr(r.args) for r in caplog.records)
    assert_no_card(logged, CARD_C)
    out, err = capsys.readouterr()
    assert_no_card(out + err, CARD_C)
    # Database file plus any WAL or journal files beside it.
    files = [p for p in db_path.parent.iterdir() if p.name.startswith(db_path.name)]
    assert files
    for path in files:
        assert CARD_C[-12:].encode() not in path.read_bytes()
        assert CARD_C.encode() not in path.read_bytes()


@control("DAT-01")
def test_luhn_invalid_card_is_rejected_without_echo(client: TestClient, db_path: Path) -> None:
    bad = "4111111111111112"  # last digit changed, Luhn check fails
    response = client.post("/decisions", json=txn(card_number=bad), headers=ANALYST)
    assert response.status_code == 422
    assert_no_card(response.text, bad)
    assert db_count(db_path) == 0


@control("DAT-01")
def test_card_ref_is_keyed_and_stable(make_client: Callable[..., TestClient]) -> None:
    # Plan 2.1: card_ref = first 32 hex of HMAC-SHA256(key, card number).
    first = decide(make_client(card_ref_key="key-one"))["input"]["card_ref"]
    again = decide(make_client(card_ref_key="key-one"), transaction_id="T-2")["input"]["card_ref"]
    other = decide(make_client(card_ref_key="key-two"), transaction_id="T-3")["input"]["card_ref"]
    assert first == again
    assert first != other
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != hashlib.sha256(CARD_A.encode()).hexdigest()[:32]


# ---------------------------------------------------------------- VER-01


@control("VER-01")
def test_replay_reports_rule_content_changes(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    # Record 1: GB -> 30 under v1. Then restart with edited v1 files.
    decide(make_client(), merchant_country="GB")
    comment_only = "# a comment that changes no rule\n" + v1_text()
    c = make_client(rules_dir=write_rules(tmp_path / "comment", "v1.yaml", comment_only))
    replay = c.get("/decisions/1/replay", headers=AUDITOR).json()
    assert (replay["rules_match"], replay["match"]) == (True, True)

    edited = v1_text().replace(
        "type: foreign_country,       points: 30", "type: foreign_country,       points: 35"
    )
    assert edited != v1_text()
    c = make_client(rules_dir=write_rules(tmp_path / "edited", "v1.yaml", edited))
    replay = c.get("/decisions/1/replay", headers=AUDITOR).json()
    assert replay["rules_match"] is False
    # Re-run on the loaded v1 content: 35 instead of the stored 30.
    assert replay["match"] is False
    assert (replay["stored"]["score"], replay["replayed"]["score"]) == (30, 35)


@control("VER-01")
def test_highest_version_is_active_and_replay_uses_stored_version(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    decide(make_client(), merchant_country="GB")  # v1: 30, step_up
    rules = tmp_path / "two_versions"
    rules.mkdir()
    shutil.copy(Path(RULES_DIR) / "v1.yaml", rules / "v1.yaml")
    v2 = (
        v1_text()
        .replace("version: 1", "version: 2")
        .replace(
            "type: foreign_country,       points: 30", "type: foreign_country,       points: 50"
        )
    )
    write_rules(rules, "v2.yaml", v2)
    c = make_client(rules_dir=rules)
    d = decide(c, transaction_id="T-2", merchant_country="GB", merchant_id="M-2")
    assert (d["rule_version"], d["score"], d["outcome"]) == (2, 50, "step_up")
    replay = c.get("/decisions/1/replay", headers=AUDITOR).json()
    assert (replay["match"], replay["rules_match"]) == (True, True)
    assert replay["replayed"]["score"] == 30
    old = c.get("/decisions/1", headers=AUDITOR).json()
    assert old["rule_version"] == 1
    assert old["rules_sha256"] != d["rules_sha256"]


@control("VER-01")
def test_replay_of_unloaded_rule_version_is_409(
    make_client: Callable[..., TestClient], tmp_path: Path
) -> None:
    decide(make_client())
    v2_only = write_rules(
        tmp_path / "v2_only", "v2.yaml", v1_text().replace("version: 1", "version: 2")
    )
    c = make_client(rules_dir=v2_only)
    assert c.get("/decisions/1/replay", headers=AUDITOR).status_code == 409


@control("VER-01")
def test_stored_payload_has_exactly_the_planned_fields(client: TestClient, db_path: Path) -> None:
    # Plan 2.3: "Hashed payload (exactly these fields, nothing else)".
    decide(client, merchant_country="GB")
    body = json.loads(payload_of(db_path, 1))
    assert set(body) == {
        "record_version",
        "id",
        "prev_hash",
        "received_at",
        "rule_version",
        "rules_sha256",
        "input",
        "features",
        "score",
        "reasons",
        "outcome",
    }
    assert set(body["input"]) == {
        "transaction_id",
        "card_masked",
        "card_ref",
        "card_status",
        "amount_minor",
        "currency",
        "merchant_id",
        "merchant_country",
        "timestamp",
    }
    assert set(body["features"]) == {"attempts_in_window", "merchant_seen_before"}
    assert body["reasons"] == [{"code": "FOREIGN_COUNTRY", "points": 30}]
    assert (body["rule_version"], body["record_version"]) == (1, 1)
    assert re.fullmatch(r"[0-9a-f]{64}", body["rules_sha256"])
    assert body["received_at"] == "2026-11-01T03:00:01.123456+00:00"  # conftest fixed clock


# ---------------------------------------------------------------- input validation (no control)


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": 0},
        {"amount_minor": -100},
        {"amount_minor": 100_000_001},
        {"amount_minor": 12.5},
        {"amount_minor": "5000"},
        {"currency": "USD"},
        {"currency": "aud"},
        {"timestamp": "2026-11-01T12:00:00"},
        {"timestamp": "not a time"},
        {"merchant_country": "gb"},
        {"merchant_country": "GBR"},
        {"card_status": "lost"},
        {"card_number": "4111 1111 1111 1111"},
        {"card_number": luhn_complete("4" * 10)},  # 11 digits, Luhn-valid
        {"card_number": luhn_complete("4" + "1" * 18)},  # 20 digits
        {"transaction_id": ""},
        {"transaction_id": "T 1"},
        {"transaction_id": "T" * 65},
        {"merchant_id": "M/1"},
        {"unexpected": "field"},
    ],
)
def test_bad_input_is_rejected_and_not_recorded(
    client: TestClient, db_path: Path, overrides: dict[str, Any]
) -> None:
    response = client.post("/decisions", json=txn(**overrides), headers=ANALYST)
    assert response.status_code == 422, response.text
    assert db_count(db_path) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount_minor": 1},
        {"amount_minor": 100_000_000},
        {"card_number": luhn_complete("4" * 11)},  # 12 digits
        {"card_number": luhn_complete("4" + "0" * 17)},  # 19 digits
        {"transaction_id": "T" * 64, "merchant_id": "M_" + "x" * 62},
    ],
)
def test_boundary_input_is_accepted(client: TestClient, overrides: dict[str, Any]) -> None:
    decide(client, **overrides)


def test_unknown_id_is_404(client: TestClient) -> None:
    assert client.get("/decisions/1", headers=AUDITOR).status_code == 404
    assert client.get("/decisions/1/replay", headers=ANALYST).status_code == 404


def test_rejected_requests_are_not_velocity_attempts(client: TestClient) -> None:
    # R7: rejected (422) requests are not attempts. Five valid attempts plus
    # three rejected ones: the fifth valid attempt still counts 5.
    for i in range(4):
        decide(client, transaction_id=f"T-{i}")
    missing = txn()
    del missing["timestamp"]
    for body in (txn(currency="USD"), txn(amount_minor=0), missing):
        assert client.post("/decisions", json=body, headers=ANALYST).status_code == 422
    d = decide(client, transaction_id="T-5")
    assert (d["features"]["attempts_in_window"], d["score"]) == (5, 0)
