# WHATBREAKS_PROJECT_PLAN.md

> **Note on this file.** This document *is* the deliverable. When execution begins, it should be
> committed to the new repository root as `WHATBREAKS_PROJECT_PLAN.md` (or split into
> `docs/adr/` + `ROADMAP.md`). No implementation code is proposed here.

---

## Context — why this document exists

The goal is a production-quality open-source developer tool that answers one question before a
SQL/dbt change merges: *"what downstream data will this break?"*

The brief asked for critical evaluation before planning. That evaluation materially changed the
design. Two findings drove every decision below:

1. **The lineage engine is commoditized.** `sqlglot` ships column-level lineage. Recce (Apache-2.0,
   ~469★), SQLMesh, dbt-column-lineage (MIT, ~75★), dbt Cloud Explorer, and Datafold all build on
   or reimplement it. *Building another column-level lineage engine is not a differentiator.*
2. **Every existing CI impact tool requires heavy infrastructure.** Atlan, Sifflet, Metaplane and
   DataHub's actions all require a hosted catalog. Recce and dbt-column-lineage require a live
   warehouse connection (`catalog.json` comes from `dbt docs generate`). **Nothing exists that is
   purely static — no warehouse, no secrets, no backend, no second environment.**

Finding 2 is the wedge. Finding 1 means the wedge must be defended on *rigor*, not features.

**Confirmed direction (user-selected):** static zero-dependency linter, optimizing for technical
depth (correctness, uncertainty modelling, difficult-SQL corpus) over adoption polish.

---

## 1. Executive summary

**whatbreaks** is a static analyzer for dbt projects that classifies the breaking-change risk of a
Git diff and reports the column-level blast radius — with **no warehouse connection, no hosted
service, and no secrets**.

It is positioned as a **linter**, not a catalog: closer in spirit to `mypy` and `ruff` than to
DataHub or Atlan. Deterministic, fast, stable rule IDs, suppressible, exit codes, one JSON schema.

Three commitments define the product:

- **Never overclaim.** Every finding carries a *severity* and an independent *confidence*, plus
  file/line evidence. Analysis coverage is always reported. A clean result is never printed when
  coverage is partial.
- **Never require infrastructure.** The floor is `dbt parse` artifacts, which work offline. Better
  metadata (`catalog.json`, `target/compiled/`) is auto-detected and silently upgrades confidence,
  but is never required.
- **Fork-safe by construction.** Because it needs no secrets, it is the only tool of its class that
  can run on pull requests from forks — where `GITHUB_TOKEN` is read-only and secrets are withheld.

**Central technical insight:** whatbreaks is a **graph differ, not a text differ**. It builds the
column-level output contract of every model on the base commit and the head commit, then diffs the
two graphs. Text diffs tell you which files changed; graph diffs tell you which *contracts* changed
— including the important case where a model's output columns change without its file being edited,
because an upstream `SELECT *` changed.

**Central technical risk:** without schema information, `SELECT *` cannot be expanded and joined
columns cannot be disambiguated. The entire zero-warehouse thesis rests on whether real dbt projects
are analyzable without a catalog. **Phase 0 exists solely to measure this before any product code is
written.** If Tier-1 coverage on public projects is below ~70%, the wedge is wrong and the plan
should be revised, not forced.

---

## 2. Problem definition

### The observable failure

An engineer renames or drops a column in a dbt model. dbt's own tests pass. The PR merges. Three
hops downstream, a model that referenced that column now fails — or worse, silently produces nulls —
and a dashboard is wrong for a week before anyone notices.

### Why existing safeguards don't catch it

| Safeguard | Why it misses this |
| --- | --- |
| `dbt build` in CI | Requires a warehouse and a full run; slow, expensive, often deferred or skipped |
| `dbt ls --select model+` | **Model-level only.** Flags 40 downstream models when 2 actually touch the column. The noise makes it ignored |
| dbt model contracts | Only apply to models explicitly `contract: {enforced: true}` **and** versioned. Community consensus is that the check is simultaneously too narrow and too aggressive |
| dbt tests | Test the data, not the schema dependency. A dropped column that nothing asserts on passes cleanly |
| Code review | A human cannot hold a 400-model column graph in their head |

### The precise problem statement

> Given a Git diff in a dbt project, determine — statically, offline, and with explicit confidence —
> which downstream columns, tests, and exposures depend on the specific columns that changed.

### The smallest painful problem worth solving

**Column removal and model removal.** These are the highest-frequency, highest-confidence,
highest-damage cases, and they are the only ones detectable with near-certainty from static analysis
alone. Everything else (renames, type changes, expression changes) is inference and belongs later.

**This is the MVP. Nothing else.**

---

## 3. Target users

**Primary user — the analytics engineer on a 50–800 model dbt project.**
Works in SQL and YAML daily. Owns a dbt repo with GitHub PRs. Has a warehouse but *not* necessarily
credentials wired into every CI job. Has been burned by exactly this failure. Will install a tool
that takes under five minutes and produces zero false positives; will uninstall one that produces
noise.

**Secondary user — the data platform / infra engineer.**
Owns the CI pipeline. Cares about runtime, cost, secret hygiene, and fork safety. Is the person who
will *reject* a tool that requires warehouse credentials in a PR workflow.

**Tertiary user — the OSS dbt package maintainer.**
Receives PRs from forks. Currently has *no* option: every existing impact tool needs secrets that
fork PRs cannot access. This user is small in number but is a genuine zero-alternative constituency
and therefore a disproportionately good early-adopter and advocate.

**Explicit non-users:**
- Enterprise data governance teams wanting a searchable catalog → use DataHub/OpenMetadata
- Teams wanting to know *whether the data values changed* → use Recce or Datafold (different question)
- Teams already on SQLMesh → they have this natively

---

## 4. Product principles

1. **Honest over complete.** An `UNKNOWN` with a reason beats a confident wrong answer. Coverage is
   always reported.
2. **A linter, not a platform.** No server, no database, no web UI, no daemon, no account.
3. **Evidence, not graphs.** Every finding cites a file, a line, and an expression. The output should
   read like a compiler error, not a visualization.
4. **Zero required infrastructure.** Better metadata improves results; nothing is mandatory beyond
   `dbt parse`.
5. **Deterministic.** Same inputs → byte-identical output. No network calls, no LLMs, no sampling.
6. **Escape hatches everywhere.** Stable rule IDs, inline suppressions, config-file ignores. A tool
   with no `# noqa` gets uninstalled on its first false positive.
7. **Minimal dependency surface.** Target ≤4 runtime dependencies. This is a CI tool; install time
   and supply-chain surface are features.
8. **Silence is the default success state.** Print nothing when nothing is wrong, except the coverage
   line.

---

## 5. Competitive landscape

### Direct — column-level lineage for dbt

| Tool | License | Requires | Overlap | Why it doesn't close the gap |
| --- | --- | --- | --- | --- |
| **Recce** | Apache-2.0, ~469★ | Warehouse connection + **two dbt environments** | High — SQLGlot-based breaking-change analysis, categorizes breaking/partial/non-breaking, CI integration | It is a *data validation* tool. It answers "did the data change?" by querying. Cannot run without a warehouse; PR gating is a paid-cloud feature |
| **dbt-column-lineage** (Fszta) | MIT, ~75★ | `manifest.json` + **`catalog.json`** | Medium — sqlglot CLL, "Analyze Impact" | Local interactive web UI on port 8000. No CI surface, no diff analysis, no exit codes. `catalog.json` requires a warehouse |
| **SQLMesh** | Apache-2.0 | Adopting SQLMesh as your transformation framework | High — native CLL, automatic breaking/non-breaking categorization, semantic diffing | Requires migrating off dbt-core's execution model. This is a framework decision, not a tool install |
| **dbt Cloud Explorer** | Proprietary | dbt Cloud, Enterprise tier | High — native CLL on Snowflake/BigQuery/Databricks/Redshift | Paid, hosted, dbt Cloud only |
| **Datafold** | Proprietary | SaaS + warehouse | Very high — this is the category leader for CI impact analysis, incl. BI lineage | Commercial. Its OSS `data-diff` was **archived May 2024** — a deliberate retreat from OSS that leaves the free tier of this category thinner than it was |

