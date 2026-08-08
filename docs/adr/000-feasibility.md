# ADR 000 — Phase 0 feasibility: can a dbt project be analysed at column level without a warehouse?

- **Status:** **Accepted — gate PASSED, proceed to Phase 1**
- **Date:** 2026-08-08
- **Decides:** whether whatbreaks' zero-warehouse wedge is viable, per the Phase 0 gate in
  `WHATBREAKS_PROJECT_PLAN.md` §18.

---

## 1. The question

whatbreaks' entire differentiation rests on one unproven claim:

> A dbt project can be analysed at the column level using only offline artifacts
> (`dbt parse` output + files on disk), with no warehouse connection and no `catalog.json`.

If that claim is false, the wedge is wrong. Every competitor in this space
(Recce, dbt-column-lineage, Datafold, Atlan, Sifflet, Metaplane) requires either a live warehouse or
a hosted catalog. They may require it *because it is actually required*. Phase 0 exists to find out
before any product code is written.

## 2. Gate (defined in advance, not after seeing results)

Metric: **% of models reaching `EXACT` schema resolution** using offline inputs only.

| Result | Decision |
| --- | --- |
| **≥ 70%** | Proceed as designed. `catalog.json` stays optional. |
| **50–70%** | Proceed, but reposition: `catalog.json` becomes *strongly recommended*; README leads with degraded-mode honesty. |
| **< 50%** | **The wedge is wrong.** Stop and revise — likely making `catalog.json` required and differentiating purely on fork-safe CI ergonomics + the uncertainty model. |

## 3. Method

**Sample.** 11 public dbt projects, chosen for variety rather than convenience — canonical
(jaffle_shop ×2), large real-world analytics (tuva_core, 1,182 models), production packages
(fivetran_shopify, fivetran_netsuite, velir_ga4, dbt_artifacts, snowflake_monitoring), and
deliberately macro-hostile codebases (elementary, dbt_expectations) chosen to stress the Jinja path.

**Per project:** vendor packages → `dbt parse` → load `manifest.json` → for every model:

1. **M1 `compiled_code` availability** — does `dbt parse` hand us SQL for free?
2. **M2 Jinja renderability** — does `raw_code` render against a stubbed dbt context?
3. **M3 `SELECT *` prevalence** — split into *any* star vs star *surviving* qualification.
4. **M4 seed-schema quality** — do sources/seeds/models declare columns offline?
5. **M5 schema resolution** — topological inference over the model DAG → EXACT / PARTIAL / UNKNOWN.

**Scripts:** `tools/phase0_probe.py`, `tools/vendor_packages.py`, `tools/list_targets.py`.
Raw output: `.phase0/out/raw.json`, `.phase0/out/summary.json`. Reproducible by a third party.

**Stub philosophy.** The Jinja stub deliberately does **not** fake package macros (`dbt_utils.*`).
Unknown names raise a *named* failure rather than rendering to empty string, because silently
rendering an unknown macro to `""` produces syntactically valid but semantically wrong SQL — the
exact failure mode whatbreaks exists to prevent. Failure reasons are counted, not swallowed.

---

## 4. Findings so far

### F1 — `dbt parse` yields **no** compiled SQL (confirmed, 0% across sample)

`compiled_code` is null for every model after `dbt parse`. This confirms the documented behaviour
and settles a core architecture question: **offline Jinja rendering is mandatory, not optional.**
The "read `target/compiled/`" fast path in the plan (Decision 1) only helps users who already run
`dbt compile` against a warehouse — it cannot be the primary path.

### F2 — Star-over-CTE is resolvable; only *surviving* stars matter (design-changing)

Initial measurement showed 0% EXACT and 100% top-level `SELECT *` on jaffle_shop, which looked
fatal. It was a bug in the probe, and the diagnosis changed the product design.

The dominant dbt idiom is:

```sql
with source as (select * from {{ ref('raw') }}),
     renamed as (select id as customer_id, first_name from source)
select * from renamed          -- top-level SELECT *
```

Nearly every model ends in `select * from final`. But that star is over a **CTE**, and sqlglot's
`qualify(..., infer_schema=True)` expands it correctly **even with a completely empty schema**,
because the CTE's projection is explicit. Verified directly: outputs resolved to
`['customer_id', 'first_name', 'last_name']` with `schema={}`.

**Consequence for the product:** raw `SELECT *` prevalence is a *misleading* metric and must not be
used as the uncertainty signal. The correct signal is **"did an unresolved star survive
qualification?"** This is now the basis of the EXACT/PARTIAL/UNKNOWN classification, and it is far
more favourable to the zero-warehouse thesis than raw star counts suggest.

