"""Decision engine. Expected values are worked out by hand from docs/PRD.md and
docs/plan.md section 3, using the v1 values: foreign +30, amount >= 1,000.00 AUD
+25, more than 5 attempts in the window +40, new merchant 00:00-05:59 +10;
under 30 approve, 30-69 step_up, 70 or more decline."""

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest

from app import engine
from app.engine import Decision, Features, Transaction, decide, outcome_for_score
from app.rules import RuleSet, active_rule_set, load_rule_sets
from tests.controls import control

V1 = active_rule_set(load_rule_sets())

# Domestic, small, daytime, known merchant, first attempt: scores 0 under v1.
QUIET_TXN: dict[str, Any] = {
    "card_status": "active",
    "amount_minor": 5_000,
    "merchant_id": "M-1000",
    "merchant_country": "AU",
    "timestamp": "2026-11-01T14:00:00+11:00",
}
QUIET_FEATURES = Features(attempts_in_window=1, merchant_seen_before=True)


def txn(**overrides: Any) -> Transaction:
    return Transaction.model_validate({**QUIET_TXN, **overrides})


def features(**overrides: Any) -> Features:
    return QUIET_FEATURES.model_copy(update=overrides)


def reason(code: str, points: int | None, rule: str | None = None) -> dict[str, Any]:
    return {"code": code, "points": points, "rule": rule}


def dump(decision: Decision) -> dict[str, Any]:
    return decision.model_dump()


def raise_error(*args: Any) -> bool:
    raise RuntimeError("rule blew up")


@pytest.fixture
def restore_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    yield monkeypatch
    monkeypatch.undo()
    time.tzset()


# A1: determinism


@control("REP-01")
def test_t01_same_input_and_rule_version_give_the_same_decision() -> None:
    prd_example = {
        "card_status": "active",
        "amount_minor": 200_000,
        "merchant_id": "M-2000",
        "merchant_country": "GB",
        "timestamp": "2026-11-01T03:00:00+00:00",
    }
    shuffled = dict(reversed(list(prd_example.items())))
    history = Features(attempts_in_window=1, merchant_seen_before=False)

    first = decide(Transaction.model_validate(prd_example), history, V1)
    second = decide(Transaction.model_validate(prd_example), history, V1)
    from_shuffled = decide(Transaction.model_validate(shuffled), history, V1)

    assert first == second == from_shuffled
    assert first.outcome == "step_up"


@control("REP-01")
def test_t02_night_rule_uses_the_transaction_offset_not_the_server_timezone(
    restore_tz: pytest.MonkeyPatch,
) -> None:
    # 03:00 at UTC-05:00 is night for the merchant, but 08:00 UTC and 19:00 in
    # Melbourne, so reading the server clock or timezone would miss it.
    us_night = txn(merchant_country="US", timestamp="2026-11-01T03:00:00-05:00")
    new_merchant = features(merchant_seen_before=False)
    expected = {
        "score": 40,
        "reasons": [reason("FOREIGN_COUNTRY", 30), reason("NEW_MERCHANT_AT_NIGHT", 10)],
        "outcome": "step_up",
    }

    decisions = []
    for zone in ("UTC", "Australia/Melbourne"):
        restore_tz.setenv("TZ", zone)
        time.tzset()
        decisions.append(dump(decide(us_night, new_merchant, V1)))

    assert decisions == [expected, expected]


# A4: fail safe


@control("FS-01")
def test_t15_scored_rule_error_on_zero_score_gives_step_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(engine.SCORED_RULE_CHECKS, "amount_at_least", raise_error)

    decision = decide(txn(), QUIET_FEATURES, V1)

    assert dump(decision) == {
        "score": 0,
        "reasons": [reason("RULE_ERROR", None, rule="UNUSUAL_AMOUNT")],
        "outcome": "step_up",
    }


@control("FS-01")
def test_t16_history_unavailable_gives_step_up() -> None:
    decision = decide(txn(), None, V1)

    assert dump(decision) == {
        "score": 0,
        "reasons": [reason("HISTORY_UNAVAILABLE", None)],
        "outcome": "step_up",
    }