### Direct — PR-comment impact actions

| Action | Backend required |
| --- | --- |
| `acryldata/dbt-impact-action` | DataHub deployment |
| `atlanhq/dbt-action` | Atlan SaaS |
| Sifflet dbt impact action | Sifflet SaaS |
| Metaplane | Metaplane SaaS |

**All four are thin clients over a hosted catalog's lineage API.** None performs analysis locally.
None can run on a fork PR. This is the clearest, most defensible statement of the gap.

### Adjacent — do not compete with these

- **`sqlglot`** — the foundation, not a competitor. whatbreaks is a consumer.
- **`sqllineage`** — table+column lineage, but not dbt-aware and no CI story.
- **`dbt-checkpoint`, `dbt-project-evaluator`, `SQLFluff`** — dbt CI linters, but for *style and
  convention*, not semantic impact. Good precedent for CLI ergonomics and adoption pattern; worth
  studying, not duplicating.
- **OpenLineage / Marquez** — runtime lineage from execution events. Orthogonal (post-hoc, not
  pre-merge).

### Features that should NOT be copied

- **Interactive lineage graph UI.** Everyone has one. It is a maintenance sink, it does not fit in a
  PR, and it directly contradicts the "linter not platform" principle.
- **Data value diffing.** Requires a warehouse. That is Recce's and Datafold's game; entering it
  forfeits the entire wedge.
- **A catalog / metadata store.** The moment whatbreaks persists state to a server, it becomes a
  worse DataHub.
- **LLM-based lineage inference.** Non-deterministic, unexplainable, and unfalsifiable — the exact
  opposite of the product's value proposition.

---

## 6. Differentiation

whatbreaks wins on exactly five properties, in priority order:

1. **Zero infrastructure.** `pip install whatbreaks` + `dbt parse`. No warehouse, no secrets, no
   account, no server. *No competitor in the CI-impact category has this.*
2. **Fork-safe.** A direct consequence of (1). The only tool in its class that can analyze a pull
   request from a fork, where `GITHUB_TOKEN` is read-only and secrets are unavailable.
3. **An explicit uncertainty model.** Severity and confidence are orthogonal axes. Coverage is
   always reported. This is the correctness property everyone else papers over — the honest
   competitor claim is *"we could not analyze 18 of your 430 models, here is each reason."*
4. **Graph diffing, not text diffing.** Detects breakage in models whose files were never touched
   (upstream `SELECT *` shifted). This class of bug is invisible to file-diff-based tooling.
5. **Empirically validated predictions.** The test suite materializes real dbt projects on DuckDB,
   applies real breaking changes, and asserts that whatbreaks predicted what actually broke. Almost
   no lineage tool has a ground-truth oracle. (See §14 and §28.)

### Honest assessment of the odds

This is a crowded field and whatbreaks is not going to out-feature Datafold or out-fund Recce. The
realistic ceiling is a *well-regarded niche tool* — hundreds of stars, a few thousand installs, real
users in OSS dbt packages and secret-averse platform teams. That is a genuinely good outcome for a
solo project and an excellent technical calling card. **It is not a business.** Planning it as one
would be a mistake.

The strategy that fails is trying to become a small Recce. The strategy that works is being
*obviously the right tool for one narrow job* and unusually rigorous about correctness.

---

## 7. Functional requirements

### MVP (must)

- **F1** Accept two dbt `manifest.json` files (base, head), or a Git ref from which to derive base.
- **F2** Recover analyzable SQL per model, preferring pre-compiled SQL when available, falling back
  to sandboxed Jinja rendering with a stubbed dbt context.
- **F3** Infer each model's output column set via topologically-ordered schema propagation, seeded
  from YAML-declared columns and source definitions.
- **F4** Build a column-level dependency graph using `sqlglot.lineage`.
- **F5** Diff base and head graphs; classify `MODEL_REMOVED`, `COLUMN_REMOVED`, `COLUMN_ADDED`.
- **F6** Compute blast radius: transitively reachable downstream **columns**, plus affected dbt
  **tests** and **exposures**.
- **F7** Attach severity + confidence + file/line evidence to every finding.
- **F8** Always emit a coverage report (models analyzed / total, with per-model failure reasons).
- **F9** Emit three formats: human terminal, `--format json` (versioned schema), `--format markdown`.
- **F10** Exit codes governed by `--fail-on` (default: `breaking`).

### v0.x (should)

- **F11** GitHub Action using the fork-safe two-workflow pattern (§13).
- **F12** Content-addressed caching, CI-cache-restorable.
- **F13** Rename inference (removed + added column with identical upstream lineage → `LIKELY`).
- **F14** Expression-change detection → `POSSIBLY_CHANGED_VALUES` (distinct from schema breakage).
- **F15** Config-level breaks: contract removal, materialization change, incremental `unique_key` change.
- **F16** Stable rule IDs + inline/config suppressions.
- **F17** Optional `catalog.json` tier → `SELECT *` expansion, type-change detection.

### Explicitly deferred (§25 covers the permanent exclusions)

Rename *detection with certainty*, non-dbt SQL projects, BI-layer traversal, Python models,
cross-repo lineage, any UI.

---

## 8. Non-functional requirements

| Requirement | Target |
| --- | --- |
| **Runtime** (500 models, cold, no cache) | < 20s |
| **Runtime** (500 models, warm cache, small diff) | < 5s |
| **Memory** (1000 models) | < 500 MB |
| **Runtime dependencies** | ≤ 4 (`sqlglot`, `jinja2`, a CLI lib, optionally `packaging`) |
| **Python support** | 3.10 – 3.13 |
| **dbt manifest schema support** | v10, v11, v12 (dbt-core ≥ 1.6), version-detected, explicit error on unknown |
| **Network calls** | Zero, always. Enforced by a test that blocks sockets |
| **Determinism** | Byte-identical output for identical inputs; verified in CI |
| **False-positive rate on the no-op corpus** | 0 (this is a release gate, not an aspiration) |
| **Install size** | < 20 MB |
| **Cold start** | < 500 ms to first output on a trivial project |

---

## 9. Architecture proposal

### Pipeline

```
manifest(base) ─┐
                ├─→ [1] Load ─→ [2] SQL Recovery ─→ [3] Schema Inference ─→ [4] Column Graph ─┐
manifest(head) ─┘                                                                             │
                                                                                              ▼
                                        [7] Report ←─ [6] Blast Radius ←─ [5] Graph Diff ────┘
```

Each stage is a pure function over immutable dataclasses. No stage performs I/O except [1] and [7].
This makes every stage independently golden-file testable, which is the backbone of §14.

---

### Decision 1 — How to obtain analyzable SQL

**Alternatives**

| Option | Trade-offs |
| --- | --- |
| **A.** Use `dbt-core` as a library to compile | Highest fidelity. But requires an adapter and a live connection → destroys fork-safety, and couples to unstable dbt internals. **Rejected.** |
| **B.** Render Jinja ourselves with a stubbed dbt context | Fully offline. Handles the majority of models. Fails on introspective macros (`run_query`, `adapter.*`, `dbt_utils.star()`). Adds a Jinja execution surface |
| **C.** Require `dbt compile` and read `target/compiled/` | Zero parsing risk, perfect fidelity. But requires a warehouse |
| **D.** Regex-strip Jinja | Unreliable in ways that produce *silent* wrong answers. **Rejected outright** — violates principle 1 |

**Recommendation: C-when-available, B-as-floor — with a mandatory macro-compilation stage.**

