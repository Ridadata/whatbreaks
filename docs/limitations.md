# Limitations

This page is linked from the first screen of the README on purpose.

Every tool in this space implies completeness. whatbreaks does the opposite,
because the value it offers is *trustworthy* answers rather than complete ones,
and a tool that hides its blind spots cannot be trusted about anything else.

If something here is wrong or out of date, that is a bug worth filing.

---

## What it fundamentally cannot do

These follow from the design and will not be fixed by a better implementation.

### `SELECT *` over an undocumented source

To expand `select * from {{ source('raw', 'orders') }}`, the columns of
`raw.orders` must be known. They live in the warehouse. If the source declares
no columns in YAML and no `catalog.json` is supplied, they cannot be known
offline.

Such models are reported `PARTIAL` with reason `surviving_star`, never guessed.
Supplying `catalog.json` (`dbt docs generate`) resolves them; the coverage
report tells you how many would be fixed that way.

**Note the nuance:** a star over a *CTE* is fine and resolves without any
schema. Around half of real models contain a `SELECT *` and almost all of them
resolve. Only stars over genuinely unknown relations degrade.

### Macros that need a live warehouse

`run_query`, `adapter.get_relation`, `load_result` and friends execute against
the database at compile time. No static tool can resolve them.

They are reported as `needs_warehouse`, and `FailureKind.is_fixable` returns
`False` — the distinction matters, because "supply better inputs" is useless
advice here and correct advice elsewhere.

### Value semantics

whatbreaks reasons about **schema**: which columns exist and what feeds them. It
does not know whether a change alters the *values* in a column. Rewriting
`sum(x)` to `avg(x)` keeps the same column and the same input, and is reported
as no change.

That question needs to query your warehouse, which is
[Recce](https://github.com/DataRecce/recce)'s and Datafold's job. They answer it
well.

### Python models

Out of scope. Their `raw_code` is Python, and they are reported by name with
reason `python_model` rather than as a SQL parse error — a misleading
diagnostic sends people hunting for a bug in SQL they never wrote.

---

## What it does not do *yet*

Deliberate omissions, not oversights. Each is inference rather than fact, and
shipping a guess as a finding is the failure mode this project exists to avoid.

| Not yet | Why it waits |
| --- | --- |
| Renames (`WB004`) | Indistinguishable from remove + add without heuristics. Ships once the lineage engine is trustworthy enough that "identical upstream lineage" is a reliable signal |
| Expression changes (`WB005`) | Changes values, not schema. Conflating "your pipeline errors" with "your numbers moved" is the main source of alert fatigue in this category, so it needs its own severity |
| Contract / materialization changes (`WB006`–`WB008`) | Straightforward; simply not written yet |
| Type changes (`WB009`) | Needs `catalog.json` to be meaningful |
| GitHub Action | The CLI is the product; the Action is packaging. It arrives with the fork-safe two-workflow pattern, not before |
| Suppressions (`# whatbreaks: ignore`) | Needed before anyone runs this on a large project in anger |

---

## Accuracy, measured

From [ADR 000](adr/000-feasibility.md), across 7 public dbt projects, 164 models,
no warehouse:

| | |
| --- | ---: |
| Output columns resolved exactly | 75.6% |
| Any usable schema | 83.5% |
| Analytics-style projects only | 93.3% |
| dbt *packages* (macro-heavy, a harder population) | 73.8% |

The analytics figure rests on only 2 projects and 15 models. It is directionally
right and under-evidenced, and the gate decision deliberately leaned on the
package subset as a conservative lower bound instead. That caveat is recorded
rather than smoothed over.

---

## Known sharp edges

- **Model name collisions.** `relation_key` is derived from a model's name, so
  two packages each defining `orders` collide. Mitigated by scoping schema
  lookups to a node's actual `depends_on` parents, so it only matters if one
  model depends on both. Not yet detected explicitly.
- **Dialect coverage is uneven.** `sqlglot` handles the parsing, and its
  support varies by dialect. A model that fails to parse is reported, not
  silently skipped, but the failure rate differs across warehouses.
- **`dbt parse` must succeed first.** Getting a manifest turned out to be harder
  than analysing one: 7 of 14 public projects would not parse in a clean room
  (adapter mismatch, version drift, missing vars, absent source tables). That is
  a property of the ecosystem rather than of whatbreaks, but you will meet it.
- **No incremental caching yet.** Every run analyses the whole project. Fine at
  the sizes measured; it will need attention before it meets the plan's
  performance target on very large projects.

---

## How uncertainty is reported

Two orthogonal axes, and they are not interchangeable:

- **Severity** — how bad is it if true? `breaking` / `possibly_breaking` /
  `safe` / `info`.
- **Confidence** — how sure are we? `confirmed` / `likely` / `unknown`.

Findings built from SQL that whatbreaks rendered itself are capped at `likely`.
`confirmed` requires evidence from dbt rather than from our reconstruction —
which in practice means either dbt-compiled SQL, or a fact taken straight from
the manifest such as a dangling `ref()`.

If analysis was incomplete, the report says so and states plainly that absence
of findings is not proof of safety.
