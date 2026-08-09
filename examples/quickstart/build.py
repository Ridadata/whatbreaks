"""Generate the quickstart example's two manifests.

A committed, runnable example matters more than it looks: it lets someone try
whatbreaks 20 seconds after cloning, without a warehouse, a dbt install, or a
project of their own. It is also what the README's demo is recorded against, so
the demo is reproducible rather than a screenshot nobody can check.

The change it models is the canonical one: a column is dropped from a staging
model that two downstream models still use - one by name, one only in a filter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent

BASE_STG = """
with source as (
    select * from {{ ref('raw_orders') }}
),
renamed as (
    select
        id           as order_id,
        user_id      as customer_id,
        order_date,
        status,
        amount
    from source
)
select * from renamed
"""

# The change under review: `status` is gone.
HEAD_STG = BASE_STG.replace("        status,\n", "")

ORDERS = """
with orders as (
    select * from {{ ref('stg_orders') }}
)
select
    orders.order_id,
    orders.customer_id,
    orders.status      as order_status,
    orders.amount
from orders
"""

# Uses `status` only in a WHERE clause: it never reaches an output column, so
# projection lineage alone would miss it - but removing `status` still breaks
# this model outright.
CUSTOMERS = """
with completed as (
    select * from {{ ref('stg_orders') }} where status = 'completed'
)
select
    customer_id,
    count(order_id) as completed_orders,
    sum(amount)     as lifetime_value
from completed
group by customer_id
"""


def node(
    name: str,
    sql: str,
    depends: list[str],
    columns: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "resource_type": "model",
        "package_name": "shop",
        "original_file_path": f"models/{name}.sql",
        "raw_code": sql,
        "depends_on": {"nodes": depends},
        "columns": {c: {"name": c} for c in (columns or [])},
        "config": {"materialized": "view"},
    }


def manifest(*, head: bool) -> dict[str, Any]:
    nodes: dict[str, Any] = {
        "seed.shop.raw_orders": {
            "name": "raw_orders",
            "resource_type": "seed",
            "package_name": "shop",
            "original_file_path": "seeds/raw_orders.csv",
            "depends_on": {"nodes": []},
            "columns": {
                c: {"name": c} for c in ("id", "user_id", "order_date", "status", "amount")
            },
            "config": {},
        },
        "model.shop.stg_orders": node(
            "stg_orders", HEAD_STG if head else BASE_STG, ["seed.shop.raw_orders"]
        ),
        # Documented columns, as a maintained project would have. It means a
        # model broken by the change degrades to PARTIAL rather than UNKNOWN,
        # which is both realistic and a better demonstration.
        "model.shop.orders": node(
            "orders",
            ORDERS,
            ["model.shop.stg_orders"],
            columns=["order_id", "customer_id", "order_status", "amount"],
        ),
        "model.shop.customers": node("customers", CUSTOMERS, ["model.shop.stg_orders"]),
        "test.shop.accepted_values_stg_orders_status": {
            "name": "accepted_values_stg_orders_status",
            "resource_type": "test",
            "package_name": "shop",
            "column_name": "status",
            "attached_node": "model.shop.stg_orders",
            "depends_on": {"nodes": ["model.shop.stg_orders"]},
            "test_metadata": {"name": "accepted_values"},
        },
    }
    return {
        "metadata": {
            "dbt_schema_version": "https://schemas.getdbt.com/dbt/manifest/v12.json",
            "dbt_version": "1.11.12",
            "adapter_type": "duckdb",
            "project_name": "shop",
        },
        "nodes": nodes,
        "sources": {},
        "exposures": {
            "exposure.shop.revenue_dashboard": {
                "name": "revenue_dashboard",
                "type": "dashboard",
                "url": "https://bi.example.com/revenue",
                "owner": {"email": "analytics@example.com"},
                "depends_on": {"nodes": ["model.shop.orders"]},
            }
        },
        "macros": {},
    }


def write(side: str, *, head: bool) -> Path:
    root = HERE / side
    (root / "target").mkdir(parents=True, exist_ok=True)
    (root / "dbt_project.yml").write_text("name: shop\nprofile: shop\n", encoding="utf-8")
    path = root / "target" / "manifest.json"
    path.write_text(json.dumps(manifest(head=head), indent=1), encoding="utf-8")
    return path


if __name__ == "__main__":
    for side, is_head in (("base", False), ("head", True)):
        print(f"wrote {write(side, head=is_head).relative_to(HERE)}")