> **Revised after Phase 0 (ADR 000 F1, F6).** Two measured corrections to this decision:
> `dbt parse` yields `compiled_code` for **0%** of models, so option C is only available to users
> who already run `dbt compile` against a warehouse — B is the primary path, not the fallback. And
> option B is only viable *with* compiled manifest macros: naive stubbing renders **34.1%** of
> models, compiling `manifest.macros` raises it to **80.5%**. Macro compilation is therefore a
> required stage, not an enhancement. See R10.

Detection order per model:
1. `manifest.nodes[*].compiled_code` populated (manifest came from `dbt compile`/`dbt build`) → use it.
2. `target/compiled/**/*.sql` present on disk → use it.
3. Otherwise → render `raw_code` via `jinja2.sandbox.SandboxedEnvironment` against a context built
   from **(a)** every `{% macro %}` in `manifest.macros`, compiled in two passes so macros can call
   each other across packages, plus **(b)** a stub dbt context where `ref()`/`source()` return
   placeholder relation identifiers, `config()`/`log()` are no-ops, `var()`/`env_var()` return
   declared defaults, and `adapter.dispatch` resolves `<adapter>__x` → `default__x`.
4. Rendering raises, or the result fails `sqlglot.parse_one` → model is `UNPARSEABLE`. **Never guess.**

Introspective constructs (`run_query`, `adapter.get_relation`, `load_result`) must stay hard
failures. Rendering an unresolvable macro to `""` yields syntactically valid but semantically wrong
SQL — the exact failure mode this tool exists to prevent.

The confidence attached to every finding records which tier produced it.

**Note on the model-level DAG:** dbt's manifest already resolves `ref()` and `source()` into
`depends_on.nodes`. whatbreaks **must not** re-parse `ref()` to build the model graph — that graph is
free and authoritative. Jinja rendering is needed only to make the SQL body parseable.

---

### Decision 2 — Schema inference without a warehouse

This is the technical heart of the project.

**Alternatives**

| Option | Trade-offs |
| --- | --- |
| **A.** Require `catalog.json` | Accurate. Requires a warehouse. **Rejected as a requirement**, accepted as an optional tier |
| **B.** No schema at all | `SELECT *` unexpandable, joined columns unresolvable. Coverage would be unusably low |
| **C.** Topologically-ordered inference seeded from YAML | Offline. Accuracy depends on how well sources are documented. Unknowns propagate downstream |

**Recommendation: C, with A as an auto-detected upgrade.**

Algorithm:
1. Topologically sort models using `depends_on` from the manifest.
2. Seed leaf schemas from `sources[*].columns` declared in YAML.
3. For each model in order, run `sqlglot.optimizer.qualify` with the accumulated schema, then derive
   the model's output column set from the qualified projection.
4. If a model cannot be fully resolved (unexpandable `SELECT *` on an unknown parent, unparseable
   SQL, ambiguous join column), mark its schema `PARTIAL` or `UNKNOWN` and **propagate that state to
   every descendant.**
5. Where a model has YAML-declared `columns`, use them to *corroborate or repair* the inferred set —
   and surface any mismatch as its own diagnostic (a genuinely useful side-effect: it tells users
   their docs are stale).

The propagation of unknown-ness is what makes the confidence model honest rather than decorative,
and it is the single most important piece of code in the project.

---

### Decision 3 — Change detection strategy

**Alternatives**

| Option | Trade-offs |
| --- | --- |
| **A.** Git text diff of `.sql`/`.yml` | Cheap. But misses indirect breakage entirely, and produces noise on reformatting |
| **B.** Two manifests, diff the *nodes* | dbt's own Slim CI pattern. Model-level only |
| **C.** Two manifests, diff the *computed column graphs* | Catches indirect breakage. Immune to formatting noise. Costs a second full analysis pass |

**Recommendation: C.**

The base manifest is obtained by `git worktree add` on the merge-base and running `dbt parse` there —
cheap, offline, and requiring no stored artifact. If the user supplies a base manifest (e.g. from an
artifact store), use it and skip the worktree.

C is what makes formatting-only PRs produce zero findings, which is the difference between a tool
people keep and a tool people mute.

---

### Decision 4 — Graph representation

**Alternatives:** `networkx` (batteries included, extra dep) · `rustworkx` (fast, heavy wheel) ·
hand-rolled dataclasses + adjacency dicts.

**Recommendation: hand-rolled.** The only operations needed are topological sort and reverse
reachability from a seed set. Both are ~30 lines. Given NFR "≤4 dependencies," pulling in a graph
library to avoid writing a topological sort is a bad trade for a CI tool.

---

### Decision 5 — CLI framework

**Alternatives:** `argparse` (stdlib, verbose) · `click` (mature, ubiquitous) · `typer` (nice, pulls
`click` + `rich`).

**Recommendation: `click`.** Mature, single dependency, no transitive `rich`. Terminal output should
be hand-formatted with ANSI codes and degrade to plain text when not a TTY — a `rich` dependency is
not worth it for a tool whose primary output surface is CI logs and markdown.

---

### Decision 6 — GitHub Action packaging

**Alternatives:** JS/TS action (fast, needs committed `dist/`, must shell to Python anyway) ·
Docker action (reproducible, slow image pull, Linux-only) · **composite action**.

**Recommendation: composite action.** `setup-python` → `pip install whatbreaks` → run CLI → post
comment via the `gh` CLI already present on runners. No build step, no committed `dist/`, no second
language in the repo, and the CLI stays the single source of truth. Comment posting is a `gh pr
comment --edit-last` call, which gives idempotent single-comment updates for free.

---

### Decision 7 — Output schema stability

The JSON output is a public API. It carries `schema_version` independent of the tool version.
Versioning policy in §22.

---

## 10. Data / lineage model

```
Project
 └── Model            id, path, dialect, materialization, contract, sql_tier, parse_status
      ├── ColumnRef   name, model_id, inferred_type?, source_expression, line, col
      └── Schema      columns[], resolution: EXACT | PARTIAL | UNKNOWN, reason?

ColumnEdge            downstream: ColumnRef
                      upstream:   ColumnRef
                      kind:       DIRECT | EXPRESSION | AGGREGATE | JOIN_KEY | STAR_EXPANDED
                      confidence: CONFIRMED | LIKELY | UNKNOWN
                      evidence:   file, line, snippet

Consumer              dbt tests, exposures, downstream models — nodes that can be "broken"
```

**`kind` matters for classification.** A `DIRECT` edge (`SELECT a AS b`) breaks hard when `a` is
removed. An `AGGREGATE` edge (`SUM(a) AS total`) breaks hard too. A `JOIN_KEY` edge means the column
is used in a predicate — removing it breaks the query but doesn't change the projection. These
warrant different messages, and conflating them is how tools become noisy.

**`STAR_EXPANDED` edges are always at most `LIKELY`** unless a catalog confirmed the expansion.

---

## 11. Change-impact model

### Classifications

| ID | Change | Severity | Max confidence | Release |
| --- | --- | --- | --- | --- |
| `WB001` | Column removed from model output | BREAKING | CONFIRMED | **MVP** |
| `WB002` | Model removed or renamed | BREAKING | CONFIRMED | **MVP** |
| `WB003` | Column added | SAFE | CONFIRMED | **MVP** |
| `WB004` | Column likely renamed (removed+added, identical upstream lineage) | BREAKING | LIKELY | v0.3 |
| `WB005` | Column expression changed (same name, different lineage/AST) | POSSIBLY_BREAKING | LIKELY | v0.3 |
| `WB006` | Contract enforcement removed | BREAKING | CONFIRMED | v0.3 |
| `WB007` | Materialization changed | POSSIBLY_BREAKING | CONFIRMED | v0.3 |
| `WB008` | Incremental `unique_key` changed | POSSIBLY_BREAKING | CONFIRMED | v0.3 |
| `WB009` | Column type changed | BREAKING | CONFIRMED *(catalog required)* | v0.4 |
| `WB010` | Column added to a model consumed via `SELECT *` downstream | POSSIBLY_BREAKING | LIKELY | v0.4 |
| `WB900` | Model unanalyzable | INFO | — | **MVP** |

