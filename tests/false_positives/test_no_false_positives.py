"""The false-positive gate.

A linter's false-positive rate determines whether it survives contact with a
real team far more than its feature count does. One wrong BREAKING on a
reformatting commit and the tool gets removed from the workflow that week.

So this is a **release gate**, not a test suite. Any regression here blocks a
release, and that is deliberately a stronger commitment than "the tests pass".

The bar: a no-op change must produce nothing at or above POSSIBLY_BREAKING.
SAFE and INFO findings are allowed - they are informational and do not fail CI
under the default `--fail-on=breaking` - but anything that would alarm a
reviewer or turn a check red is a failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.false_positives.conftest import Case, all_cases
from whatbreaks.analysis import Analysis
from whatbreaks.diff import Severity, classify, diff_analyses

CASES = all_cases()

pytestmark = pytest.mark.false_positive


def run_case(case: Case, tmp_path: Path):
    base = Analysis.run(case.write(tmp_path, "base"))
    head = Analysis.run(case.write(tmp_path, "head"))
    return classify(diff_analyses(base, head), head, base)


def test_the_corpus_is_not_empty() -> None:
    """Guard against a vacuously passing gate.

    A gate that runs zero cases passes forever and protects nothing.
    """
    assert len(CASES) >= 12, f"only {len(CASES)} false-positive cases found"


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_no_op_change_raises_no_alarm(case: Case, tmp_path: Path) -> None:
    findings = run_case(case, tmp_path)
    alarming = [f for f in findings.items if f.severity.rank >= Severity.POSSIBLY_BREAKING.rank]
    assert not alarming, (
        f"{case.name}: {case.description}\n"
        f"  why this must be silent: {case.why}\n"
        f"  but whatbreaks reported:\n"
        + "\n".join(f"    [{f.severity.value}] {f.rule} {f.summary} - {f.detail}" for f in alarming)
    )


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_no_op_change_analyses_completely(case: Case, tmp_path: Path) -> None:
    """The corpus must exercise the real path, not the degraded one.

    A case whose models fail to resolve would pass the gate above trivially,
    for the wrong reason: we cannot report a break in a model we never read.
    Requiring full coverage keeps the gate honest.
    """
    findings = run_case(case, tmp_path)
    coverage = findings.coverage
    assert coverage is not None
    assert coverage.unknown == 0, (
        f"{case.name}: {coverage.unknown} model(s) could not be analysed, so this "
        f"case passes for the wrong reason"
    )


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_case_documents_why(case: Case) -> None:
    """A case without a rationale is a case nobody can maintain."""
    assert case.why.strip(), f"{case.name} has no `why:` explaining the expectation"


def test_the_gate_can_actually_fail(tmp_path: Path) -> None:
    """Guard against a gate that cannot fail.

    Every assertion above is of the form "nothing was reported". If the harness
    silently analysed nothing, or classification quietly returned no findings,
    all of them would pass while protecting nothing at all.

    So: feed the same harness a genuinely breaking change and require that it
    IS flagged. This is the only test in this file that expects an alarm.
    """
    breaking = Case(
        name="_meta_breaking",
        description="a column that a downstream model selects is removed",
        why="this one SHOULD be reported; it exists to prove the gate works",
        models={
            "up": {"base": "select 1 as a, 2 as b from tbl", "head": "select 1 as a from tbl"},
            "down": {
                "depends_on": ["up"],
                "base": "select b as kept from {{ ref('up') }}",
            },
        },
    )
    findings = run_case(breaking, tmp_path)
    alarming = [f for f in findings.items if f.severity.rank >= Severity.POSSIBLY_BREAKING.rank]
    assert alarming, "the false-positive gate would pass anything - it is vacuous"
    assert any(f.rule == "WB001" and f.column == "b" for f in alarming)