After this fix, jaffle_shop_duckdb resolved **100% EXACT** with no warehouse and no catalog.

### F3 — Seeds are free schema (offline)

A dbt seed's columns are its CSV header, sitting on disk. Seeds were initially not registered as
known-schema parents, which incorrectly degraded every downstream model. Seeds (and any node with
YAML-declared columns) should seed the inference graph. This costs nothing and requires no warehouse.

### F4 — Environment: Avast TLS interception breaks all Python HTTPS on this machine

`dbt deps` cannot reach `hub.getdbt.com`. Root cause is not dbt: **Avast Web/Mail Shield** performs
TLS interception with a CA certificate whose Basic Constraints extension is not marked critical,
which OpenSSL 3.x rejects outright (`CERTIFICATE_VERIFY_FAILED ... Basic Constraints of CA cert not
marked critical`). Exporting the Windows cert stores to a PEM and setting `SSL_CERT_FILE` /
`REQUESTS_CA_BUNDLE` does **not** help, because the offending certificate is the interceptor's own
root and is required for the chain.

Git is unaffected (Windows schannel), so packages are resolved by `git clone` into `dbt_packages/`
(`tools/vendor_packages.py`). This is a local environment issue with no bearing on the product, but
it is recorded because it will affect any Python HTTPS work on this machine, and it shaped the
methodology.

### F5 — Methodological trap: vendored packages masquerading as projects

After vendoring, `dbt_packages/dbt_utils/integration_tests` contained more models than several real
projects, and naive "largest models/ directory" target discovery selected the *dependency* instead
of the project under test. Caught before the results run. Target discovery now excludes
`dbt_packages/` and prefers shallower project roots.

Recorded because it is the kind of error that silently produces plausible-but-meaningless numbers —
and because the equivalent mistake in the product (analysing vendored package models as if they were
first-party) would produce exactly the kind of false positive that gets a linter uninstalled.

---

### F6 — Package macros do not need faking: the manifest carries their source (decisive)

`manifest.json` contains `macro_sql` — the full source of **every** macro, first-party *and*
package. Package macros can therefore be compiled and executed offline rather than stubbed.

Two bugs had to be fixed before this worked, and both are instructive for the product:

1. **`macro_sql` is not all macros.** It also carries `{% materialization %}`, `{% test %}` and
   `{% snapshot %}` blocks — dbt Jinja extensions plain Jinja2 cannot parse. One such block fails
   the whole package compile. Filter to `{% macro %}` blocks only.
2. **Macros must be compiled twice.** Compiling against a context that does not yet contain the
   other packages' namespaces means a macro calling `dbt_utils.y()` sees `dbt_utils` as undefined —
   *from inside the macro body*, even though models could see it. A second pass, compiling against
   a context that already contains pass 1's output, fixes it.

Measured effect, whole sample:

| | naive stub only | + compiled manifest macros |
| --- | ---: | ---: |
| models rendered | **34.1%** | **80.5%** |

Per project, the swing is larger still: `dbt-project-evaluator` 0% → 91.7% rendered,
`dbt_expectations` 16.7% → 83.3%, `dbt_artifacts` 38.2% → 100%.

**This changes Decision 1 in the plan.** Compiling manifest macros is not an optimisation; it is
the difference between a tool that works and one that does not. It must be a first-class stage of
SQL recovery.

### F7 — Severe, non-random sample attrition

7 of 14 projects never produced a manifest (see F5 and §7). Crucially the losses were **not
random**: `tuva_core` (1,182 models), `tuva_input` and `jaffle_shop_modern` were three of the five
*analytics* projects. The analytics subset therefore collapsed to 2 projects / 15 models, which is
the single biggest weakness in this measurement.

---

## 5. Results

dbt-core 1.11.12 · sqlglot 30.15.0 · 14 projects attempted, **7 produced a manifest**, 164 models
analysed. Raw data: `.phase0/out/summary.json`, `.phase0/out/raw.json`.

### 5.1 Analytics projects — the target population and the gate

| Project | Models | Rendered | EXACT |
| --- | ---: | ---: | ---: |
| jaffle_shop_duckdb | 5 | 100% | **100.0%** |
| dbt_bootcamp (airbnb) | 10 | 100% | **90.0%** |
| **Weighted** | **15** | **100%** | **93.3%** |
| **Unweighted per-project mean** | | | **95.0%** |

### 5.2 Libraries / packages — secondary evidence

Reported separately because packages are macro-heavy *by construction*: cross-adapter reusability
is their purpose. They are a harder population than whatbreaks' actual target.