### Why `WB004` (rename) is deliberately *not* MVP

A rename is indistinguishable from "removed one column, added an unrelated one" without heuristics.
Getting it wrong in either direction is costly: a false rename hides a real break; a missed rename
produces a scary-but-wrong "column removed." It ships once the lineage engine is trustworthy enough
that "identical upstream lineage" is a reliable signal — which is a *consequence* of MVP quality, not
a precondition.

### Why `WB005` (expression change) is separated from schema breakage

An expression change does not break the query — it changes the *values*. Conflating "your pipeline
will error" with "your numbers may move" is the single most common source of alert fatigue in this
category. They get different severities, different sections in the report, and independent
`--fail-on` thresholds.

### Severity × confidence matrix — what fails CI

| | CONFIRMED | LIKELY | UNKNOWN |
| --- | --- | --- | --- |
| **BREAKING** | ❌ fail (default) | ⚠ warn | ⚠ warn |
| **POSSIBLY_BREAKING** | ⚠ warn | ⚠ warn | ⚠ warn |
| **SAFE** | ✓ | ✓ | ⚠ warn |

**`--fail-on` default is `breaking`, which means only the CONFIRMED/BREAKING cell fails.** Failing
CI on uncertainty is how a linter gets removed from a repo in week two. Uncertainty is reported
loudly and blocks nothing unless explicitly configured to.

---

## 12. Reliability strategy

The governing rule: **the tool must never present an incomplete analysis as a complete one.**

### Coverage reporting — always, unconditionally

```
whatbreaks: analyzed 412/430 models (95.8%)
  18 not analyzed:
     12  unresolvable Jinja macro (run_query / adapter call)
      4  unexpandable SELECT * (upstream schema unknown)
      2  SQL parse error (dialect: snowflake)
  Run `whatbreaks coverage --explain` for per-model detail.
```

This prints on success too. A "no breaking changes found" line with no coverage line is a lie by
omission.

### Failure taxonomy and behavior

| Condition | Behavior |
| --- | --- |
| Model's Jinja unrenderable | Mark `UNPARSEABLE`, propagate UNKNOWN downstream, `WB900`, continue |
| SQL fails `sqlglot.parse_one` | Same, with the dialect and error position recorded |
| `SELECT *` on unknown-schema parent | Model schema = `PARTIAL`; downstream edges capped at `LIKELY` |
| Ambiguous column across joins | Emit an edge to *every* candidate, confidence `UNKNOWN`, list candidates in evidence |
| Manifest schema version unrecognized | **Hard error, exit 2.** Do not attempt best-effort parsing of an unknown artifact format |
| Base manifest unobtainable | Hard error with actionable guidance. Do not silently degrade to single-commit analysis |
| Cycle in the model DAG | Hard error — dbt itself forbids this, so its presence means the manifest is corrupt |
| Analysis exceeds `--timeout` | Emit partial results **clearly labelled partial**, exit 2 |

### The anti-silence invariant

A regression test asserts: for every fixture, if any model is `PARTIAL` or `UNKNOWN`, the rendered
report contains a visible uncertainty notice. This is tested, not merely intended.

---

## 13. Security model

whatbreaks runs inside CI, on untrusted pull-request content. This is treated as a primary design
constraint, not an afterthought.

### Threat model

| Threat | Mitigation |
| --- | --- |
| **Malicious SQL/Jinja in a PR achieving RCE** | Never execute SQL. Jinja rendered in `jinja2.sandbox.SandboxedEnvironment` with no filesystem loader, no `import`, a wall-clock timeout, and an output size cap. Note: the user has already run `dbt parse`, which executes project Jinja unsandboxed — whatbreaks adds no *new* execution surface, but sandboxes anyway |
| **Secret exfiltration** | The tool makes **zero network calls** and reads **zero credentials**. It never parses `profiles.yml` for anything but `type`. A test suite that blocks all sockets enforces this |
| **"Pwn request" via `pull_request_target`** | **The Action must never use `pull_request_target` with a checkout of PR head.** This is the documented critical anti-pattern; the docs will say so explicitly and explain why |
| **Fork PRs cannot post comments** | Fork `pull_request` runs get a read-only `GITHUB_TOKEN` and no secrets. Handled by the **two-workflow pattern** below |
| **Markdown/HTML injection into the PR comment** | Model names, column names and SQL snippets are untrusted input. Escape all interpolated content; fence all snippets; strip `@`-mentions; hard-cap comment length |
| **Malicious `manifest.json`** | Treat as untrusted: validate schema version, cap node count, cap string lengths, guard against zip-bomb-style nesting |
| **Path traversal via manifest paths** | Resolve all file reads against the project root and reject escapes |
| **Supply chain** | ≤4 runtime deps; `uv.lock` committed; Dependabot; `pip-audit` in CI; sdist + wheel published with PyPI Trusted Publishing (OIDC, no long-lived token); the Action pinned by SHA in all documented examples |

### The fork-safe two-workflow pattern (shipped as the default template)

```
Workflow 1  ·  on: pull_request        ·  permissions: contents: read
   Runs the analysis on untrusted PR code. No secrets. Read-only token.
   Uploads report.json + PR number as an artifact. Never posts anything.

Workflow 2  ·  on: workflow_run        ·  permissions: pull-requests: write
   Triggered by workflow 1's completion. Checks out NOTHING from the PR.
   Downloads the artifact, validates it, posts/updates the comment.
```

This is the only pattern that is both fork-compatible and safe. **The documentation will present it
as the default and explain the vulnerability in the naive alternative** — this is itself a
credibility signal, since most tools in this space get it wrong.

### Safe defaults summary

- Read-only filesystem access outside the cache directory
- No network, ever
- No SQL execution, ever
- `--fail-on=breaking` (fail only on confirmed breakage)
- Action pinned by SHA in all docs
- Least-privilege `permissions:` blocks in every example workflow

---

## 14. Testing strategy

Given the "technical depth" priority, this is where disproportionate effort goes. **The test corpus
is the actual product moat.**

### Layer 1 — Unit tests
Pure functions: topological sort, reverse reachability, config resolution, suppression matching,
exit-code derivation.

### Layer 2 — Lineage golden-file tests
Each fixture is a directory:

```
tests/corpus/cte_nested_alias_shadowing/
  input.sql
  schema.json
  dialect.txt
  expected.json          # full lineage graph
  support_level.txt      # SUPPORTED | DEGRADED | UNSUPPORTED
```

**The critical design choice:** the test asserts the *support level and confidence*, not only the
lineage. A fixture tagged `DEGRADED` **must** produce `LIKELY`/`UNKNOWN` confidence. This makes
"never overclaim" a mechanically enforced property rather than a slogan.

### The hard-SQL corpus — build order

Organized by construct, each with SUPPORTED/DEGRADED/UNSUPPORTED classification:

1. **Baseline:** simple projections, aliases, table-qualified and unqualified columns
2. **CTEs:** single, chained, nested, name-shadowing a real table, recursive
3. **Set ops:** `UNION` / `UNION ALL` / `INTERSECT` / `EXCEPT` — *column lineage by position, not
   name*, which is a classic source of silent wrongness
4. **Joins:** inner/left/self/cross, `USING`, `NATURAL`, ambiguous unqualified columns, alias shadowing
5. **Stars:** `SELECT *`, `SELECT t.*`, BigQuery `SELECT * EXCEPT(...)` / `REPLACE(...)`
6. **Expressions:** `CASE`, `COALESCE`, arithmetic across columns, nested function calls
7. **Aggregations & windows:** `GROUP BY 1,2` (positional), `HAVING`, `OVER (PARTITION BY ...)`,
   Snowflake `QUALIFY`
8. **Subqueries:** scalar, correlated, `IN`/`EXISTS`, lateral / `CROSS JOIN UNNEST`
9. **Semi-structured:** BigQuery `STRUCT`/`ARRAY` field access, Snowflake `col:field::type`,
   `PIVOT`/`UNPIVOT`
