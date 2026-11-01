"""Request and response bodies, and card number reduction (docs/plan.md 2.1).

The full card number exists only inside TransactionRequest. to_record() turns
it into card_masked (last 4 digits) and card_ref (keyed hash) and drops it.
"""

import hashlib
import hmac
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from app.engine import Features, Outcome, Reason
from app.rules import Thresholds
from app.store import RecordInput

IDENTIFIER = r"^[A-Za-z0-9_-]+$"


def luhn_valid(number: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(number)):
        digit = int(char)
        if position % 2:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


class TransactionRequest(BaseModel):
    """Body of POST /decisions. Strict: bad input is rejected, never coerced."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: str = Field(min_length=1, max_length=64, pattern=IDENTIFIER)
    card_number: str = Field(pattern=r"^[0-9]{12,19}$", repr=False)
    card_status: Literal["active", "reported_stolen"]
    amount_minor: int = Field(strict=True, gt=0, le=100_000_000)
    currency: Literal["AUD"]
    merchant_id: str = Field(min_length=1, max_length=64, pattern=IDENTIFIER)
    merchant_country: str = Field(pattern=r"^[A-Z]{2}$")
    timestamp: AwareDatetime

    @field_validator("timestamp", mode="before")
    @classmethod
    def _iso_text(cls, value: object) -> object:
        # Only ISO 8601 text: an epoch number has no offset to read local time from.
        if not isinstance(value, str):
            raise ValueError("timestamp must be ISO 8601 text with a UTC offset")
        return value

    @field_validator("card_number")
    @classmethod
    def _luhn(cls, value: str) -> str:
        if not luhn_valid(value):
            raise ValueError("card number fails the Luhn check")
        return value

    def to_record(self, card_ref_key: str) -> RecordInput:
        """The transaction as stored: masked card and keyed card reference, no card number."""
        card_ref = hmac.new(card_ref_key.encode(), self.card_number.encode(), hashlib.sha256)
        return RecordInput(
            card_masked=f"****{self.card_number[-4:]}",
            card_ref=card_ref.hexdigest()[:32],
            **self.model_dump(exclude={"card_number"}),
        )


class DecisionRecord(BaseModel):
    """A stored decision, as returned by GET /decisions and GET /decisions/{id}."""

    record_version: int
    id: int
    prev_hash: str
    received_at: str
    rule_version: int
    rules_sha256: str
    input: RecordInput
    features: Features | None
    score: int | None
    reasons: list[Reason]
    outcome: Outcome


class CreatedDecision(DecisionRecord):
    """Response of POST /decisions: the stored decision plus thresholds for display."""

    thresholds: Thresholds


class ErrorResponse(BaseModel):
    detail: str
