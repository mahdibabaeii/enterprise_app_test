"""Decision API and roles. Expected values come by hand from docs/PRD.md and
docs/plan.md sections 2.1, 2.5 and 3 (endpoints, roles, status codes), using the
v1 rules: foreign +30, amount >= 1,000.00 AUD +25, more than 5 attempts in 60 s
+40, new merchant 00:00-05:59 local +10; thresholds 30 and 70."""

import shutil
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import store
from app.main import create_app
from app.rules import RULES_DIR
from tests.controls import control

ANALYST = {"X-Role": "analyst"}
AUDITOR = {"X-Role": "auditor"}
CARD = "4539578763621486"  # synthetic, Luhn-valid

# Domestic, small, daytime (14:00 local): scores 0 under v1 for a first attempt.
QUIET: dict[str, Any] = {
    "transaction_id": "T-0001",
    "card_number": CARD,
    "card_status": "active",
    "amount_minor": 5_000,
    "currency": "AUD",
    "merchant_id": "M-1000",
    "merchant_country": "AU",
    "timestamp": "2026-11-01T14:00:00+11:00",
}

# The PRD example: 2,000 AUD in GB at 03:00 local, new merchant: 30 + 25 + 10 = 65.
PRD_EXAMPLE = {
    **QUIET,
    "transaction_id": "T-0007",
    "amount_minor": 200_000,
    "merchant_id": "M-1234",
    "merchant_country": "GB",
    "timestamp": "2026-11-01T03:00:00+00:00",
}
PRD_REASONS = [
    {"code": "FOREIGN_COUNTRY", "points": 30, "rule": None},
    {"code": "UNUSUAL_AMOUNT", "points": 25, "rule": None},
    {"code": "NEW_MERCHANT_AT_NIGHT", "points": 10, "rule": None},
]


def body(**overrides: Any) -> dict[str, Any]:
    return {**QUIET, **overrides}


def record_count(db_path: Path) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute("SELECT count(*) FROM decisions").fetchone()[0]
    finally:
        connection.close()


def copy_rules(tmp_path: Path) -> Path:
    rules_dir = tmp_path / "rules"
    shutil.copytree(RULES_DIR, rules_dir)
    return rules_dir


# A1: replay


@control("REP-01")
def test_t03_post_then_replay_matches(client: TestClient) -> None:
    created = client.post("/decisions", json=PRD_EXAMPLE, headers=ANALYST)

    assert created.status_code == 201
    decision = created.json()
    assert (decision["id"], decision["score"], decision["outcome"]) == (1, 65, "step_up")
    assert decision["reasons"] == PRD_REASONS
    assert decision["thresholds"] == {"step_up": 30, "decline": 70}
    assert decision["input"]["card_masked"] == "****1486"
    assert "card_number" not in decision["input"]
    assert decision["received_at"] == "2026-11-01T03:00:01.123456+00:00"

    replay = client.get("/decisions/1/replay", headers=AUDITOR)

    assert replay.status_code == 200
    stored = {
        "features": {"attempts_in_window": 1, "merchant_seen_before": False},
        "score": 65,
        "reasons": PRD_REASONS,
        "outcome": "step_up",
    }
    assert replay.json() == {
        "id": 1,
        "match": True,
        "rules_match": True,
        "features_match": True,
        "stored": stored,
        "replayed": stored,
    }


@control("REP-01")
def test_t42_replay_ignores_received_at(make_client: Callable[..., TestClient]) -> None:
    make_client().post("/decisions", json=PRD_EXAMPLE, headers=ANALYST)
    later = make_client(clock=lambda: datetime(2027, 6, 1, 23, 0, tzinfo=UTC))

    replay = later.get("/decisions/1/replay", headers=ANALYST).json()

    assert (replay["match"], replay["rules_match"], replay["features_match"]) == (True, True, True)


@control("REP-01")
def test_replay_recomputes_features_from_earlier_records_only(client: TestClient) -> None:
    client.post("/decisions", json=body(transaction_id="T-1"), headers=ANALYST)
    client.post("/decisions", json=body(transaction_id="T-2"), headers=ANALYST)
    # A later record for the same card and merchant must not change record 1's history.
    client.post("/decisions", json=body(transaction_id="T-3"), headers=ANALYST)

    first = client.get("/decisions/1/replay", headers=ANALYST).json()
    second = client.get("/decisions/2/replay", headers=ANALYST).json()

    assert first["replayed"]["features"] == {"attempts_in_window": 1, "merchant_seen_before": False}
    assert second["replayed"]["features"] == {"attempts_in_window": 2, "merchant_seen_before": True}
    assert first["features_match"] and second["features_match"]


