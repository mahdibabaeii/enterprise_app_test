"""SQLite store for the hash-chained decision log (docs/plan.md 2.2-2.5).

The table has four columns: id, prev_hash, payload and hash. Every fact lives in
the payload, which is written as canonical JSON, and hash is the SHA-256 of
those stored bytes. Appends are serialised with BEGIN IMMEDIATE. There is no
update or delete path.
"""

import json
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_serializer

from app.chain import (
    GENESIS_HASH,
    NonCanonicalPayload,
    canonical_json,
    parse_canonical,
    rule_set_sha256,
    sha256_hex,
)
from app.engine import Decision, Features, Outcome, Reason, Transaction, decide
from app.rules import AttemptsOver, RuleSet

logger = logging.getLogger(__name__)

RECORD_VERSION = 1
BUSY_TIMEOUT_SECONDS = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    prev_hash TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE
) STRICT
"""


class StoreWriteError(Exception):
    """The decision could not be recorded. Nothing was written."""


class RuleVersionUnavailable(Exception):
    """A stored record names a rule version that is not loaded, so it cannot be replayed."""


class RecordInput(BaseModel):
    """The transaction as stored: card number already reduced to card_masked and card_ref."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction_id: str
    card_masked: str = Field(pattern=r"^\*{4}[0-9]{4}$")
    card_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    card_status: Literal["active", "reported_stolen"]
    amount_minor: int
    currency: str
    merchant_id: str
    merchant_country: str
    timestamp: AwareDatetime

    @field_serializer("timestamp")
    def _keep_offset(self, value: datetime) -> str:
        return value.isoformat()


class VerifyResult(BaseModel):
    """records_checked counts every record read, including the first broken one."""

    intact: bool
    records_checked: int
    first_broken_id: int | None = None
    reason: str | None = None


class DecisionView(BaseModel):
    """The parts of a decision that replay compares."""

    features: Features | None
    score: int | None
    reasons: list[Reason]
    outcome: Outcome


class ReplayResult(BaseModel):
    """match: replaying the stored input and features gives the stored decision.
    rules_match: the loaded rule set has the content the record was decided with.
    features_match: features recomputed from earlier records equal the stored ones;
    null when history was unavailable at decision time."""

    id: int
    match: bool
    rules_match: bool
    features_match: bool | None
    stored: DecisionView
    replayed: DecisionView


def utc_now() -> datetime:
    return datetime.now(UTC)


def read_features(
    connection: sqlite3.Connection, txn: RecordInput, rule_set: RuleSet, before_id: int
) -> Features:
    """History features from records with a smaller id for the same card (plan R6, R7).

    Attempts are earlier records whose transaction timestamp lies in
    [t - window, t], plus the current one. A merchant is seen before if any
    earlier record for the card used it. The window comes from the rule set's
    single attempts_over rule, which the rule loader requires.
    """
    window = next(
        rule.window_seconds for rule in rule_set.scored_rules if isinstance(rule, AttemptsOver)
    )
    earliest = txn.timestamp - timedelta(seconds=window)
    history = connection.execute(
        "SELECT json_extract(payload, '$.input.timestamp'),"
        " json_extract(payload, '$.input.merchant_id')"
        " FROM decisions WHERE id < ? AND json_extract(payload, '$.input.card_ref') = ?",
        (before_id, txn.card_ref),
    ).fetchall()
    in_window = [
        stamp for stamp, _ in history if earliest <= datetime.fromisoformat(stamp) <= txn.timestamp
    ]
    return Features(
        attempts_in_window=len(in_window) + 1,
        merchant_seen_before=any(merchant == txn.merchant_id for _, merchant in history),
    )


def _engine_transaction(txn: RecordInput) -> Transaction:
    return Transaction.model_validate(txn.model_dump(include=set(Transaction.model_fields)))


def _reasons(decision: Decision) -> list[dict[str, Any]]:
    """Reason entries as in plan 2.3: "rule" appears only on RULE_ERROR entries."""
    return [
        reason.model_dump(exclude=set() if reason.rule else {"rule"}) for reason in decision.reasons
    ]