| Project | Models | Rendered | EXACT |
| --- | ---: | ---: | ---: |
| dbt_artifacts | 34 | 100% | **100.0%** |
| dbt-project-evaluator | 48 | 91.7% | **85.4%** |
| snowflake_monitoring | 25 | 92.0% | **84.0%** |
| dbt_expectations (integration_tests) | 12 | 83.3% | **83.3%** |
| elementary | 30 | 20.0% | **20.0%** |
| **Weighted** | **149** | **78.5%** | **75.2%** |
| **Unweighted per-project mean** | | | **74.5%** |

`elementary` at 20% is the clear outlier — it is among the most macro-dense dbt codebases in
existence and is close to a worst case.

### 5.3 Combined

| Metric | Weighted | Per-project mean |
| --- | ---: | ---: |
| `compiled_code` available from `dbt parse` | **0.0%** | — |
| Rendered (naive stub only) | 34.1% | — |
| Rendered (+ compiled manifest macros) | **80.5%** | — |
| sqlglot parsed | 76.8% | — |
| Contains any `SELECT *` | 50.0% | 57.4% |
| **EXACT schema resolution** | **76.8%** | **80.4%** |
| PARTIAL | 0.0% | — |
| UNKNOWN | 23.2% | — |

Residual render failures are a thin tail — `get` (13), `elementary` (8), `load_result` (3),
`dbt` (2) — i.e. no single remaining blocker dominates.

---

## 6. Decision

**GATE PASSED → proceed as designed. `catalog.json` stays optional.**

| Population | EXACT (weighted) | Gate | Verdict |
| --- | ---: | --- | --- |
| Analytics (target) | **93.3%** | ≥70% | **PASS** |
| Packages (harder) | **75.2%** | ≥70% | **PASS** |
| Combined | **76.8%** | ≥70% | **PASS** |

**The reasoning does not rest on the analytics number.** With only 2 projects and 15 models, the
93.3% figure is not decision-grade on its own, and it would be dishonest to lead with it.

The load-bearing argument is the *package* subset. Libraries are systematically harder than
first-party analytics projects — more macros, more dispatch, more adapter branching — and they
still clear the gate at 75.2% weighted / 74.5% per-project mean across 149 models and 5 projects.
Packages function as a **conservative lower bound**: a population strictly harder than the target
cleared the threshold, so the target population can be expected to clear it by a wider margin. The
thin analytics sample is consistent with that and points the same way.

**Therefore:** the zero-warehouse wedge is viable. `catalog.json` remains an optional accuracy
upgrade rather than a requirement, and the "no warehouse, no secrets, no backend" positioning is
supported by evidence rather than hope.

**Confidence: moderate, not high.** See §7 — this should be re-measured once the real engine exists
and can be pointed at a larger, analytics-weighted sample.

## 7. Threats to validity

- **The analytics sample is too small (most serious).** 2 projects / 15 models. tuva_core (1,182
  models), tuva_input and jaffle_shop_modern all failed to parse, and they were three of the five
  analytics projects. The gate call therefore leans deliberately on the package subset as a
  conservative lower bound rather than on the analytics figure. **This must be re-measured** once
  the real engine exists.
- **Sample attrition was severe and non-random.** 7 of 14 projects produced no manifest, and the
  losses were concentrated in exactly the population the gate is about.
- **The probe is not a product-grade dbt context.** 80.5% renderability comes from ~250 lines of
  stubbing plus manifest macro compilation. A real implementation would do better on the residual
  tail — but it also has to survive dbt versions this probe never saw.
- **The probe measures name resolution, not lineage correctness.** "EXACT" means the model's output
  column *set* was resolved unambiguously. It does not prove the column-to-column *edges* are
  right. That is Phase 1's problem and needs its own oracle.
- **Package bias.** Several sample entries are dbt *packages* rather than end-user analytics
  projects. Packages are more macro-heavy and more dialect-portable than typical first-party
  projects, so they likely *understate* renderability for the target user. Noted rather than
  corrected.
- **No private projects.** Public dbt code may be systematically cleaner (more documented columns,
  more consistent style) than private enterprise projects. This likely *overstates* the result.
- **Single dbt version.** All measurements on dbt-core 1.11.12 / sqlglot 30.15.0.
- **Adapter skew.** All projects were parsed against a DuckDB profile regardless of their declared
  adapter, so dialect-specific syntax is exercised only as far as each project's SQL is written for
  its true adapter. Dialect mapping is applied for sqlglot parsing, but adapter-specific macro
  behaviour is not.
