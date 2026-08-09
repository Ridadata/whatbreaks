# whatbreaks

**Static breaking-change analysis for dbt. Column-level blast radius in CI — no warehouse, no
secrets, no backend.**

[![CI](https://github.com/Ridadata/whatbreaks/actions/workflows/ci.yml/badge.svg)](https://github.com/Ridadata/whatbreaks/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> **v0.1.0 — early but real.** It works, it is tested against real dbt projects, and the output is
> honest about what it could not analyse. The rule set is deliberately small (three rules), there
> is no GitHub Action yet, and the JSON shape may still move. See [Limitations](docs/limitations.md).

---

## The problem

You rename a column. `dbt build` passes. Three hops downstream a dashboard silently breaks, and you
find out a week later from an angry Slack message.

`dbt ls --select model+` tells you which **models** are downstream. It does not tell you which
**columns** are, so it flags 40 models when 2 actually touch what you changed. That noise is why
nobody runs it.

## What it does

```console
$ whatbreaks check --base base/target/manifest.json --head target/manifest.json

BREAKING  WB001  column stg_orders.status was removed
          still referenced by: orders; breaks 1 column, 1 model, 1 test
          confidence: likely
    info  WB900  orders could not be compared
          head schema is partial

analysed 5/5 models (100.0%) - 4 exact, 1 partial, 0 not analysed
      1  qualify_error
note: analysis was incomplete, so absence of findings is not proof of safety
```

Exit code `1`, because something actually breaks. On a no-op change it prints one line and exits `0`.

## Install

```console
pip install whatbreaks     # or: uv tool install whatbreaks
```

Three runtime dependencies plus PyYAML. No warehouse driver, no service, no account.

## Use

You need two manifests: one from the base commit, one from the change.

```bash
# base
git worktree add ../base origin/main
(cd ../base && dbt parse)

# head
dbt parse

whatbreaks check --base ../base/target/manifest.json --head target/manifest.json
```

`dbt parse` is enough — it runs offline and needs no warehouse connection.

| Flag | Purpose |
| --- | --- |
| `--fail-on breaking` | default; only confirmed breakage fails the run |
| `--fail-on possibly-breaking` | stricter |
| `--fail-on never` | report only, always exit 0 |
| `--format text\|json\|markdown` | humans, tooling, PR comments |

Exit codes: `0` clean · `1` findings at or above the threshold · `2` bad input (nothing analysed).

There is also `whatbreaks debug schema|graph|sql|coverage` for inspecting what the tool sees.

## Why another one of these

Column-level lineage for dbt is not new, and this does not pretend to invent it. `sqlglot` does the
parsing; several good tools build on it.

The gap is elsewhere. **Every existing CI impact tool needs infrastructure:**

| Tool | Needs |
| --- | --- |
| [Recce](https://github.com/DataRecce/recce) | live warehouse + two dbt environments |
| [dbt-column-lineage](https://github.com/Fszta/dbt-column-lineage) | `catalog.json`, i.e. a warehouse |
| [SQLMesh](https://github.com/TobikoData/sqlmesh) | adopting SQLMesh as your framework |
| dbt Cloud Explorer | dbt Cloud, Enterprise tier |
| Datafold / Atlan / Sifflet / Metaplane | a hosted catalog (SaaS) |

whatbreaks needs `dbt parse` output and nothing else. That has one consequence worth stating: it is
the only tool of its class that can run on a pull request **from a fork**, where `GITHUB_TOKEN` is
read-only and secrets are unavailable.

If you already run Recce or Datafold happily, they answer a *different and often better* question —
"did the data actually change?" — by querying your warehouse. whatbreaks answers "what could break?"
statically, for free, in seconds.

## Design commitments

- **Never overclaim.** Findings carry a *severity* and an independent *confidence*. Coverage is
  always reported. A clean result is never printed without saying how much was analysed.
- **Absence of evidence is not evidence of absence.** A removed column with no consumer is `SAFE`
  only when coverage is complete; otherwise it is `POSSIBLY_BREAKING`.
- **A graph diff, not a text diff.** Reformatting, column reordering and CTE renames produce
  nothing. A model whose columns changed because an *upstream* `SELECT *` changed is caught even
  though its own file was never touched.
- **Deterministic.** No network calls, ever — enforced by a test that blocks sockets. No LLMs.
  Same inputs, byte-identical output.

## Rules

| Rule | Change | Severity |
| --- | --- | --- |
| [WB001](docs/rules/WB001.md) | column removed | breaking / possibly / safe, by blast radius |
| [WB002](docs/rules/WB002.md) | model removed | breaking if still referenced |
| [WB003](docs/rules/WB003.md) | column added | safe |
| [WB900](docs/rules/WB900.md) | model could not be analysed | info |

Renames, type changes and expression changes are inference rather than fact, and wait for later
releases rather than shipping as guesses.

## How well does it work?

Measured, not asserted. Across 7 public dbt projects (164 models) with **no warehouse**:

| | |
| --- | ---: |
| Models whose output columns resolve exactly | **75.6%** |
| Models with any usable schema | **83.5%** |
| Analytics-style projects (the target population) | **93.3%** |
| False positives on the [no-op corpus](tests/false_positives/) | **0** |

Method, per-project numbers and threats to validity: [ADR 000](docs/adr/000-feasibility.md).
The measurement scripts are in [`tools/`](tools/) and are reproducible.

## Limitations

Being explicit about these is a design goal, not an apology. Full list:
[docs/limitations.md](docs/limitations.md). The headlines:

- Without `catalog.json`, `SELECT *` over a source with no declared columns cannot be expanded.
  Those models are reported `PARTIAL`/`UNKNOWN`, never guessed.
- Macros needing a live warehouse (`run_query`, `adapter.get_relation`) are unresolvable offline and
  are reported as such rather than rendered to empty string.
- Python models are out of scope, and say so by name.

## Contributing

Found SQL whatbreaks gets wrong? [A corpus case](tests/false_positives/README.md) is the most useful
possible bug report — it reproduces the problem, documents the expectation, and becomes the
regression test the moment it is fixed. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

MIT
