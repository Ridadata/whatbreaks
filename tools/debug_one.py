"""Debug helper: show exactly why one model resolves the way it does."""

from __future__ import annotations
import json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from phase0_probe import render_raw_code, parse_sql, DIALECT_MAP, _ident  # noqa: E402

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
target = sys.argv[2] if len(sys.argv) > 2 else None

adapter = (manifest.get("metadata") or {}).get("adapter_type", "")
dialect = DIALECT_MAP.get(adapter, "")
nodes = manifest.get("nodes") or {}
models = {k: v for k, v in nodes.items() if v.get("resource_type") == "model"}
seeds = {k: v for k, v in nodes.items() if v.get("resource_type") == "seed"}

print(f"adapter={adapter} dialect={dialect} models={len(models)} seeds={len(seeds)}")
print("seed columns declared:", {v["name"]: len(v.get("columns") or {}) for v in seeds.values()})

for uid, node in models.items():
    if target and node.get("name") != target:
        continue
    node["_adapter"] = adapter
    print("\n" + "=" * 70)
    print("MODEL:", node.get("name"))
    sql, reason = render_raw_code(node)
    if sql is None:
        print("RENDER FAILED:", reason)
        continue
    print("--- rendered ---")
    print(sql.strip()[:900])
    tree, err = parse_sql(sql, dialect)
    if tree is None:
        print("PARSE FAILED:", err)
        continue
    print("--- qualify with EMPTY schema ---")
    try:
        q = qualify(
            tree.copy(),
            schema={},
            dialect=dialect or None,
            infer_schema=True,
            validate_qualify_columns=False,
        )
        sel = q if isinstance(q, exp.Select) else q.find(exp.Select)
        outs = [e.alias_or_name for e in sel.expressions] if sel else []
        stars = [
            e
            for e in (sel.expressions if sel else [])
            if isinstance(e, exp.Star)
            or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
        ]
        print("outputs:", outs)
        print("unresolved top-level stars:", len(stars))
    except Exception as e:
        print("QUALIFY ERROR:", type(e).__name__, e)

    print("--- qualify WITH leaf schema supplied ---")
    leaf_schema = {}
    for d in (node.get("depends_on") or {}).get("nodes", []):
        n2 = nodes.get(d)
        if not n2:
            continue
        cols = list((n2.get("columns") or {}).keys())
        if n2.get("resource_type") == "seed" and not cols:
            cols = [
                "id",
                "first_name",
                "last_name",
                "order_id",
                "amount",
                "user_id",
                "order_date",
                "status",
                "payment_method",
            ]
        if cols:
            leaf_schema[_ident("model", n2["name"])] = {c: "UNKNOWN" for c in cols}
    print("leaf schema keys:", list(leaf_schema))
    try:
        q = qualify(
            tree.copy(),
            schema=leaf_schema,
            dialect=dialect or None,
            infer_schema=True,
            validate_qualify_columns=False,
        )
        sel = q if isinstance(q, exp.Select) else q.find(exp.Select)
        outs = [e.alias_or_name for e in sel.expressions] if sel else []
        stars = [
            e
            for e in (sel.expressions if sel else [])
            if isinstance(e, exp.Star)
            or (isinstance(e, exp.Column) and isinstance(e.this, exp.Star))
        ]
        print("outputs:", outs)
        print("unresolved top-level stars:", len(stars))
    except Exception as e:
        print("QUALIFY ERROR:", type(e).__name__, e)
