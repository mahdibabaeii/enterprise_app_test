"""Decision engine: a pure function of transaction, history features and rule set.

No I/O and no clock. Time-based rules read the transaction's own timestamp in
its own UTC offset. Fail safe: a rule error or missing history raises the
outcome to at least step_up and never downgrades a decline.
"""

import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from app.rules import (
    AmountAtLeast,
    AttemptsOver,
    CardStatusIs,
    ForeignCountry,
    MerchantInList,
    NewMerchantAtNight,
    RuleSet,
    Thresholds,
)

logger = logging.getLogger(__name__)

Outcome = Literal["approve", "step_up", "decline"]
SEVERITY: dict[Outcome, int] = {"approve": 0, "step_up": 1, "decline": 2}

RULE_ERROR = "RULE_ERROR"
HISTORY_UNAVAILABLE = "HISTORY_UNAVAILABLE"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Transaction(_Model):
    """The transaction fields the rules read."""

    card_status: Literal["active", "reported_stolen"]
    amount_minor: int
    merchant_id: str
    merchant_country: str
    timestamp: AwareDatetime


class Features(_Model):
    """History-derived facts computed by the store from earlier records."""

    attempts_in_window: int
    merchant_seen_before: bool


class Reason(_Model):
    code: str
    points: int | None
    rule: str | None = None


class Decision(_Model):
    score: int | None
    reasons: list[Reason]
    outcome: Outcome


def _card_status_is(rule: CardStatusIs, txn: Transaction) -> bool:
    return txn.card_status == rule.status


def _merchant_in_list(rule: MerchantInList, txn: Transaction) -> bool:
    return txn.merchant_id in rule.merchants


def _foreign_country(
    rule: ForeignCountry, txn: Transaction, features: Features | None, rule_set: RuleSet
) -> bool:
    return txn.merchant_country != rule_set.home_country


def _amount_at_least(
    rule: AmountAtLeast, txn: Transaction, features: Features | None, rule_set: RuleSet
) -> bool:
    return txn.amount_minor >= rule.amount_minor


def _attempts_over(
    rule: AttemptsOver, txn: Transaction, features: Features, rule_set: RuleSet
) -> bool:
    return features.attempts_in_window > rule.max_attempts


def _new_merchant_at_night(
    rule: NewMerchantAtNight, txn: Transaction, features: Features, rule_set: RuleSet
) -> bool:
    local_hour = txn.timestamp.hour  # wall-clock hour in the transaction's own offset
    is_night = rule.night_start_hour <= local_hour < rule.night_end_hour
    return is_night and not features.merchant_seen_before


HARD_RULE_CHECKS: dict[str, Callable[..., bool]] = {
    "card_status_is": _card_status_is,
    "merchant_in_list": _merchant_in_list,
}
SCORED_RULE_CHECKS: dict[str, Callable[..., bool]] = {
    "foreign_country": _foreign_country,
    "amount_at_least": _amount_at_least,
    "attempts_over": _attempts_over,
    "new_merchant_at_night": _new_merchant_at_night,
}
RULES_NEEDING_HISTORY = frozenset({"attempts_over", "new_merchant_at_night"})


def outcome_for_score(score: int, thresholds: Thresholds) -> Outcome:
    if score >= thresholds.decline:
        return "decline"
    if score >= thresholds.step_up:
        return "step_up"
    return "approve"


def _rule_error(rule: Any, error: Exception) -> Reason:
    """Log the rule code and exception class only: the message may hold input values."""
    logger.warning("rule %s raised %s", rule.code, type(error).__name__)
    return Reason(code=RULE_ERROR, points=None, rule=rule.code)


def decide(txn: Transaction, features: Features | None, rule_set: RuleSet) -> Decision:
    """Decide a transaction. `features` is None when history was unavailable."""
    reasons: list[Reason] = []
    floor: Outcome = "approve"

    for hard_rule in rule_set.hard_rules:
        try:
            matched = HARD_RULE_CHECKS[hard_rule.type](hard_rule, txn)
        except Exception as error:
            reasons.append(_rule_error(hard_rule, error))
            floor = "step_up"
            continue
        if matched:
            return Decision(
                score=None,
                reasons=[Reason(code=hard_rule.code, points=None)],
                outcome="decline",
            )

    score = 0
    for scored_rule in rule_set.scored_rules:
        if features is None and scored_rule.type in RULES_NEEDING_HISTORY:
            continue
        try:
            matched = SCORED_RULE_CHECKS[scored_rule.type](scored_rule, txn, features, rule_set)
        except Exception as error:
            reasons.append(_rule_error(scored_rule, error))
            floor = "step_up"
            continue
        if matched:
            score += scored_rule.points
            reasons.append(Reason(code=scored_rule.code, points=scored_rule.points))

    if features is None:
        reasons.append(Reason(code=HISTORY_UNAVAILABLE, points=None))
        floor = "step_up"

    outcome = max(outcome_for_score(score, rule_set.thresholds), floor, key=SEVERITY.__getitem__)
    return Decision(score=score, reasons=reasons, outcome=outcome)