10. **Identifiers:** quoted, case-sensitive, reserved words, Unicode
11. **dbt-specific:** `ref`/`source`/`this`, incremental `is_incremental()` branches, snapshots,
    ephemeral models, `dbt_utils.star()`, custom macros, `{% for %}` generated columns
12. **Adversarial:** deliberately unparseable SQL, deeply nested queries, 500-column projections

### Layer 3 — The DuckDB execution oracle *(the differentiator)*

A harness that, for each scenario:
1. Materializes a real dbt project on DuckDB and runs `dbt build` → must pass
2. Applies a scripted change (drop a column, rename a model, …)
3. Runs `dbt build` again and records **what actually broke**
4. Runs whatbreaks on the two commits and compares its prediction to reality

This yields real precision/recall numbers against ground truth, tracked over time and published in
the README. DuckDB makes it free and fast enough to run in CI on every commit.

**No other tool in this category has a ground-truth oracle. This is the single strongest technical
statement the project can make.**

### Layer 4 — The false-positive corpus *(a release gate)*

PRs that must produce **exactly zero** findings:
- Reformatting / whitespace / comment-only changes
- Reordering columns in a `SELECT`
- Renaming a CTE
- Adding a new independent model
- Adding a column at the end of a projection
- Changing a model's description in YAML

**Any regression here blocks release.** FP rate determines retention more than feature count does.

### Layer 5 — Property-based invariant

For any edge whatbreaks marks `CONFIRMED`: removing that upstream column from the schema **must**
cause `sqlglot.optimizer.qualify` to fail on the downstream model. This is a mechanical oracle for
the confidence claim itself, and it can be run over the entire corpus via Hypothesis-driven schema
mutation.

### Layer 6 — Real-world corpus
Vendored manifests from public dbt projects (jaffle_shop, dbt-labs integration projects, and the
largest public dbt repos findable). Assert coverage does not regress. Publish the coverage number.

### Layer 7 — End-to-end / Action tests
`act` or a live test repo exercising the two-workflow pattern, including a fork-PR simulation.

---

## 15. Repository structure

```
whatbreaks/
├── src/whatbreaks/
│   ├── cli.py                  # click entrypoint, exit-code derivation
│   ├── config.py               # .whatbreaks.toml / pyproject.toml loading, suppressions
│   ├── manifest/
│   │   ├── loader.py           # version detection, validation, untrusted-input hardening
│   │   └── models.py           # dataclasses over the manifest subset we consume
│   ├── sql/
│   │   ├── recovery.py         # Decision 1: compiled → on-disk → sandboxed Jinja
│   │   ├── jinja_stub.py       # the stubbed dbt context
│   │   └── dialect.py          # adapter_type → sqlglot dialect mapping
│   ├── lineage/
│   │   ├── schema_inference.py # Decision 2 — the heart. Topological + unknown propagation
│   │   ├── column_graph.py     # sqlglot.lineage → ColumnEdge
│   │   └── uncertainty.py      # confidence algebra and propagation rules
│   ├── diff/
│   │   ├── graph_diff.py       # Decision 3
│   │   └── classify.py         # WB001..WB010 rules, one function per rule
│   ├── impact/
│   │   └── blast_radius.py     # reverse reachability + tests/exposures
│   ├── report/
│   │   ├── terminal.py  ├── json.py  ├── markdown.py
│   │   └── schema/output-v1.json
│   ├── cache.py                # content-addressed
│   └── graph.py                # toposort + reverse reachability (no networkx)
├── tests/
│   ├── unit/  ├── corpus/  ├── fixtures/dbt_projects/  ├── oracle/  ├── false_positives/
│   ├── real_world/manifests/   └── e2e/
├── action/action.yml           # composite action
├── .github/workflows/
│   ├── ci.yml  ├── oracle.yml  ├── release.yml
│   └── templates/              # the two shipped user-facing workflow templates
├── docs/
│   ├── index.md  ├── getting-started.md  ├── rules/WB001.md …
│   ├── limitations.md          # ← a first-class, prominent document
│   ├── security.md  └── adr/
├── examples/
├── pyproject.toml  ├── uv.lock  ├── CONTRIBUTING.md  ├── SECURITY.md  └── CHANGELOG.md
```

**Component responsibilities worth calling out:**

- **`lineage/uncertainty.py`** — the confidence algebra lives in exactly one place. Every rule that
  needs to downgrade a confidence calls into it. Scattering this logic is how "never overclaim"
  quietly dies.
- **`docs/limitations.md`** — linked from the README's first screen. Publishing your limitations
  prominently is a trust move, and in a field where every competitor implies completeness, it is also
  a positioning move.
- **`docs/rules/WBxxx.md`** — one page per rule (like `ruff`), with a triggering example, why it
  matters, and how to suppress. This is table stakes for a tool that wants to be trusted in CI.

---

## 16. Development workflow

- **Tooling:** `uv` for env + locking; `ruff` for lint+format; `mypy --strict`; `pytest` +
  `pytest-cov`; `hypothesis` for the property layer.
- **Pre-commit:** ruff, mypy, and a fast subset of the corpus.
- **CI matrix:** Python 3.10–3.13 × dbt manifest schema v10/v11/v12.
- **Separate `oracle.yml` workflow** — the DuckDB oracle runs on every PR but as a distinct,
  slower job with its own status badge showing current precision/recall.
- **Branching:** trunk-based; short-lived branches; every PR must include either a corpus fixture or
  a stated reason why not.
- **ADRs:** every Decision in §9 becomes `docs/adr/NNN-*.md`. Future contributors need the reasoning,
  and this is exactly the artifact that signals seniority to a reviewer.

---

## 17. MVP definition

**The MVP is one sentence:** *given two dbt manifests, tell me which downstream columns break if I
remove a column or a model — and tell me honestly what you couldn't analyze.*

**In:** F1–F10. Rules `WB001`, `WB002`, `WB003`, `WB900`. Terminal + JSON + Markdown output. Coverage
reporting. Exit codes.

**Out of MVP:** GitHub Action, caching, rename inference, expression changes, type changes,
config-level rules, `catalog.json` tier, suppressions, docs site.

**MVP acceptance:** on a 200-model reference project, correctly identifies every downstream column
affected by a column removal, with zero false positives on the no-op corpus, in under 20 seconds,
with no network access and no warehouse credentials.

---

## 18. Detailed implementation phases

### Phase 0 — Feasibility spike *(3–5 days) · DO NOT SKIP*

**This phase exists to potentially kill the project cheaply.** The entire zero-warehouse thesis
depends on assumptions that have not been measured.

**Objective:** determine empirically whether real dbt projects are analyzable without a catalog.

**Work:**
1. Collect 8–12 public dbt projects (jaffle_shop, dbt-labs integration projects, and the largest
   public dbt repos findable on GitHub).
2. Run `dbt parse` on each; measure:
   - **% of models whose `raw_code` renders successfully** with a naive stub Jinja context
   - **% of models containing `SELECT *` or `t.*`**
   - **% of models whose parents have YAML-declared columns** (schema-inference seed quality)
   - **% of models that `sqlglot.parse_one` handles** per dialect
3. Prototype the topological schema-inference loop end-to-end on the largest project. Measure final
   **EXACT / PARTIAL / UNKNOWN** distribution.

**Acceptance:** ≥70% of models reach `EXACT` schema resolution using Tier-1 (offline) inputs only.

**Decision gate:**
- **≥70%** → proceed as planned.
- **50–70%** → proceed, but reposition: `catalog.json` becomes *strongly recommended* rather than
  optional, and the README leads with degraded-mode honesty.
- **<50%** → **the wedge is wrong.** Stop and revise. The likely pivot is to accept `catalog.json` as
  required and differentiate purely on the fork-safe CI ergonomics and the uncertainty model.

**Risk this addresses:** building the entire tool on an unvalidated premise. This is the single
highest-value five days in the plan.

