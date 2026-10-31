"""Rule set loading. Expected values come from docs/PRD.md and docs/plan.md section 3."""

from pathlib import Path

import pytest

from app.rules import RuleSetError, active_rule_set, load_rule_sets
from tests.controls import control

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

VALID_RULES = """\
version: 1
thresholds: {step_up: 30, decline: 70}
home_country: AU
hard_rules:
  - {code: CARD_REPORTED_STOLEN, type: card_status_is, status: reported_stolen}
  - {code: MERCHANT_BLOCKLISTED, type: merchant_in_list, merchants: [M-BLOCK-001]}
scored_rules:
  - {code: FOREIGN_COUNTRY, type: foreign_country, points: 30}
  - {code: UNUSUAL_AMOUNT, type: amount_at_least, amount_minor: 100000, points: 25}
  - {code: HIGH_VELOCITY, type: attempts_over, window_seconds: 60, max_attempts: 5, points: 40}
  - {code: NEW_MERCHANT_AT_NIGHT, type: new_merchant_at_night, night_start_hour: 0,
     night_end_hour: 6, points: 10}
"""


def write_rules(directory: Path, name: str, text: str) -> None:
    (directory / name).write_text(text)


@control("VER-01")
def test_v1_rule_set_holds_the_prd_values() -> None:
    rule_set = active_rule_set(load_rule_sets(RULES_DIR))

    assert rule_set.version == 1
    assert (rule_set.thresholds.step_up, rule_set.thresholds.decline) == (30, 70)
    assert rule_set.home_country == "AU"
    assert [rule.code for rule in rule_set.hard_rules] == [
        "CARD_REPORTED_STOLEN",
        "MERCHANT_BLOCKLISTED",
    ]
    assert {rule.code: rule.points for rule in rule_set.scored_rules} == {
        "FOREIGN_COUNTRY": 30,
        "UNUSUAL_AMOUNT": 25,
        "HIGH_VELOCITY": 40,
        "NEW_MERCHANT_AT_NIGHT": 10,
    }


@control("VER-01")
def test_highest_integer_version_is_active(tmp_path: Path) -> None:
    write_rules(tmp_path, "v2.yaml", VALID_RULES.replace("version: 1", "version: 2"))
    write_rules(tmp_path, "v10.yaml", VALID_RULES.replace("version: 1", "version: 10"))
    write_rules(tmp_path, "v9.yaml", VALID_RULES.replace("version: 1", "version: 9"))

    rule_sets = load_rule_sets(tmp_path)

    assert sorted(rule_sets) == [2, 9, 10]
    assert active_rule_set(rule_sets).version == 10


@control("FS-01")
@control("VER-01")
@pytest.mark.parametrize(
    ("case", "text"),
    [
        (
            "unknown rule type",
            VALID_RULES.replace("type: foreign_country", "type: lucky_number"),
        ),
        (
            "missing points",
            VALID_RULES.replace("type: foreign_country, points: 30", "type: foreign_country"),
        ),
        ("string version", VALID_RULES.replace("version: 1", 'version: "1"')),
        ("fractional version", VALID_RULES.replace("version: 1", "version: 1.5")),
        ("points as text", VALID_RULES.replace("points: 25", 'points: "25"')),
        ("unknown field", VALID_RULES.replace("home_country: AU", "home_country: AU\nbonus: 5")),
        ("not yaml", "version: [1\n"),
    ],
)
def test_t19_invalid_rule_file_fails_to_load(tmp_path: Path, case: str, text: str) -> None:
    write_rules(tmp_path, "v1.yaml", text)

    with pytest.raises(RuleSetError):
        load_rule_sets(tmp_path)


@control("FS-01")
@control("VER-01")
def test_t19_duplicate_version_fails_to_load(tmp_path: Path) -> None:
    write_rules(tmp_path, "v1.yaml", VALID_RULES)
    write_rules(tmp_path, "v1-copy.yaml", VALID_RULES)

    with pytest.raises(RuleSetError):
        load_rule_sets(tmp_path)


@control("FS-01")
def test_t19_empty_rules_directory_fails_to_load(tmp_path: Path) -> None:
    with pytest.raises(RuleSetError):
        load_rule_sets(tmp_path)
