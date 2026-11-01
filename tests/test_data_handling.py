"""Card data handling (control DAT-01, criterion A8). Expected values come from
docs/plan.md section 2.1: card_masked is "****" plus the last 4 digits, and
card_ref is the first 32 hex characters of HMAC-SHA256(CARD_REF_KEY, card number)."""

import hashlib
import hmac
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import engine
from app.models import TransactionRequest
from tests.conftest import TEST_CARD_REF_KEY
from tests.controls import control

ANALYST = {"X-Role": "analyst"}
AUDITOR = {"X-Role": "auditor"}
CARD = "4539578763621486"  # synthetic, Luhn-valid
LUHN_INVALID = "4539578763621487"

VALID: dict[str, Any] = {
    "transaction_id": "T-0001",
    "card_number": CARD,
    "card_status": "active",
    "amount_minor": 5_000,
    "currency": "AUD",
    "merchant_id": "M-1000",
    "merchant_country": "AU",
    "timestamp": "2026-11-01T14:00:00+11:00",
}


def twelve_digit_runs(number: str) -> list[str]:
    """Every 12-digit run of the card number: none may appear anywhere."""
    return [number[start : start + 12] for start in range(len(number) - 11)]


def database_bytes(db_path: Path) -> bytes:
    return b"".join(
        path.read_bytes() for path in db_path.parent.iterdir() if path.name.startswith(db_path.name)
    )


@control("DAT-01")
def test_t29_full_card_number_never_reaches_logs_responses_or_database(
    client: TestClient, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)

    created = client.post("/decisions", json=VALID, headers=ANALYST)
    responses = [
        created,
        client.get("/decisions/1", headers=AUDITOR),
        client.get("/decisions", headers=AUDITOR),
        client.get("/decisions/1/replay", headers=AUDITOR),
    ]

    assert created.status_code == 201
    assert created.json()["input"]["card_masked"] == "****1486"
    texts = {
        "logs": caplog.text,
        "responses": "".join(response.text for response in responses),
        "database": database_bytes(db_path).decode("latin-1"),
    }
    for where, text in texts.items():
        for run in twelve_digit_runs(CARD):
            assert run not in text, f"{where} holds part of the card number"
    assert "****1486" in caplog.text


@control("DAT-01")
def test_t30_luhn_invalid_card_gives_422_without_echoing_it(client: TestClient) -> None:
    response = client.post(
        "/decisions", json={**VALID, "card_number": LUHN_INVALID}, headers=ANALYST
    )

    assert response.status_code == 422
    for run in twelve_digit_runs(LUHN_INVALID):
        assert run not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "card_number"]


@control("DAT-01")
def test_t30_validation_errors_never_echo_input(client: TestClient) -> None:
    response = client.post(
        "/decisions",
        json={**VALID, "card_number": f"x{CARD}", "merchant_country": "Wonderland"},
        headers=ANALYST,
    )

    assert response.status_code == 422
    assert "Wonderland" not in response.text
    for run in twelve_digit_runs(CARD):
        assert run not in response.text


@control("DAT-01")
def test_t30_unexpected_field_named_with_a_card_number_is_not_echoed(client: TestClient) -> None:
    response = client.post("/decisions", json={**VALID, CARD: "x"}, headers=ANALYST)

    assert response.status_code == 422
    for run in twelve_digit_runs(CARD):
        assert run not in response.text
    assert response.json()["detail"] == [
        {"loc": ["body"], "msg": "Extra inputs are not permitted", "type": "extra_forbidden"}
    ]


@control("DAT-01")
def test_t30_documented_422_body_has_no_input_fields(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    create = schema["paths"]["/decisions"]["post"]["responses"]["422"]
    name = create["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]
    issue = schema["components"]["schemas"][name]["properties"]["detail"]["items"]["$ref"]

    fields = schema["components"]["schemas"][issue.rsplit("/", 1)[-1]]["properties"]
    assert set(fields) == {"loc", "msg", "type"}


@control("DAT-01")
def test_t31_card_is_masked_and_referenced_by_keyed_hash() -> None:
    request = TransactionRequest.model_validate({**VALID, "card_number": "4111111111111111"})

    record = request.to_record(TEST_CARD_REF_KEY)
    again = request.to_record(TEST_CARD_REF_KEY)
    other_key = request.to_record("another-key")

    expected_ref = hmac.new(
        TEST_CARD_REF_KEY.encode(), b"4111111111111111", hashlib.sha256
    ).hexdigest()[:32]
    assert record.card_masked == "****1111"
    assert record.card_ref == expected_ref == again.card_ref
    assert other_key.card_ref != record.card_ref
    assert "4111111111111111" not in repr(request)


@control("DAT-01")
def test_t44_rule_error_logs_rule_code_and_class_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def leaky_rule(rule: Any, txn: Any, *args: Any) -> bool:
        raise ValueError(f"cannot score {txn.merchant_id} amount {txn.amount_minor}")

    monkeypatch.setitem(engine.SCORED_RULE_CHECKS, "amount_at_least", leaky_rule)
    caplog.set_level(logging.DEBUG)

    response = client.post(
        "/decisions",
        json={**VALID, "merchant_id": "M-SECRET-77", "amount_minor": 4_321},
        headers=ANALYST,
    )

    assert response.json()["reasons"] == [
        {"code": "RULE_ERROR", "points": None, "rule": "UNUSUAL_AMOUNT"}
    ]
    warnings = [
        record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert any("UNUSUAL_AMOUNT" in message and "ValueError" in message for message in warnings)
    assert "M-SECRET-77" not in caplog.text
    assert "4321" not in caplog.text
    assert "cannot score" not in caplog.text


@control("DAT-01")
@pytest.mark.parametrize("field", ["transaction_id", "merchant_id"])
def test_card_number_in_identifier_fields_is_rejected(
    client: TestClient, db_path: Path, field: str
) -> None:
    # Red team R4: identifiers are stored and returned in clear, so they must not
    # carry the card number, whole or embedded.
    for value in (CARD, f"T-{CARD}-X"):
        response = client.post("/decisions", json={**VALID, field: value}, headers=ANALYST)

        assert response.status_code == 422, value
        for run in twelve_digit_runs(CARD):
            assert run not in response.text
    assert not db_path.exists() or CARD[:12].encode() not in database_bytes(db_path)
    assert client.get("/decisions", headers=AUDITOR).json() == []