@control("VER-01")
def test_t05_rule_content_edited_under_the_same_version_is_reported(
    tmp_path: Path, make_client: Callable[..., TestClient]
) -> None:
    rules_dir = copy_rules(tmp_path)
    make_client(rules_dir=rules_dir).post("/decisions", json=PRD_EXAMPLE, headers=ANALYST)
    rules_file = rules_dir / "v1.yaml"
    rules_file.write_text(rules_file.read_text().replace("points: 25", "points: 26"))

    replay = make_client(rules_dir=rules_dir).get("/decisions/1/replay", headers=AUDITOR).json()

    assert replay["rules_match"] is False
    assert replay["match"] is False
    assert replay["replayed"]["score"] == 66


@control("VER-01")
def test_t38_comment_only_rule_edit_still_matches(
    tmp_path: Path, make_client: Callable[..., TestClient]
) -> None:
    rules_dir = copy_rules(tmp_path)
    make_client(rules_dir=rules_dir).post("/decisions", json=PRD_EXAMPLE, headers=ANALYST)
    rules_file = rules_dir / "v1.yaml"
    rules_file.write_text("# reviewed, no change\n" + rules_file.read_text() + "\n# end\n")

    replay = make_client(rules_dir=rules_dir).get("/decisions/1/replay", headers=AUDITOR).json()

    assert (replay["match"], replay["rules_match"]) == (True, True)


# A3: roles


@control("ACC-01")
def test_t12_auditor_cannot_create_a_decision(client: TestClient, db_path: Path) -> None:
    response = client.post("/decisions", json=body(), headers=AUDITOR)

    assert response.status_code == 403
    assert "auditor" in response.json()["detail"]
    assert record_count(db_path) == 0


@control("ACC-01")
@pytest.mark.parametrize(
    "headers", [{}, {"X-Role": "admin"}, {"X-Role": "Analyst"}, {"X-Role": ""}]
)
def test_t13_missing_or_unknown_role_cannot_create(
    client: TestClient, db_path: Path, headers: dict[str, str]
) -> None:
    response = client.post("/decisions", json=body(), headers=headers)

    assert response.status_code == 401
    assert record_count(db_path) == 0


# (method, path) -> expected status for analyst, auditor and no role. Written
# from plan section 3; every route the app serves must appear here.
ROLE_MATRIX: dict[tuple[str, str], tuple[int, int, int]] = {
    ("POST", "/decisions"): (201, 403, 401),
    ("GET", "/decisions"): (200, 200, 401),
    ("GET", "/decisions/{decision_id}"): (200, 200, 401),
    ("GET", "/decisions/{decision_id}/replay"): (200, 200, 401),
    ("GET", "/audit/verify"): (403, 200, 401),
    ("GET", "/"): (200, 200, 200),
    ("GET", "/docs"): (200, 200, 200),
    ("GET", "/openapi.json"): (200, 200, 200),
}


@control("ACC-01")
def test_t14_role_matrix_covers_every_route(client: TestClient) -> None:
    served = {
        (method, route.path)
        for route in client.app.routes
        for method in getattr(route, "methods", None) or {"GET"}
        if method != "HEAD"
    }
    assert served == set(ROLE_MATRIX)

    client.post("/decisions", json=body(), headers=ANALYST)
    for (method, path), expected in ROLE_MATRIX.items():
        url = path.replace("{decision_id}", "1")
        for headers, status in zip((ANALYST, AUDITOR, {}), expected, strict=True):
            payload = body(transaction_id="T-MATRIX") if method == "POST" else None
            response = client.request(method, url, json=payload, headers=headers)
            assert response.status_code == status, (method, path, headers)


@control("ACC-01")
def test_route_without_an_allowlist_entry_refuses_every_role(db_path: Path) -> None:
    app = create_app(db_path)

    @app.get("/unlisted")
    def unlisted() -> dict[str, bool]:
        return {"reached": True}

    with TestClient(app) as client:
        assert client.get("/unlisted", headers=ANALYST).status_code == 403
        assert client.get("/unlisted", headers=AUDITOR).status_code == 403
        assert client.get("/unlisted").status_code == 401


# A4: fail safe through the API


@control("FS-01")
def test_t18_history_failure_gives_recorded_step_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_history(*args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "read_features", broken_history)

    response = client.post("/decisions", json=body(), headers=ANALYST)

    assert response.status_code == 201
    decision = response.json()
    assert (decision["outcome"], decision["features"]) == ("step_up", None)
    assert decision["reasons"] == [{"code": "HISTORY_UNAVAILABLE", "points": None, "rule": None}]
    stored = client.get("/decisions/1", headers=AUDITOR).json()
    assert (stored["outcome"], stored["features"]) == ("step_up", None)
    assert client.get("/audit/verify", headers=AUDITOR).json()["intact"] is True


