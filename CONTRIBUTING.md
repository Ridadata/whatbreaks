# Contributing

## The most useful contribution

**Found SQL that whatbreaks gets wrong?** A test case is worth more than a bug
report, because it reproduces the problem, documents the expectation, and
becomes the regression test the moment it is fixed.

- **Wrong lineage, or a finding that should not have fired** →
  add a case to [`tests/false_positives/cases/`](tests/false_positives/README.md).
  One YAML file, no code.
- **A break we failed to report** → the more valuable half. False negatives are
  the worst outcome this tool can produce: someone merges a breaking change
  believing it is safe. Open an issue with the two versions of the SQL.

## Setup

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest
```

## Before opening a pull request

```bash
uv run ruff format src tests
uv run ruff check .
uv run mypy
uv run pytest
```

All four must pass; CI runs them across Python 3.10–3.13 on Linux and Windows.

## Standards this project holds itself to

These are not style preferences. They are why the tool can be trusted.

1. **Never overclaim.** If analysis is incomplete, say so. A finding built on a
   partial schema is not `confirmed`. Every downgrade goes through
   `lineage/uncertainty.py` — scattering that logic is how the guarantee dies.
2. **Never render an unresolvable macro to empty string.** It produces SQL that
   parses cleanly and means something else. Fail loudly, with a name.
3. **False positives are a release gate.** `tests/false_positives/` runs as its
   own required CI job. A regression there blocks a release.
4. **No network calls, ever.** Enforced by an autouse fixture that blocks
   socket creation. This is both a determinism and a security property.
5. **Verify against real projects, not just fixtures.** Every significant bug so
   far was found by running the tool on real dbt projects or by the mechanical
   oracle — never by the unit tests alone. Tests written by the author of the
   code share its blind spots. `tools/validate_*.py` exist for this.

## Adding a rule

1. Pick the next free `WBxxx` id.
2. Implement in `diff/classify.py`. Severity and confidence are orthogonal;
   derive confidence via `confidence_for`, never by hand.
3. Add a page under `docs/rules/`.
4. Add both a positive test and a false-positive case.
5. New rules land as non-failing for one minor cycle — see the release policy
   in the [plan](WHATBREAKS_PROJECT_PLAN.md).

## Commit messages

Explain *why*, and what the alternative was. The commit log is the design
record for this project; several commits document bugs whose diagnosis is more
valuable than the fix.