@control("FS-01")
def test_t16_history_unavailable_skips_rules_that_need_history() -> None:
    # Foreign and large still score from the transaction itself: 30 + 25 = 55.
    decision = decide(
        txn(merchant_country="GB", amount_minor=200_000, timestamp="2026-11-01T03:00:00+00:00"),
        None,
        V1,
    )

    assert dump(decision) == {
        "score": 55,
        "reasons": [
            reason("FOREIGN_COUNTRY", 30),
            reason("UNUSUAL_AMOUNT", 25),
            reason("HISTORY_UNAVAILABLE", None),
        ],
        "outcome": "step_up",
    }


@control("FS-01")
def test_t17_missing_history_never_downgrades_a_hard_rule_decline() -> None:
    decision = decide(txn(card_status="reported_stolen"), None, V1)

    assert decision.outcome == "decline"


@control("FS-01")
def test_t17_rule_error_never_downgrades_a_score_decline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(engine.SCORED_RULE_CHECKS, "amount_at_least", raise_error)

    # Foreign 30 + velocity 40 = 70 even with the amount rule failing.
    decision = decide(
        txn(merchant_country="GB", amount_minor=200_000),
        features(attempts_in_window=6),
        V1,
    )

    assert dump(decision) == {
        "score": 70,
        "reasons": [
            reason("FOREIGN_COUNTRY", 30),
            reason("RULE_ERROR", None, rule="UNUSUAL_AMOUNT"),
            reason("HIGH_VELOCITY", 40),
        ],
        "outcome": "decline",
    }


@control("FS-01")
def test_t41_hard_rule_error_gives_step_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(engine.HARD_RULE_CHECKS, "card_status_is", raise_error)

    decision = decide(txn(), QUIET_FEATURES, V1)

    assert dump(decision) == {
        "score": 0,
        "reasons": [reason("RULE_ERROR", None, rule="CARD_REPORTED_STOLEN")],
        "outcome": "step_up",
    }


@control("FS-01")
def test_t41_later_matching_hard_rule_still_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(engine.HARD_RULE_CHECKS, "card_status_is", raise_error)

    decision = decide(txn(merchant_id="M-BLOCK-001"), QUIET_FEATURES, V1)

    assert dump(decision) == {
        "score": None,
        "reasons": [reason("MERCHANT_BLOCKLISTED", None)],
        "outcome": "decline",
    }


# A5: thresholds


@control("EXP-01")
@pytest.mark.parametrize(
    ("score", "outcome"),
    [
        (0, "approve"),
        (29, "approve"),
        (30, "step_up"),
        (69, "step_up"),
        (70, "decline"),
        (105, "decline"),
    ],
)
def test_t20_threshold_mapping_with_v1_thresholds(score: int, outcome: str) -> None:
    assert outcome_for_score(score, V1.thresholds) == outcome


EDGE_RULE_SET = RuleSet.model_validate(
    {
        "version": 99,
        "thresholds": {"step_up": 30, "decline": 70},
        "home_country": "AU",
        "hard_rules": [],
        "scored_rules": [
            {"code": "FOREIGN_COUNTRY", "type": "foreign_country", "points": 29},
            {
                "code": "UNUSUAL_AMOUNT",
                "type": "amount_at_least",
                "amount_minor": 100_000,
                "points": 1,
            },
            {
                "code": "HIGH_VELOCITY",
                "type": "attempts_over",
                "window_seconds": 60,
                "max_attempts": 5,
                "points": 40,
            },
        ],
    }
)


@control("EXP-01")
@pytest.mark.parametrize(
    ("amount_minor", "attempts", "score", "outcome"),
    [
        (5_000, 1, 29, "approve"),
        (100_000, 1, 30, "step_up"),
        (5_000, 6, 69, "step_up"),
        (100_000, 6, 70, "decline"),
    ],
)
def test_t21_reachable_threshold_edges_end_to_end(
    amount_minor: int, attempts: int, score: int, outcome: str
) -> None:
    decision = decide(
        txn(merchant_country="NZ", amount_minor=amount_minor),
        features(attempts_in_window=attempts),
        EDGE_RULE_SET,
    )

    assert (decision.score, decision.outcome) == (score, outcome)


