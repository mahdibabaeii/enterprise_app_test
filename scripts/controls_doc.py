"""Generate docs/controls.md from the @control tags on the tests (PRD controls section).

Collects the test suite without running it, groups tests by their control id
and writes the document. Fails if a test names an unknown control id, or if a
control has no test. make verify runs this with --check, which fails when
docs/controls.md is out of date.

Usage:
  python -m scripts.controls_doc           write docs/controls.md
  python -m scripts.controls_doc --check   exit 1 if docs/controls.md is stale
"""

import contextlib
import io
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "controls.md"


@dataclass(frozen=True)
class Control:
    id: str
    name: str
    does: str
    expectation: str


# From docs/PRD.md (controls table) and docs/plan.md section 4 (control ids).
# Expectations name the type only; regulatory references are not yet confirmed (plan R24).
CONTROLS = [
    Control(
        "EXP-01",
        "Explainability",
        "Every decision returns reason codes and a score",
        "Model risk management, fair treatment of customers",
    ),
    Control(
        "REP-01",
        "Reproducibility",
        "Replay re-runs stored input on the stored rule version",
        "Model risk, audit evidence",
    ),
    Control(
        "AUD-01",
        "Tamper evidence",
        "Hash-chained log with verify that names the first altered record",
        "Record integrity, internal audit",
    ),
    Control(
        "AUD-02",
        "Append-only, serialised appends",
        "No update or delete path; concurrent appends cannot extend the same hash",
        "Record integrity, internal audit",
    ),
    Control(
        "ACC-01",
        "Least privilege",
        "Analyst and auditor roles enforced on every endpoint, deny by default",
        "Access control, separation of duties",
    ),
    Control(
        "FS-01",
        "Fail safe",
        "Errors and missing data give step-up, never approve",
        "Operational resilience",
    ),
    Control(
        "DAT-01",
        "Data minimisation",
        "Synthetic data only, card numbers masked to the last 4 digits,"
        " no sensitive values in logs",
        "Privacy, payment data handling",
    ),
    Control(
        "VER-01",
        "Versioned change",
        "Rules are versioned files, and each decision records its version",
        "Change management",
    ),
]


@dataclass(frozen=True)
class TaggedTest:
    """One test function; parametrized cases are counted, not listed."""

    path: str
    name: str
    controls: tuple[str, ...]
    cases: int


class ControlsError(Exception):
    """A tag names an unknown control, or a control has no test."""


class _Collector:
    def __init__(self) -> None:
        self.items: list[pytest.Item] = []

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.items = list(session.items)


def collect() -> list[TaggedTest]:
    """Collect the suite without running it, one entry per test function in file order."""
    collector = _Collector()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = pytest.main(
            ["--collect-only", "-q", "-p", "no:cacheprovider", str(REPO_ROOT / "tests")],
            plugins=[collector],
        )
    if code != 0:
        raise ControlsError(f"test collection failed with exit code {code}:\n{output.getvalue()}")
    tests: dict[tuple[str, str], TaggedTest] = {}
    for item in collector.items:
        path = Path(str(item.path)).relative_to(REPO_ROOT).as_posix()
        name = getattr(item, "originalname", item.name)
        controls = tuple(sorted({marker.args[0] for marker in item.iter_markers("control")}))
        key = (path, name)
        previous = tests.get(key)
        tests[key] = TaggedTest(path, name, controls, previous.cases + 1 if previous else 1)
    return list(tests.values())


def _title(test: TaggedTest) -> str:
    """test_t07_edited_payload_fails -> 'T07: edited payload fails'."""
    words = test.name.removeprefix("test_")
    match = re.match(r"t(\d{2})_(.*)", words)
    label = f"T{match.group(1)}: {match.group(2)}" if match else words
    label = label.replace("_", " ")
    return f"{label} ({test.cases} cases)" if test.cases > 1 else label


def build(tests: list[TaggedTest]) -> str:
    """Render the controls document. Raise ControlsError on unknown or untested controls."""
    known = {control.id for control in CONTROLS}
    by_control: dict[str, list[TaggedTest]] = defaultdict(list)
    for test in tests:
        for control_id in test.controls:
            if control_id not in known:
                raise ControlsError(f"{test.path}::{test.name} names unknown control {control_id}")
            by_control[control_id].append(test)
    untested = [control.id for control in CONTROLS if not by_control[control.id]]
    if untested:
        raise ControlsError(f"no test covers control {', '.join(untested)}")

    lines = [
        "# Controls",
        "",
        "Generated by `python -m scripts.controls_doc` from the `@control` tags on the",
        "tests. Do not edit by hand; `make verify` fails if this file is out of date.",
        "",
        "The expectation column names the type of expectation each control supports.",
        "Exact regulatory references are to be confirmed before they appear here",
        "(docs/plan.md R24).",
        "",
        "| Id | Control | What the system does | Expectation it supports | Tests |",
        "| --- | --- | --- | --- | --- |",
    ]
    for control in CONTROLS:
        count = sum(test.cases for test in by_control[control.id])
        lines.append(
            f"| [{control.id}](#{control.id.lower()}) | {control.name} | {control.does}"
            f" | {control.expectation} | {count} |"
        )
    for control in CONTROLS:
        lines += ["", f"## {control.id}", "", f"**{control.name}.** {control.does}.", ""]
        current_path = None
        for test in by_control[control.id]:
            if test.path != current_path:
                current_path = test.path
                lines.append(f"- `{test.path}`")
            lines.append(f"  - {_title(test)}")
    untagged = [test for test in tests if not test.controls]
    lines += [
        "",
        "## Tests without a control tag",
        "",
        "These cover input validation, lookups, the demo commands, start-up",
        "(acceptance criterion A9, which has no control in the PRD) and this generator.",
        "",
    ]
    lines += [f"- `{test.path}`: {_title(test)}" for test in untagged]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    try:
        document = build(collect())
    except ControlsError as error:
        print(f"controls document FAILED: {error}", file=sys.stderr)
        return 1
    if "--check" in argv:
        if not OUTPUT.exists() or OUTPUT.read_text() != document:
            print(
                f"controls document FAILED: {OUTPUT.relative_to(REPO_ROOT)} is out of date;"
                " run: uv run python -m scripts.controls_doc",
                file=sys.stderr,
            )
            return 1
        print("controls document is up to date")
        return 0
    OUTPUT.write_text(document)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