class Store:
    def __init__(self, path: Path, clock: Callable[[], datetime] = utc_now) -> None:
        self._path = path
        self._clock = clock
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(SCHEMA)
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)

    def append(self, txn: RecordInput, rule_set: RuleSet) -> dict[str, Any]:
        """Decide `txn` against `rule_set` and append the record. Return the stored payload.

        Raise StoreWriteError if the record cannot be written; then nothing is stored.
        """
        rules_sha256 = rule_set_sha256(rule_set)
        connection: sqlite3.Connection | None = None
        try:
            connection = self._connect()
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                "SELECT id, hash FROM decisions ORDER BY id DESC LIMIT 1"
            ).fetchone()
            record_id, prev_hash = (head[0] + 1, head[1]) if head else (1, GENESIS_HASH)
            try:
                features = read_features(connection, txn, rule_set, before_id=record_id)
            except Exception as error:
                logger.warning("history unavailable: %s", type(error).__name__)
                features = None
            # SQLite rolls back by itself on some errors (I/O, full, busy). In autocommit
            # mode the insert would then commit alone, outside the write lock.
            if not connection.in_transaction:
                raise StoreWriteError("decision could not be recorded: transaction was rolled back")
            decision = decide(_engine_transaction(txn), features, rule_set)
            payload = {
                "record_version": RECORD_VERSION,
                "id": record_id,
                "prev_hash": prev_hash,
                "received_at": self._clock().astimezone(UTC).isoformat(),
                "rule_version": rule_set.version,
                "rules_sha256": rules_sha256,
                "input": txn.model_dump(),
                "features": None if features is None else features.model_dump(),
                "score": decision.score,
                "reasons": _reasons(decision),
                "outcome": decision.outcome,
            }
            data = canonical_json(payload)
            inserted = connection.execute(
                "INSERT INTO decisions (id, prev_hash, payload, hash) VALUES (?, ?, ?, ?)",
                (record_id, prev_hash, data.decode(), sha256_hex(data)),
            )
            if inserted.rowcount != 1:
                raise StoreWriteError("decision could not be recorded: no row was inserted")
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            raise StoreWriteError(
                f"decision could not be recorded: {type(error).__name__}: {error}"
            ) from error
        finally:
            if connection is not None:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                connection.close()
        logger.info(
            "decision %s outcome=%s score=%s card=%s",
            payload["id"],
            payload["outcome"],
            payload["score"],
            txn.card_masked,
        )
        return payload

    def get(self, record_id: int) -> dict[str, Any] | None:
        """The stored payload of one record, or None. Integrity is checked by verify()."""
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload FROM decisions WHERE id = ?", (record_id,)
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else json.loads(row[0])

    def latest(self, limit: int) -> list[dict[str, Any]]:
        """The stored payloads of the latest records, newest first."""
        connection = self._connect()
        try:
            found = connection.execute(
                "SELECT payload FROM decisions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            connection.close()
        return [json.loads(text) for (text,) in found]

    def replay(self, record_id: int, rule_sets: dict[int, RuleSet]) -> ReplayResult | None:
        """Re-run a stored decision on its stored rule version (plan 2.5). None if unknown id."""
        payload = self.get(record_id)
        if payload is None:
            return None
        rule_set = rule_sets.get(payload["rule_version"])
        if rule_set is None:
            raise RuleVersionUnavailable(
                f"rule version {payload['rule_version']} is not loaded; record {record_id}"
                " cannot be replayed"
            )
        txn = RecordInput.model_validate(payload["input"])
        stored_features = (
            None if payload["features"] is None else Features.model_validate(payload["features"])
        )
        decision = decide(_engine_transaction(txn), stored_features, rule_set)
        recomputed: Features | None = None
        if stored_features is not None:
            connection = self._connect()
            try:
                recomputed = read_features(connection, txn, rule_set, before_id=record_id)
            finally:
                connection.close()
        stored = DecisionView(
            features=stored_features,
            score=payload["score"],
            reasons=payload["reasons"],
            outcome=payload["outcome"],
        )
        replayed = DecisionView(
            features=recomputed,
            score=decision.score,
            reasons=decision.reasons,
            outcome=decision.outcome,
        )
        return ReplayResult(
            id=record_id,
            match=stored.model_dump(exclude={"features"})
            == replayed.model_dump(exclude={"features"}),
            rules_match=rule_set_sha256(rule_set) == payload["rules_sha256"],
            features_match=None if stored_features is None else recomputed == stored_features,
            stored=stored,
            replayed=replayed,
        )

    def verify(self) -> VerifyResult:
        """Walk the chain in id order and stop at the first broken record (plan 2.5)."""
        connection = self._connect()
        try:
            records = connection.execute(
                "SELECT id, prev_hash, CAST(payload AS BLOB), typeof(payload), hash"
                " FROM decisions ORDER BY id"
            )
            prev_id, prev_hash, checked = 0, GENESIS_HASH, 0
            for record_id, column_prev_hash, data, payload_type, record_hash in records:
                checked += 1
                reason = _broken_reason(
                    record_id, column_prev_hash, data, payload_type, record_hash, prev_id, prev_hash
                )
                if reason:
                    return VerifyResult(
                        intact=False,
                        records_checked=checked,
                        first_broken_id=record_id,
                        reason=reason,
                    )
                prev_id, prev_hash = record_id, record_hash
            return VerifyResult(intact=True, records_checked=checked)
        finally:
            connection.close()


def _broken_reason(
    record_id: int,
    column_prev_hash: Any,
    data: bytes,
    payload_type: str,
    record_hash: Any,
    prev_id: int,
    prev_hash: str,
) -> str | None:
    """Why this record breaks the chain, or None if it is intact."""
    expected_id = prev_id + 1
    if record_id < expected_id:
        return f"record {record_id} is out of sequence; expected record {expected_id}"
    if record_id > expected_id:
        if record_id == expected_id + 1:
            missing = f"record {expected_id} is missing"
        else:
            missing = f"records {expected_id} to {record_id - 1} are missing"
        return f"expected record {expected_id} but found record {record_id}; {missing}"
    if payload_type != "text" or sha256_hex(data) != record_hash:
        return (
            f"hash of record {record_id} does not match its payload; record {record_id} was altered"
        )
    try:
        payload = parse_canonical(data)
    except NonCanonicalPayload as error:
        return f"payload of record {record_id} {error}; record {record_id} was altered"
    payload_id = payload.get("id") if isinstance(payload, dict) else None
    if type(payload_id) is not int or payload_id != record_id:
        return f"payload id of record {record_id} does not match; record {record_id} was altered"
    if payload.get("prev_hash") != prev_hash or column_prev_hash != prev_hash:
        if prev_id == 0:
            return "prev_hash of record 1 is not the genesis hash; record 1 was altered"
        return (
            f"prev_hash of record {record_id} does not match hash of record {prev_id};"
            f" record {prev_id} or {record_id} was altered"
        )
    return None
