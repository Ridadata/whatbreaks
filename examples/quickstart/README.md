# Quickstart example

A three-model dbt project, committed as two `manifest.json` files — one before a change, one after.
No warehouse, no dbt install, no setup.

```bash
whatbreaks check \
  --base examples/quickstart/base/target/manifest.json \
  --head examples/quickstart/head/target/manifest.json
```

This is the run in the README's demo.

## The change

`stg_orders` drops its `status` column:

```diff
  select
      id      as order_id,
      user_id as customer_id,
      order_date,
-     status,
      amount
  from source
```

## Why these three models

Each one exists to exercise a distinct behaviour.

| Model | Uses `status` | What it demonstrates |
| :--- | :--- | :--- |
| `orders` | `orders.status as order_status` | a named dependency, through a **CTE alias** — the canonical dbt idiom, and the case where naive alias resolution misses the reference entirely |
| `customers` | `where status = 'completed'` | a **filter-only** dependency. It never reaches an output column, so projection lineage alone would miss it — yet removing `status` breaks the model outright |
| `stg_orders` | — | carries an `accepted_values` test on `status`, so the test shows up in the blast radius |

There is also an exposure (`revenue_dashboard`) downstream of `orders`, so the report reaches past
the dbt DAG into something a human recognises.

## What you should see

A `BREAKING` finding naming both consumers, the affected downstream column, the failing test and the
exposure — and a coverage line, because `orders` cannot be fully resolved once the column it selects
no longer exists. That last part is not a defect: it is the tool declining to guess, and saying so.

Exit code `1`.

## Regenerating

```bash
python examples/quickstart/build.py
```

The manifests are generated rather than hand-maintained, so they cannot drift out of sync with each
other.
