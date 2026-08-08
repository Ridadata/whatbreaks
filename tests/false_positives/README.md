# False-positive corpus

**This is a release gate.** A regression here blocks a release.

## Why it exists

A linter's false-positive rate decides whether it survives contact with a real
team far more than its feature count does. One wrong `BREAKING` on a
reformatting commit and whatbreaks gets removed from the workflow that week —
and the person who removes it will be right to.

So the false-positive budget is effectively zero, and it is enforced rather
than intended.

## The bar

Every case is a change that **must not alarm anyone**. Concretely: no finding
at or above `POSSIBLY_BREAKING`.

`SAFE` and `INFO` findings are allowed. They are informational, they do not
fail CI under the default `--fail-on=breaking`, and suppressing them would hide
genuinely useful signal like "a column was added".

Two further conditions keep the gate honest:

- **Cases must analyse completely.** A case whose models fail to resolve would
  pass trivially, for the wrong reason — we cannot report a break in a model we
  never read. `test_no_op_change_analyses_completely` rejects that.
- **The gate must be able to fail.** Every other assertion here is of the form
  "nothing was reported", so a harness that silently analysed nothing would
  pass everything. `test_the_gate_can_actually_fail` feeds the same harness a
  real breaking change and requires that it *is* flagged.

## Adding a case

Drop a YAML file in `cases/`. That is the whole process — no code.

```yaml
description: what changed, in one line
why: |
  Why this must be silent. Not optional: a case without a rationale is a case
  nobody can maintain, and a test asserts this field is present.
models:
  orders:
    base: "select 1 as a from tbl"
    head: "SELECT 1 AS a FROM tbl"   # omit `head` if the model is unchanged
    depends_on: [some_other_model]   # optional
    columns: [a]                     # optional, YAML-declared columns
    config: {materialized: view}     # optional
```

**Found SQL that whatbreaks gets wrong?** A case file is the most useful
possible bug report: it reproduces the problem, documents the expectation, and
becomes the regression test the moment it is fixed.
