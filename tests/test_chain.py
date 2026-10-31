"""Canonical JSON and hashing. Expected values come from docs/plan.md sections 2.3
and 2.5: canonical JSON is json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=True) as UTF-8, hashes are lowercase SHA-256 hex, and rules_sha256
covers the canonical JSON of the validated rule set, not the file bytes."""

import hashlib
import json
from pathlib import Path

import pytest

from app.chain import (
    GENESIS_HASH,
    NonCanonicalPayload,
    canonical_json,
    parse_canonical,
    rule_set_sha256,
    sha256_hex,
)
from app.rules import active_rule_set, load_rule_sets
from tests.controls import control

RULES_DIR = Path(__file__).resolve().parent.parent / "rules"

# rules/v1.yaml written out by hand from plan section 3.
V1_CONTENT = {
    "version": 1,
    "thresholds": {"step_up": 30, "decline": 70},
    "home_country": "AU",
    "hard_rules": [
        {"code": "CARD_REPORTED_STOLEN", "type": "card_status_is", "status": "reported_stolen"},
        {
            "code": "MERCHANT_BLOCKLISTED",
            "type": "merchant_in_list",
            "merchants": ["M-BLOCK-001", "M-BLOCK-002"],
        },
    ],
    "scored_rules": [
        {"code": "FOREIGN_COUNTRY", "type": "foreign_country", "points": 30},
        {
            "code": "UNUSUAL_AMOUNT",
            "type": "amount_at_least",
            "amount_minor": 100000,
            "points": 25,
        },
        {
            "code": "HIGH_VELOCITY",
            "type": "attempts_over",
            "window_seconds": 60,
            "max_attempts": 5,
            "points": 40,
        },
        {
            "code": "NEW_MERCHANT_AT_NIGHT",
            "type": "new_merchant_at_night",
            "night_start_hour": 0,
            "night_end_hour": 6,
            "points": 10,
        },
    ],
}


def rule_set_hash_from(directory: Path) -> str:
    return rule_set_sha256(active_rule_set(load_rule_sets(directory)))


@control("AUD-01")
def test_canonical_json_sorts_keys_drops_spaces_and_escapes_non_ascii() -> None:
    payload = {"b": 1, "a": ["é", None, True], "c": {"z": 0, "y": "x"}}

    assert canonical_json(payload) == b'{"a":["\\u00e9",null,true],"b":1,"c":{"y":"x","z":0}}'


@control("AUD-01")
def test_sha256_hex_is_lowercase_sha256() -> None:
    # FIPS 180-2 test vector for "abc".
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


@control("AUD-01")
def test_genesis_hash_is_64_zeros() -> None:
    assert GENESIS_HASH == "0" * 64


@control("AUD-01")
def test_parse_canonical_accepts_canonical_bytes() -> None:
    assert parse_canonical(b'{"a":[1,"\\u00e9"],"b":null}') == {"a": [1, "é"], "b": None}


@control("AUD-01")
@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (b'{"a":1,"a":2}', "duplicate key"),
        (b'{"b":{"x":1,"x":1}}', "duplicate key"),
        (b'{"b":1,"a":2}', "not canonical"),
        (b'{"a": 1}', "not canonical"),
        ('{"a":"é"}'.encode(), "not canonical"),
        (b'{"a":"\xff"}', "not canonical"),
        (b'{"a":1', "not valid JSON"),
    ],
)
def test_parse_canonical_rejects_duplicate_keys_and_other_forms(data: bytes, problem: str) -> None:
    with pytest.raises(NonCanonicalPayload, match=problem):
        parse_canonical(data)


@control("VER-01")
def test_rules_sha256_is_the_hash_of_the_canonical_validated_rule_set() -> None:
    expected = hashlib.sha256(
        json.dumps(V1_CONTENT, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()

    assert rule_set_hash_from(RULES_DIR) == expected


@control("VER-01")
def test_rules_sha256_ignores_comments_line_endings_and_key_order(tmp_path: Path) -> None:
    text = (RULES_DIR / "v1.yaml").read_text()
    text = "# a new comment\n" + text.replace("home_country: AU", "home_country: AU  # still AU")
    text = text.replace("{step_up: 30, decline: 70}", "{decline: 70, step_up: 30}")
    (tmp_path / "v1.yaml").write_bytes(text.replace("\n", "\r\n").encode())

    assert rule_set_hash_from(tmp_path) == rule_set_hash_from(RULES_DIR)


@control("VER-01")
def test_rules_sha256_changes_when_a_point_value_changes(tmp_path: Path) -> None:
    text = (RULES_DIR / "v1.yaml").read_text()
    (tmp_path / "v1.yaml").write_text(text.replace("points: 25", "points: 26"))

    assert rule_set_hash_from(tmp_path) != rule_set_hash_from(RULES_DIR)
