"""Load and validate versioned rule sets from rules/*.yaml.

Every threshold and point value lives in the YAML. Loading fails closed: any
invalid file stops the load, so the service cannot start on a bad rule set.
"""

from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"


class RuleSetError(Exception):
    """A rule file is missing, malformed or inconsistent."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Thresholds(_Model):
    step_up: int = Field(ge=0)
    decline: int = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.step_up >= self.decline:
            raise ValueError("step_up threshold must be below decline threshold")
        return self


class CardStatusIs(_Model):
    code: str
    type: Literal["card_status_is"]
    status: Literal["active", "reported_stolen"]


class MerchantInList(_Model):
    code: str
    type: Literal["merchant_in_list"]
    merchants: list[str] = Field(min_length=1)


class ForeignCountry(_Model):
    code: str
    type: Literal["foreign_country"]
    points: int = Field(gt=0)


class AmountAtLeast(_Model):
    code: str
    type: Literal["amount_at_least"]
    amount_minor: int = Field(gt=0)
    points: int = Field(gt=0)


class AttemptsOver(_Model):
    code: str
    type: Literal["attempts_over"]
    window_seconds: int = Field(gt=0)
    max_attempts: int = Field(ge=0)
    points: int = Field(gt=0)


class NewMerchantAtNight(_Model):
    code: str
    type: Literal["new_merchant_at_night"]
    night_start_hour: int = Field(ge=0, le=23)
    night_end_hour: int = Field(ge=1, le=24)
    points: int = Field(gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.night_start_hour >= self.night_end_hour:
            raise ValueError("night_start_hour must be before night_end_hour")
        return self


HardRule = Annotated[CardStatusIs | MerchantInList, Field(discriminator="type")]
ScoredRule = Annotated[
    ForeignCountry | AmountAtLeast | AttemptsOver | NewMerchantAtNight,
    Field(discriminator="type"),
]


class RuleSet(_Model):
    version: int = Field(ge=1)
    thresholds: Thresholds
    home_country: str = Field(pattern=r"^[A-Z]{2}$")
    hard_rules: list[HardRule]
    scored_rules: list[ScoredRule]

    @model_validator(mode="after")
    def _unique_codes(self) -> Self:
        codes = [rule.code for rule in (*self.hard_rules, *self.scored_rules)]
        if len(codes) != len(set(codes)):
            raise ValueError("rule codes must be unique")
        return self

    @model_validator(mode="after")
    def _one_velocity_window(self) -> Self:
        # Records hold a single attempts_in_window count, so exactly one window.
        if sum(isinstance(rule, AttemptsOver) for rule in self.scored_rules) != 1:
            raise ValueError("a rule set needs exactly one attempts_over rule")
        return self


def load_rule_sets(directory: Path = RULES_DIR) -> dict[int, RuleSet]:
    """Load every rules/*.yaml file, keyed by version. Raise RuleSetError on any problem."""
    rule_sets: dict[int, RuleSet] = {}
    for path in sorted(directory.glob("*.yaml")):
        try:
            rule_set = RuleSet.model_validate(yaml.safe_load(path.read_text()))
        except (yaml.YAMLError, ValidationError) as error:
            raise RuleSetError(f"{path.name}: invalid rule set: {error}") from error
        if rule_set.version in rule_sets:
            raise RuleSetError(f"{path.name}: duplicate rule set version {rule_set.version}")
        rule_sets[rule_set.version] = rule_set
    if not rule_sets:
        raise RuleSetError(f"no rule sets found in {directory}")
    return rule_sets


def active_rule_set(rule_sets: dict[int, RuleSet]) -> RuleSet:
    """The active rule set is the one with the highest version."""
    return rule_sets[max(rule_sets)]