**Deliverable:** `docs/adr/000-feasibility.md` with the measured numbers. Publishable as a blog post
regardless of outcome — "I measured `SELECT *` prevalence across N public dbt projects" is a
genuinely useful artifact to the community and costs nothing extra.

---

### Phase 1 — Lineage core *(2–3 weeks)*

**Objective:** a correct, honest column graph for a single commit.

**Deliverables:** `manifest/`, `sql/recovery.py`, `sql/jinja_stub.py`, `lineage/schema_inference.py`,
`lineage/column_graph.py`, `lineage/uncertainty.py`, `graph.py`. A hidden
`whatbreaks debug graph` command dumping the graph as JSON.

**Tests:** corpus layers 1–2 with ≥60 fixtures covering categories 1–8. The property-based invariant
(layer 5) lands here — it validates the confidence algebra at the moment the algebra is written.

**Acceptance:**
- All SUPPORTED fixtures produce exactly-correct lineage
- All DEGRADED fixtures produce correct lineage **at reduced confidence** (asserted)
- All UNSUPPORTED fixtures fail cleanly with a diagnostic, never a wrong answer
- Zero crashes on the real-world corpus

**Risks:** sqlglot dialect gaps (→ pin the version, upstream fixes, document); Jinja stub coverage
lower than Phase 0 suggested (→ widen the compiled-SQL fast path).

**Deferred:** everything about diffs.

---

### Phase 2 — Diff and classification *(2 weeks)*

**Objective:** turn two graphs into findings.

**Deliverables:** `diff/graph_diff.py`, `diff/classify.py` (WB001/002/003/900),
`impact/blast_radius.py`, base-manifest acquisition via `git worktree`.

**Tests:** false-positive corpus (layer 4) — the release gate. Diff fixtures as
`(base_project, head_project, expected_findings)` triples.

**Acceptance:** zero findings on every no-op fixture; correct blast radius on every breaking fixture;
tests and exposures correctly included.

**Risks:** false positives from formatting changes (mitigated by design — graph diffing, not text
diffing — but must be *proven*, not assumed).

---

### Phase 3 — CLI, reporting, and the uncertainty surface *(1.5 weeks)*

**Deliverables:** `cli.py`, all three reporters, `output-v1.json` schema, coverage reporting,
`--fail-on`, exit codes, `whatbreaks coverage --explain`.

**Acceptance:** the anti-silence invariant test passes. Output is byte-deterministic. A user can read
the terminal output and know exactly which columns break, where, and what was not analyzed.

**Milestone: v0.1.0 → PyPI.** Usable, honest, no Action yet.

---

### Phase 4 — DuckDB execution oracle *(2 weeks)*

**Objective:** ground truth. This is the phase that makes the project exceptional; it is placed
before the Action deliberately, consistent with the correctness-first priority.

**Deliverables:** `tests/oracle/` harness, ≥25 scenarios, precision/recall computation, a CI job with
a badge, and the numbers published in the README.

**Acceptance:** precision ≥95% and recall ≥90% on the oracle suite for `WB001`/`WB002`. Any
prediction miss is either fixed or converted into a documented limitation.

**Risks:** DuckDB dialect divergence from Snowflake/BigQuery means the oracle validates the
*algorithm* rather than every dialect. Document this honestly rather than overstating what the oracle
proves.

---

### Phase 5 — GitHub Action *(1.5 weeks)*

**Deliverables:** composite `action/action.yml`; both workflow templates; markdown comment renderer
with injection hardening; idempotent comment update; `docs/security.md` explaining the
`pull_request_target` vulnerability class.

**Acceptance:** works on a same-repo PR and on a fork PR; comment updates rather than duplicates;
no secrets required; injection tests pass.

**Milestone: v0.2.0.**

---

### Phase 6 — Rule expansion and ergonomics *(2 weeks)*

**Deliverables:** WB004–WB008; rule IDs; inline + config suppressions; `.whatbreaks.toml`;
content-addressed cache; `docs/rules/` pages.

**Acceptance:** performance NFRs met with a warm cache; every rule has a doc page and a fixture.

**Milestone: v0.3.0.**

---

### Phase 7 — Catalog tier and v1.0 hardening *(2 weeks)*

**Deliverables:** optional `catalog.json` ingestion; `SELECT *` expansion; WB009/WB010; frozen
output schema v1; docs site; published coverage + oracle benchmarks; `docs/limitations.md`.

**Milestone: v1.0.0.**

---

## 19. Milestones and acceptance criteria

| Milestone | Gate |
| --- | --- |
| **Phase 0 gate** | ≥70% EXACT schema resolution on public projects, else revise the wedge |
| **v0.1.0** | Correct lineage + WB001/002/003; zero FPs on no-op corpus; <20s on 500 models; no network |
| **v0.2.0** | Fork PR produces a comment; no secrets; injection-hardened |
| **v0.3.0** | WB004–008; suppressions; warm cache <5s |
| **v1.0.0** | Oracle precision ≥95% / recall ≥90%; output schema frozen; every rule documented; limitations published; ≥120 corpus fixtures |

---

## 20. Risks and mitigations

| # | Risk | Severity | Mitigation |
| --- | --- | --- | --- |
| R1 | **`SELECT *` prevalence makes offline analysis useless** | **Critical** | Phase 0 measures it before any product code. Explicit decision gate with a defined pivot |
| R2 | Jinja stub cannot render macro-heavy real projects | High | Compiled-SQL fast path; honest `WB900`; measured in Phase 0 |
| R3 | False positives cause uninstalls | High | FP corpus is a release gate; graph-diff (not text-diff) by design; `--fail-on=breaking` default |
| R4 | sqlglot lineage bugs / dialect gaps | Medium | Pin sqlglot; corpus tests catch regressions on upgrade; upstream contributions |
| R5 | dbt manifest schema churn | Medium | Support a declared version range; hard-error on unknown; CI matrix across v10/v11/v12 |
| R6 | Recce or SQLMesh absorbs the niche | Medium | Stay narrow. The zero-dependency, fork-safe position is architecturally awkward for both of them to adopt |
| R7 | Solo maintenance burden | Medium | ≤4 deps; no UI; no server; no BI connectors in v1; composite action avoids a JS toolchain |
| R8 | Users expect a lineage viewer and are disappointed | Low | Positioning: "linter, not catalog," stated in the first line of the README |
| R9 | The oracle validates only DuckDB semantics | Low | Document precisely what it proves; add parse-only dialect fixtures for Snowflake/BigQuery |
| R10 | **Manifest macro compilation is load-bearing but incomplete** — Phase 0 proved compiling `manifest.macros` is what makes offline analysis viable (renderability 34.1% → 80.5%), but it also exposed how deep dbt's Jinja context is: `api`, `load_result`, `graph`, `adapter.*`, `dispatch`, materialization/test blocks. "We can just load the macros" is more optimistic than the truth; the residual tail is long and dbt-version-sensitive | **High** | Treat macro compilation as a first-class stage with its own failure taxonomy, not a helper. Budget explicit time in Phase 1. Never render an unresolvable macro to `""` — fail loudly per ADR 000. Pin a supported dbt range and test the macro registry across it. Accept a permanent residual and report it as coverage |
| R12 | **Unit tests share the author's blind spots.** Every significant bug through Phases 1–2 was a *false negative* — a real break reported as safe — and not one was caught by the unit tests. The missing `return()` builtin, predicate-only dependencies, top-level-only star detection, head-graph blast radius, and CTE-qualified references were all found by running the tool on real dbt projects or by the mechanical invariant oracle. Tests written by the person who wrote the code encode the same wrong mental model | **High** | Treat real-project validation and mechanical oracles as primary verification, not supplementary. `tools/validate_*.py` must be run on any change to recovery, inference or the graph. Prefer properties an independent judge can check (`validate_qualify_columns`, real `dbt build`) over assertions restating the implementation. Every all-negative test suite needs an explicit non-vacuity test — both the invariant oracle and the false-positive gate now have one |
| R11 | **Phase 0 schedule overrun** — budgeted 3–5 days, took substantially longer. Cause was not analysis but *acquisition*: 7 of 14 public dbt projects would not produce a manifest in a clean room (adapter mismatch, dbt version drift, missing vars, absent source tables, unreachable package hub) | Medium | Recorded as a planning correction: for any future measurement phase, budget acquisition separately from analysis and assume ~50% sample attrition. Also a product requirement — whatbreaks must diagnose "could not obtain a usable manifest" as its own distinct, well-explained failure mode, surfacing dbt's own error rather than reporting "analysis failed" |

