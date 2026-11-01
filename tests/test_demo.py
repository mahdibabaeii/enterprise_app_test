"""Demo seed, tamper command and start (plan step 9, T32, criterion A9).

Expected values come from docs/plan.md: the demo database is data/demo.db,
`make demo` replaces it and reseeds through the real append path (R28), and
`make tamper` alters one record in data/demo.db only, whether or not triggers
exist (plan 2.4), so that verify names that record."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import DEFAULT_DB, create_app
from app.store import Store
from scripts import seed, tamper
from tests.controls import control

AUDITOR = {"X-Role": "auditor"}
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def demo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stand-in for data/demo.db, so tests never touch the real demo database."""
    path = tmp_path / "data" / "demo.db"
    monkeypatch.setattr(tamper, "DEMO_DB", path)
    seed.seed(path)
    return path


def payload_outcome(path: Path, record_id: int) -> str:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT json_extract(payload, '$.outcome') FROM decisions WHERE id = ?", (record_id,)
        ).fetchone()[0]
    finally:
        connection.close()


def test_demo_database_is_data_demo_db_in_the_repository() -> None:
    assert DEFAULT_DB == REPO_ROOT / "data" / "demo.db"
    assert tamper.DEMO_DB == DEFAULT_DB


def test_t32_app_starts_on_a_fresh_seeded_database(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    count = seed.seed(path)

    with TestClient(create_app(path)) as client:
        home = client.get("/")
        docs = client.get("/docs")
        listed = client.get("/decisions", headers=AUDITOR).json()
        verified = client.get("/audit/verify", headers=AUDITOR).json()

    assert home.status_code == 200
    assert home.headers["content-type"].startswith("text/html")
    assert docs.status_code == 200
    assert count == len(seed.SAMPLES) >= 8
    assert len(listed) == count
    assert verified["intact"] is True
    assert verified["records_checked"] == count
    assert {decision["outcome"] for decision in listed} == {"approve", "step_up", "decline"}


def test_seed_runs_every_sample_through_the_real_rules(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    seed.seed(path)
    decisions = [Store(path).get(n) for n in range(1, len(seed.SAMPLES) + 1)]
    codes = {reason["code"] for decision in decisions for reason in decision["reasons"]}

    # Every reason code in rules/v1.yaml appears in the demo data.
    assert {
        "CARD_REPORTED_STOLEN",
        "MERCHANT_BLOCKLISTED",
        "FOREIGN_COUNTRY",
        "UNUSUAL_AMOUNT",
        "HIGH_VELOCITY",
        "NEW_MERCHANT_AT_NIGHT",
    } <= codes
    # The PRD example is in the demo: 2,000 AUD overseas at 3am, new merchant, 65 step-up.
    assert any(d["score"] == 65 and d["outcome"] == "step_up" for d in decisions)
    # make tamper's default record is a decline, so the edit turns it into an approve.
    assert decisions[tamper.DEFAULT_RECORD - 1]["outcome"] == "decline"


def test_seed_replaces_the_database_instead_of_appending(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    seed.seed(path)
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("UPDATE decisions SET payload = '{}' WHERE id = 2")
    connection.close()
    assert not Store(path).verify().intact

    count = seed.seed(path)

    result = Store(path).verify()
    assert (result.intact, result.records_checked) == (True, count)


@control("AUD-01")
def test_tamper_alters_one_record_and_verify_names_it(
    demo_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert Store(demo_db).verify().intact

    assert tamper.main([]) == 0

    result = Store(demo_db).verify()
    assert (result.intact, result.first_broken_id) == (False, tamper.DEFAULT_RECORD)
    assert payload_outcome(demo_db, tamper.DEFAULT_RECORD) == "approve"
    assert f"record {tamper.DEFAULT_RECORD}" in capsys.readouterr().out


@control("AUD-01")
def test_tamper_takes_a_record_number(demo_db: Path) -> None:
    assert tamper.main(["5"]) == 0

    assert Store(demo_db).verify().first_broken_id == 5


@control("AUD-01")
def test_tamper_bypasses_and_restores_triggers(demo_db: Path) -> None:
    trigger = (
        "CREATE TRIGGER decisions_no_update BEFORE UPDATE ON decisions"
        " BEGIN SELECT RAISE(ABORT, 'append-only'); END"
    )
    connection = sqlite3.connect(demo_db, isolation_level=None)
    connection.execute(trigger)
    connection.close()

    assert tamper.main([]) == 0

    assert Store(demo_db).verify().first_broken_id == tamper.DEFAULT_RECORD
    connection = sqlite3.connect(demo_db)
    restored = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'decisions_no_update'"
    ).fetchall()
    connection.close()
    assert restored == [(trigger,)]


def test_tamper_refuses_any_database_but_the_demo_one(demo_db: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.db"
    seed.seed(other)
    before = other.read_bytes()

    with pytest.raises(tamper.TamperRefused):
        tamper.tamper(other, 3)

    assert other.read_bytes() == before
    assert Store(other).verify().intact


@pytest.mark.parametrize("argv", [["999"], ["0"], ["three"]])
def test_tamper_rejects_a_record_that_does_not_exist(
    demo_db: Path, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert tamper.main(argv) == 1
    assert Store(demo_db).verify().intact
    assert capsys.readouterr().err


def test_tamper_without_a_demo_database_says_how_to_create_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tamper, "DEMO_DB", tmp_path / "missing.db")

    assert tamper.main([]) == 1
    assert "make demo" in capsys.readouterr().err
    assert not (tmp_path / "missing.db").exists()