@control("EXP-01")
def test_t22_prd_example_overseas_large_night_purchase() -> None:
    # PRD: a 2,000 AUD purchase from overseas at 3am scores 30 + 25 + 10 = 65.
    decision = decide(
        txn(merchant_country="GB", amount_minor=200_000, timestamp="2026-11-01T03:00:00+00:00"),
        features(merchant_seen_before=False),
        V1,
    )

    assert dump(decision) == {
        "score": 65,
        "reasons": [
            reason("FOREIGN_COUNTRY", 30),
            reason("UNUSUAL_AMOUNT", 25),
            reason("NEW_MERCHANT_AT_NIGHT", 10),
        ],
        "outcome": "step_up",
    }


@control("EXP-01")
@pytest.mark.parametrize(
    ("overrides", "history"),
    [
        ({}, {}),
        ({"merchant_country": "GB"}, {}),
        ({"amount_minor": 150_000}, {"attempts_in_window": 7}),
        (
            {"merchant_country": "US", "timestamp": "2026-11-01T01:30:00-08:00"},
            {"merchant_seen_before": False},
        ),
        (
            {"merchant_country": "GB", "amount_minor": 999_999},
            {"attempts_in_window": 9, "merchant_seen_before": False},
        ),
    ],
)
def test_t23_reason_points_sum_to_the_score(
    overrides: dict[str, Any], history: dict[str, Any]
) -> None:
    decision = decide(txn(**overrides), features(**history), V1)

    assert decision.score == sum(r.points for r in decision.reasons if r.points is not None)


# A6: hard rules


@control("EXP-01")
def test_t24_stolen_card_declines_despite_zero_score() -> None:
    decision = decide(txn(card_status="reported_stolen"), QUIET_FEATURES, V1)

    assert dump(decision) == {
        "score": None,
        "reasons": [reason("CARD_REPORTED_STOLEN", None)],
        "outcome": "decline",
    }


@control("EXP-01")
def test_t25_blocklisted_merchant_declines() -> None:
    decision = decide(txn(merchant_id="M-BLOCK-002"), QUIET_FEATURES, V1)

    assert dump(decision) == {
        "score": None,
        "reasons": [reason("MERCHANT_BLOCKLISTED", None)],
        "outcome": "decline",
    }


# Scored rule edges (plan R4, R5, R7, D4)


@control("EXP-01")
@pytest.mark.parametrize(("attempts", "fires"), [(5, False), (6, True)])
def test_velocity_fires_on_the_sixth_attempt(attempts: int, fires: bool) -> None:
    decision = decide(txn(), features(attempts_in_window=attempts), V1)

    assert dump(decision)["reasons"] == ([reason("HIGH_VELOCITY", 40)] if fires else [])


@control("EXP-01")
@pytest.mark.parametrize(("amount_minor", "fires"), [(99_999, False), (100_000, True)])
def test_unusual_amount_starts_at_one_thousand_dollars(amount_minor: int, fires: bool) -> None:
    decision = decide(txn(amount_minor=amount_minor), QUIET_FEATURES, V1)

    assert dump(decision)["reasons"] == ([reason("UNUSUAL_AMOUNT", 25)] if fires else [])


@control("EXP-01")
@pytest.mark.parametrize(
    ("local_time", "seen_before", "fires"),
    [
        ("00:00:00", False, True),
        ("05:59:59", False, True),
        ("06:00:00", False, False),
        ("23:59:59", False, False),
        ("03:00:00", True, False),
    ],
)
def test_new_merchant_at_night_window(local_time: str, seen_before: bool, fires: bool) -> None:
    decision = decide(
        txn(timestamp=f"2026-11-01T{local_time}+11:00"),
        features(merchant_seen_before=seen_before),
        V1,
    )

    assert dump(decision)["reasons"] == ([reason("NEW_MERCHANT_AT_NIGHT", 10)] if fires else [])


@control("EXP-01")
def test_home_country_merchant_is_not_foreign() -> None:
    assert decide(txn(merchant_country="AU"), QUIET_FEATURES, V1).reasons == []
    assert decide(txn(merchant_country="NZ"), QUIET_FEATURES, V1).score == 30


def test_transaction_requires_a_timestamp_offset() -> None:
    with pytest.raises(ValueError):
        txn(timestamp="2026-11-01T03:00:00")
    assert txn().timestamp == datetime.fromisoformat("2026-11-01T14:00:00+11:00")
