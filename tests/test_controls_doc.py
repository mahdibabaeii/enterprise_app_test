"""The controls document generator (PRD: the controls document is generated from
the test tags). It must refuse a tag that names no known control and a control
that no test covers, because either would make the document misleading."""

import pytest

from scripts.controls_doc import CONTROLS, ControlsError, TaggedTest, build


def tagged(name: str, *controls: str, cases: int = 1) -> TaggedTest:
    return TaggedTest("tests/test_x.py", name, controls, cases)


def every_control_once() -> list[TaggedTest]:
    return [tagged(f"test_{c.id.lower().replace('-', '_')}", c.id) for c in CONTROLS]


def test_unknown_control_id_is_refused() -> None:
    with pytest.raises(ControlsError, match="AUD-99"):
        build([*every_control_once(), tagged("test_typo", "AUD-99")])


def test_control_without_a_test_is_refused() -> None:
    without_fail_safe = [t for t in every_control_once() if t.controls != ("FS-01",)]

    with pytest.raises(ControlsError, match="FS-01"):
        build(without_fail_safe)


def test_document_counts_cases_and_lists_tests_under_each_control() -> None:
    tests = [
        *every_control_once(),
        tagged("test_t07_edited_payload_fails_verify", "AUD-01", "AUD-02", cases=3),
        tagged("test_bad_input_gives_422", cases=14),
    ]

    document = build(tests)

    assert "| [AUD-01](#aud-01) | Tamper evidence |" in document
    aud_row = next(line for line in document.splitlines() if line.startswith("| [AUD-01]"))
    assert aud_row.endswith("| 4 |")  # one single test plus three cases
    aud_section = document.split("## AUD-01")[1].split("## AUD-02")[0]
    assert "T07: edited payload fails verify (3 cases)" in aud_section
    untagged = document.split("## Tests without a control tag")[1]
    assert "bad input gives 422 (14 cases)" in untagged