@control("REP-01")
@control("FS-01")
def test_t45_failed_safe_decision_replays_as_a_match(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_history(*args: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "read_features", broken_history)
    client.post("/decisions", json=body(), headers=ANALYST)
    monkeypatch.undo()

    replay = client.get("/decisions/1/replay", headers=AUDITOR).json()

    assert (replay["match"], replay["rules_match"], replay["features_match"]) == (True, True, None)
    assert replay["replayed"]["outcome"] == "step_up"


@control("FS-01")
@pytest.mark.parametrize("failure", ["busy timeout", "integrity error"])
def test_t43_write_failure_gives_503_and_no_decision(
    client: TestClient, db_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    blocker = sqlite3.connect(db_path, isolation_level=None)
    if failure == "busy timeout":
        monkeypatch.setattr(store, "BUSY_TIMEOUT_SECONDS", 0.1)
        blocker.execute("BEGIN IMMEDIATE")
    else:
        blocker.execute(
            "CREATE TRIGGER refuse BEFORE INSERT ON decisions"
            " BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
    try:
        response = client.post("/decisions", json=body(), headers=ANALYST)
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()

    assert response.status_code == 503
    assert set(response.json()) == {"detail"}
    assert "step-up" in response.json()["detail"]
    assert record_count(db_path) == 0


# A7: concurrency through the API


@control("AUD-02")
def test_t27_concurrent_posts_form_one_linked_chain(client: TestClient, db_path: Path) -> None:
    statuses: list[int] = []
    start = threading.Barrier(20)

    def post(n: int) -> None:
        start.wait()
        response = client.post(
            "/decisions", json=body(transaction_id=f"T-C{n:02d}"), headers=ANALYST
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=post, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == [201] * 20
    connection = sqlite3.connect(db_path)
    rows = connection.execute("SELECT id, prev_hash, hash FROM decisions ORDER BY id").fetchall()
    connection.close()
    assert [row[0] for row in rows] == list(range(1, 21))
    assert len({row[1] for row in rows}) == 20
    assert all(rows[n][1] == rows[n - 1][2] for n in range(1, 20))
    assert client.get("/audit/verify", headers=AUDITOR).json() == {
        "intact": True,
        "records_checked": 20,
        "first_broken_id": None,
        "reason": None,
    }


# Input validation and lookups


@pytest.mark.parametrize(
    ("case", "overrides"),
    [
        ("negative amount", {"amount_minor": -1}),
        ("zero amount", {"amount_minor": 0}),
        ("amount above limit", {"amount_minor": 100_000_001}),
        ("fractional amount", {"amount_minor": 50.5}),
        ("amount as text", {"amount_minor": "5000"}),
        ("non-AUD currency", {"currency": "USD"}),
        ("timestamp without offset", {"timestamp": "2026-11-01T14:00:00"}),
        ("timestamp as epoch number", {"timestamp": 1793502000}),
        ("timestamp as epoch text", {"timestamp": "1793502000"}),
        ("timestamp as fractional epoch text", {"timestamp": "1793502000.5"}),
        ("timestamp before 1970", {"timestamp": "0001-01-01T00:00:30+00:00"}),
        ("extra field", {"cvv": "123"}),
        ("non-digit card", {"card_number": "4539-5787-6362-1486"}),
        ("short card", {"card_number": "45395787636"}),
        ("lowercase country", {"merchant_country": "gb"}),
        ("bad transaction id", {"transaction_id": "T 0001"}),
        ("unknown card status", {"card_status": "lost"}),
    ],
)
def test_t34_bad_input_gives_422(
    client: TestClient, db_path: Path, case: str, overrides: dict[str, Any]
) -> None:
    response = client.post("/decisions", json=body(**overrides), headers=ANALYST)

    assert response.status_code == 422, case
    assert record_count(db_path) == 0


def test_t34_missing_field_gives_422(client: TestClient) -> None:
    incomplete = body()
    del incomplete["merchant_id"]

    assert client.post("/decisions", json=incomplete, headers=ANALYST).status_code == 422


@pytest.mark.parametrize("path", ["/decisions/1", "/decisions/1/replay", "/decisions/99"])
def test_t35_unknown_id_gives_404(client: TestClient, path: str) -> None:
    response = client.get(path, headers=AUDITOR)

    assert response.status_code == 404
    assert response.json() == {"detail": "decision not found"}


def test_list_returns_the_latest_20_newest_first(client: TestClient) -> None:
    for n in range(1, 23):
        client.post("/decisions", json=body(transaction_id=f"T-{n:02d}"), headers=ANALYST)

    listed = client.get("/decisions", headers=AUDITOR).json()

    assert [decision["id"] for decision in listed] == list(range(22, 2, -1))
    assert listed[0]["input"]["transaction_id"] == "T-22"


def test_get_returns_the_stored_decision(client: TestClient) -> None:
    created = client.post("/decisions", json=PRD_EXAMPLE, headers=ANALYST).json()

    fetched = client.get("/decisions/1", headers=AUDITOR).json()

    assert fetched == {key: value for key, value in created.items() if key != "thresholds"}
