"""Canonical JSON and SHA-256 hashing for the decision chain (docs/plan.md 2.3, 2.5).

A record's hash is the SHA-256 of its stored payload bytes. The payload holds
the previous record's hash, so one hash covers the canonical JSON and the link.
"""

import hashlib
import json
from typing import Any

from app.rules import RuleSet

GENESIS_HASH = "0" * 64


class NonCanonicalPayload(ValueError):
    """Stored payload text is not valid, duplicate-free, canonical JSON."""


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no whitespace, ASCII only, encoded as UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rule_set_sha256(rule_set: RuleSet) -> str:
    """Hash of the validated rule set's content, independent of file formatting."""
    return sha256_hex(canonical_json(rule_set.model_dump(mode="json")))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise NonCanonicalPayload("has a duplicate key")
    return dict(pairs)


def parse_canonical(data: bytes) -> Any:
    """Parse stored payload bytes, requiring them to be exactly their canonical form.

    Duplicate keys are rejected outright, because Python keeps the last one and
    SQLite's json_extract keeps the first, so the two would read different values.
    """
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise NonCanonicalPayload("is not canonical JSON") from error
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except json.JSONDecodeError as error:
        raise NonCanonicalPayload("is not valid JSON") from error
    if canonical_json(value) != data:
        raise NonCanonicalPayload("is not canonical JSON")
    return value