---

## 21. Performance strategy

**Expected scale:** typical dbt projects are 50–800 models; large ones reach ~3,000. sqlglot parses a
typical model in 1–5 ms, so raw parsing of 1,000 models is ~1–5 s. **The bottleneck is
`qualify` with star expansion during schema inference, not parsing.**

**Caching:** content-addressed, keyed on `hash(recovered_sql + dialect + schema_of_parents +
whatbreaks_version)`. Cached artifact is the model's resolved schema and its column edges. Stored in
`.whatbreaks_cache/`, restorable via `actions/cache`.

**Incrementality:** the head graph only needs recomputation for changed models *and their transitive
descendants* — because a parent's schema change invalidates children. The base graph is cached whole,
keyed on the merge-base SHA, and is usually a full cache hit.

**Deliberately not optimized now:** parallelism. Schema inference is inherently sequential in
topological order; parallelising within a topological *level* is possible but is a v1.x concern.
Architecturally preserved by keeping every stage a pure function.

**Architectural choices that protect future scaling:**
- Pure-function stages → trivially parallelisable or distributable later
- Content-addressed caching from the start → cache keys never need redesigning
- No global mutable state → no refactor needed to introduce concurrency
- **The one thing that would hurt later:** materialising the full column graph in memory. At ~3,000
  models × ~40 columns × edges this is fine (tens of MB); beyond that it would need streaming. Accept
  this limit explicitly and document it rather than pre-building for a scale that will not arrive.

---

## 22. Release strategy

- **SemVer, with a linter-specific policy:**
  - New rule added → **MINOR** (and new rules default to *warn*, not fail, for one minor cycle)
  - Existing rule's severity increased → **MAJOR** (it can newly break someone's CI)
  - Lineage accuracy improvement that surfaces new findings → **MINOR**, but called out prominently
    in the changelog
- **Output schema versioned independently** (`schema_version` in JSON). Consumers pin to it.
- **v0.x means unstable**, stated plainly. v1.0 is the stability commitment, not a marketing event.
- **Trusted Publishing to PyPI** via OIDC — no long-lived API token in repo secrets.
- **Action releases** tagged `v1` (moving) and `v1.2.3` (immutable); docs always show SHA pinning.
- Changelog is `CHANGELOG.md`, human-written, `Keep a Changelog` format.

---

## 23. Open-source / adoption strategy

Given the "technical depth first" priority, adoption effort is *deferred*, not skipped — but the
following are cheap and should be built in from the start rather than retrofitted.

**Repository description:** *"Static breaking-change analysis for dbt. Column-level blast radius in
CI — no warehouse, no secrets, no backend."*

**README structure** (order matters — the first screen is the whole pitch):
1. One sentence + the terminal output GIF (the output *is* the demo)
2. "Why another one?" — an honest comparison table including Recce, SQLMesh, Datafold. Naming
   competitors accurately is a credibility signal, not a weakness
3. Install + 3-line quickstart
4. **Limitations** — linked prominently on the first screen
5. The oracle badge with live precision/recall numbers
6. Rules index

**Documentation:** MkDocs Material on GitHub Pages. One page per rule. A first-class
`limitations.md`. A `security.md` explaining the fork-safe workflow pattern and why the naive one is
dangerous.

**Contribution strategy:** the corpus is the ideal contribution surface — "found SQL we get wrong?
open a PR with a fixture" is a low-barrier, high-value ask that directly improves the product.
Issue templates: `bug`, `wrong-lineage` (with a required minimal SQL repro), `unsupported-sql`,
`false-positive`.

**Distribution:** PyPI (primary), GitHub Actions Marketplace (secondary), `pre-commit` hook (v0.3+).

**One credibility artifact worth writing:** the Phase 0 measurement post — *"How much of a real dbt
project can you analyze without a warehouse? I measured N public projects."* It is useful to the
community independent of the tool, it establishes the problem, and it costs nothing beyond work
already done.

---

## 24. Future roadmap (post-v1.0, not before)

Ranked by how much each would transform whatbreaks from a useful utility into a differentiated
product:

1. **Cross-boundary blast radius — into the BI layer.** Parse LookML, Metabase, and Superset metadata
   so the blast radius extends past dbt's edge into actual dashboards. This is where breakage
   actually hurts, and today only paid tools (Datafold, Atlan, Sifflet) go there. **The single
   highest-value future direction**, deliberately deferred because each integration is a maintenance
   commitment against a moving target.
2. **Warehouse query-log ingestion (opt-in, offline).** Feed in exported query history to learn which
   columns are *actually consumed* by ad-hoc users and BI, not just by dbt models. Turns "this column
   has 3 downstream models" into "this column has 3 models and 47 human queries last month." That is
   a qualitatively different answer and it stays true to the no-live-connection principle.
3. **Editor / LSP integration.** Show blast radius inline as you type, before the PR exists. Moves
   the tool from gate to feedback loop — the same trajectory that made type checkers ubiquitous.
4. **SQLMesh and generic-SQL front ends.** The core engine is framework-agnostic; only the metadata
   loader is dbt-specific. Extending the front end multiplies the addressable base without touching
   the hard part.
5. **A machine-readable "column contract" artifact.** Emit each model's column contract as a
   committed, reviewable file — making schema changes visible in the diff itself, the way lockfiles
   made dependency changes visible. Potentially the most durable idea here, and the most likely to
   outlive the tool.

---

## 25. Explicitly out of scope

