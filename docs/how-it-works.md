# How it works

Two `manifest.json` files in, findings out. No warehouse, no network, no state.

```
manifest (base) ─┐
                 ├─→ recover SQL ─→ infer schemas ─→ column graph ─┐
manifest (head) ─┘                                                  │
                                          findings ←─ diff the graphs
```

Each stage is a pure function over immutable data, which is what lets every one of them be tested in
isolation and makes the output byte-deterministic.

---

## 1. Recover the SQL

A `manifest.json` from `dbt parse` contains `raw_code` — Jinja, not SQL. Three sources are tried,
best first:

| Source | When available |
| :--- | :--- |
| `compiled_code` in the manifest | only from `dbt compile` / `dbt build` |
| `target/compiled/**` on disk | same precondition |
| Offline Jinja rendering | always |

Phase 0 measured `compiled_code` at **0%** for `dbt parse` output, so offline rendering is the real
path rather than a fallback.

**The manifest already contains every macro's source.** Package macros do not need faking — they are
compiled out of `manifest.macros` and executed. Measured effect: renderability goes from **34% to
80%**. Two details are load-bearing:

- `manifest.macros` also holds `{% materialization %}`, `{% test %}` and `{% snapshot %}` blocks,
  which plain Jinja2 cannot parse. One of them fails an entire package's compile.
- Macros must be compiled **twice**. A macro compiled against a context that does not yet contain the
  other packages' namespaces sees `dbt_utils` as undefined *from inside its own body*, even though
  models can see it fine.

An unresolvable macro is **never** rendered to empty string. That produces SQL which parses cleanly
and means something else entirely — silent wrongness is the failure mode this project exists to
prevent. It fails loudly, with a name: `introspective:run_query` (unfixable offline) reads very
differently from `undefined_macro:dbt_utils.star` (something is missing).

## 2. Infer output columns

One topological pass. Leaves are seeded from what is knowable offline — source YAML, and seed CSV
headers, which are free schema sitting on disk. Each model is then qualified against its own parents'
schemas, and its output columns fall out of the qualified projection.

**The rule that makes this viable:**

> Raw `SELECT *` presence is **not** the uncertainty signal.
> A star that **survives** qualification is.

The dominant dbt idiom ends every model in `select * from final`. Around half of real models contain
a star — but almost all are over a CTE with an explicit projection, which `sqlglot` expands against
an empty schema. Treating raw star presence as uncertainty scored 0% exact on jaffle_shop; asking
whether a star survived scores 100%. Same code, same data, opposite conclusion.

Unknown-ness propagates only where it bites. A model selecting explicit columns from an unknown
parent still knows its own output names; poisoning the whole descendant chain would make the tool
useless.

Every model ends up `exact`, `partial` or `unknown`, each with a reason — and the reason matters,
because `surviving_star` is fixable with a `catalog.json` and `needs_warehouse` is not fixable by any
static tool.

## 3. Build the column graph

`sqlglot.lineage` does the AST work. What is added on top:

- **Leaf tables map back to manifest node ids**, so an edge points at a dbt model rather than a bare
  identifier.
- **Every edge carries a confidence**, from the same algebra as everything else.
- **Edges record *why* they exist.** "You dropped a column this model selects" and "you dropped a
  column this model joins on" are different messages, and `SELECT *` is different again — it does not
  error when a column vanishes, it silently produces a narrower result.
- **Columns needed only for filters, joins and grouping are tracked separately.** They never reach an
  output column, so projection lineage misses them entirely — yet removing one breaks the model
  outright. On the validation sample this was 162 dependencies that would otherwise have been
  invisible.

## 4. Diff the graphs

**This is a graph diff, not a text diff**, and that is the reason the tool works.

A text diff reports every reformatting and CTE rename as a change, and misses the case that actually
hurts: a model whose output columns changed because an *upstream* `SELECT *` changed, with no edit to
its own file.

One honesty rule governs the comparison:

> A column is reported removed only if we **knew** the base schema.

If base resolution was `unknown`, its absence in head is our ignorance, not a finding.

Blast radius is computed on the **base** graph, and this is not interchangeable with head. Once a
column is gone from its parent nothing resolves against it, so the head graph shows no consumers at
all and would answer "nothing breaks" for every removal. Base says what depended on the column; head
answers the separate question of whether the author already updated those consumers.

## 5. Classify

| | |
| :--- | :--- |
| **Severity** | how bad if true — `breaking`, `possibly_breaking`, `safe`, `info` |
| **Confidence** | how sure we are — `confirmed`, `likely`, `unknown` |

They are orthogonal and stay that way. Three things can weaken a claim, and the weakest wins: schema
resolution, whether the SQL was dbt's own compiled output or our reconstruction, and whether a
heuristic was involved.

Findings built from SQL that whatbreaks rendered itself are capped at `likely`. `confirmed` requires
evidence from dbt rather than from our reconstruction — which in practice means dbt-compiled SQL, or
a fact taken straight from the manifest such as a dangling `ref()`.

## Verification

The claims above are checked rather than asserted:

| | |
| :--- | :--- |
| [`tools/validate_recovery.py`](../tools/validate_recovery.py) | renderability against real manifests |
| [`tools/validate_inference.py`](../tools/validate_inference.py) | schema resolution vs the Phase 0 baseline |
| [`tools/validate_graph.py`](../tools/validate_graph.py) | graph build at scale, and that no edge claims unearned confidence |
| [`tools/benchmark.py`](../tools/benchmark.py) | the performance target, and determinism |
| `tests/unit/test_lineage_invariant.py` | a mechanical oracle: if we claim D depends on U, removing U **must** break qualification |
| [`tests/false_positives/`](../tests/false_positives/) | 15 no-op changes that must raise no alarm — a release gate |

The invariant oracle is the one worth singling out. Every lineage tool asserts correctness against
its own expectations, which is a closed loop. `sqlglot`'s `validate_qualify_columns` is an
independent judge that knows nothing about our graph, and a third test asserts the judge actually
judges — so the properties cannot pass vacuously.