**Permanently:**
- Any web UI, server, daemon, or hosted service
- Any database or persistent metadata store
- Data value comparison / data diffing (Recce and Datafold own this)
- Executing SQL against a warehouse
- LLM-based inference of any kind
- A general-purpose data catalog or search
- Runtime/observability lineage (OpenLineage's domain)
- Cost estimation, query optimization, style linting (SQLFluff's domain)

**Deferred past v1.0:**
- BI-layer traversal · Python models · SQLMesh and raw-SQL front ends · cross-repo lineage ·
  GitLab/Bitbucket CI · IDE integration · auto-fix suggestions

**Rejected outright:**
- Regex-based Jinja stripping (silently wrong)
- `pull_request_target` with PR checkout (security vulnerability)
- Failing CI on `UNKNOWN` findings by default (guarantees uninstallation)

---

## 26. Final recommendation

**Build it — but run Phase 0 first and be genuinely willing to act on the result.**

The concept survives critical evaluation, but only in a much narrower form than originally framed.
Three corrections to the starting premise are load-bearing:

1. **"Column-level lineage" is not the product.** It is a commoditized input available from
   `sqlglot`. The product is *honest breaking-change classification in CI with zero infrastructure*.
2. **"No warehouse needed" is a real differentiator with a real cost.** Without a catalog, `SELECT *`
   is unexpandable. The plan converts this from a hidden weakness into the explicit uncertainty
   model — but only Phase 0 can confirm the cost is survivable.
3. **Rigor is the only defensible moat.** Against Recce's funding and Datafold's head start, the
   winnable ground is being the tool that is *provably* right about what it claims and *explicitly*
   honest about what it cannot know. The DuckDB oracle and the confidence-asserting corpus are not
   testing infrastructure — they are the product's core claim, made checkable.

The realistic outcome is a well-regarded niche tool and an exceptional technical calling card. Both
are worth the roughly 12–14 weeks of solo work described here.

---

## 27. The Hardest Parts of whatbreaks

**1. Schema inference with honest unknown propagation.**
Everything else depends on it. The difficulty is not computing schemas — it is computing *partial*
schemas and correctly propagating degraded certainty through a DAG without either over-poisoning
(one unknown source marks the whole project UNKNOWN, making the tool useless) or under-poisoning
(uncertainty gets silently dropped, making the tool dishonest). There is no library for this; the
confidence algebra must be designed, and it is the one piece where a subtle bug produces confidently
wrong output rather than a crash.

**2. Deciding what *not* to claim.**
The engineering instinct is to squeeze out one more resolved edge. The product requirement is the
opposite: every ambiguous case must be *detected as ambiguous*. That means enumerating the ways
lineage can be ambiguous — unqualified columns across joins, `UNION` position-matching, `SELECT *`
over unknown parents, dynamic column generation in Jinja loops — and building explicit detection for
each. Detecting your own ignorance is strictly harder than computing an answer, and it is the thing
that separates this from a weekend sqlglot wrapper.

**3. Jinja without dbt.**
`raw_code` is not SQL; it is a template in a language whose macros can perform arbitrary computation,
including introspective warehouse queries. Rendering it offline means reimplementing enough of dbt's
context to be useful while knowing you cannot reimplement all of it. The hard part is failing
*loudly and precisely* — "macro `dbt_utils.star()` requires a catalog" is useful; "parse error" is
not. Getting the taxonomy of rendering failures right is most of the work.

**4. False positives.**
A tool that cries wolf is deleted in week two, so the FP budget is effectively zero. This is hard
because it fights every other goal: graph-diffing rather than text-diffing is the right call but
means a lineage bug anywhere can manifest as a phantom finding somewhere unrelated. Precision here
depends on the correctness of stages 1–3, which is why the FP corpus is a release gate rather than a
test suite.

**5. Establishing ground truth at all.**
Every lineage tool in this space asserts correctness against its own expectations — a closed loop.
The DuckDB oracle breaks that loop by comparing predictions to what actually breaks when dbt runs.
Building it is hard for unglamorous reasons: scripting realistic breaking changes, distinguishing
"dbt errored" from "dbt succeeded but produced wrong data," and keeping the harness fast enough for
every-commit CI. It is also the highest-leverage work in the plan.

---

## 28. What Would Make whatbreaks Exceptional?

Four capabilities, in order of impact. None is a feature in the ordinary sense; each is a claim about
the tool's epistemics.

**1. A published, mechanically-measured accuracy number.**
Precision and recall against real dbt execution, as a CI badge, regenerated every commit. Every
competitor asserts correctness; none *measures* it. A README that says "94.2% precision, 91.7% recall
across 25 real breaking-change scenarios, methodology here" is a categorically different artifact
from one that says "accurate column-level lineage." This alone would make the project notable.

**2. A corpus that tests confidence, not just answers.**
Fixtures that assert *"this SQL must yield LIKELY, not CONFIRMED."* This makes intellectual honesty a
mechanically enforced property. It is unusual enough that any senior reviewer encountering it
immediately understands the author is operating at a different level from typical tooling work.

**3. Being the only tool that works where the others structurally cannot.**
Fork PRs. Air-gapped CI. Repos where the platform team refuses to put warehouse credentials in a
pull-request workflow. This is not a marketing claim — it is a consequence of the architecture, and
it cannot be matched by any tool built on a hosted catalog or a live connection without those tools
abandoning their own designs.

**4. Extending the blast radius past dbt's edge (v1.x).**
Every OSS tool stops where the dbt DAG stops. Real breakage happens in the Looker dashboard three
hops downstream. Crossing that boundary in an OSS tool — even for just Metabase and LookML — enters
territory currently held only by commercial products, and turns whatbreaks from "a good dbt linter"
into "the only free tool that tells you the truth about your blast radius."

---

## 29. Recommended execution order for a solo developer

| # | Step | Time | Why here |
| --- | --- | --- | --- |
| 1 | **Phase 0 spike.** Measure `SELECT *` prevalence, Jinja renderability, seed-schema quality across 8–12 public dbt projects. Write `adr/000-feasibility.md` | 3–5 d | Cheapest possible falsification of the core premise. **Honor the decision gate** |
| 2 | Repo scaffold: `src/` layout, `uv`, ruff, mypy strict, pytest, CI matrix. Empty but complete | 1 d | Never retrofit tooling. Do it while the repo is empty |
| 3 | Manifest loader + dataclasses + version detection + hardening | 3 d | Everything reads from here |
| 4 | SQL recovery (compiled → on-disk → sandboxed Jinja) with a precise failure taxonomy | 4 d | The riskiest component after schema inference; de-risk early |
| 5 | **Schema inference + the confidence algebra**, with corpus categories 1–8 written *alongside* | 7–9 d | The heart. Write fixtures as you go, not after |
| 6 | Column graph via `sqlglot.lineage` + the property-based invariant test | 4 d | The invariant validates the algebra at the moment it exists |
| 7 | `whatbreaks debug graph` (hidden command) | 1 d | You cannot debug what you cannot see. Pays for itself immediately |
| 8 | Graph diff + WB001/002/003 + blast radius | 6 d | The actual product logic — small, because the hard work is already done |
| 9 | **False-positive corpus.** Make it a release gate now, before habits form | 2 d | Retrofitting an FP budget never works |
| 10 | CLI, three reporters, coverage reporting, exit codes, anti-silence test | 5 d | **→ ship v0.1.0 to PyPI** |
| 11 | **DuckDB execution oracle**, ≥25 scenarios, published precision/recall | 8–10 d | The differentiator. Deliberately before the Action, per the correctness-first priority |
| 12 | Composite GitHub Action + both workflow templates + injection hardening + `security.md` | 6 d | **→ v0.2.0.** Now the tool is distributable |
| 13 | WB004–008, rule IDs, suppressions, caching, `docs/rules/` | 8 d | **→ v0.3.0** |
| 14 | Optional `catalog.json` tier, `SELECT *` expansion, WB009/010 | 6 d | Accuracy upgrade for those who can supply it |
| 15 | Docs site, `limitations.md`, freeze output schema v1, publish benchmarks | 4 d | **→ v1.0.0** |
| 16 | Write the Phase 0 measurement post | 1 d | Distribution, from work already done |

**≈ 12–14 weeks of focused solo work to a credible v1.0.**

Two rules to hold throughout:
- **Step 1's gate is real.** If coverage comes in under 50%, revising the wedge is the correct
  outcome, not a failure. Discovering that in week one costs five days; discovering it in week ten
  costs the project.
- **Never let a fixture be written after the code it tests.** In this project the corpus *is* the
  specification, and the confidence assertions in it are what make the central claim checkable.

---

## Verification plan

How to confirm each phase actually works, end to end:

- **Phase 0:** `adr/000-feasibility.md` contains measured percentages per project, with the raw
  script committed under `tools/`. Reproducible by a third party.
- **Phases 1–2:** `pytest tests/corpus tests/false_positives` green. Every DEGRADED fixture asserts
  reduced confidence; every no-op fixture asserts zero findings.
- **Phase 3:** on a scratch dbt project, drop a column from a mid-DAG model, run
  `whatbreaks check --base HEAD~1`, and confirm the reported downstream columns match a manually
  traced list. Confirm exit code 1. Confirm `--format json` validates against `output-v1.json`.
  Confirm a socket-blocking test proves zero network access.
- **Phase 4:** `pytest tests/oracle` prints precision/recall; every miss is triaged into either a
  fix or a documented limitation in `limitations.md`.
- **Phase 5:** open a PR from a fork of the test repo and confirm a comment appears without any
  secret configured; re-push and confirm the comment updates rather than duplicates; submit a model
  named with markdown/HTML and confirm it renders escaped.
- **Phases 6–7:** re-run the full suite plus a timing assertion on a 500-model generated project
  (cold <20s, warm <5s).
