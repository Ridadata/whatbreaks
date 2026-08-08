"""Debug: why does the manifest macro registry not resolve package namespaces?"""
from __future__ import annotations
import json, sys, traceback
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import phase0_probe as P  # noqa: E402

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace"))
adapter = (manifest.get("metadata") or {}).get("adapter_type", "")
nodes = manifest.get("nodes") or {}
models = {k: v for k, v in nodes.items() if v.get("resource_type") == "model"}

by_pkg = defaultdict(list)
for m in (manifest.get("macros") or {}).values():
    if m.get("macro_sql") and m.get("name"):
        by_pkg[m.get("package_name") or "?"].append(m)

print("packages in manifest.macros:")
for pkg, ms in sorted(by_pkg.items(), key=lambda x: -len(x[1])):
    print(f"   {pkg:35} {len(ms)} macros")

probe_node = dict(next(iter(models.values())))
probe_node["_adapter"] = adapter
shared = dict(P.build_stub_context(probe_node))
shared["execute"] = False
shared["return"] = lambda v=None: v

print("\nper-package compile results:")
for pkg, macros in sorted(by_pkg.items(), key=lambda x: -len(x[1]))[:6]:
    src = "\n".join(m["macro_sql"] for m in macros)
    try:
        mod = P.JINJA_ENV.from_string(src).make_module(shared)
        ok = sum(1 for m in macros if callable(getattr(mod, m["name"], None)))
        print(f"   {pkg:35} BULK OK   exported {ok}/{len(macros)}")
    except Exception as e:
        print(f"   {pkg:35} BULK FAIL {type(e).__name__}: {str(e)[:130]}")
        ok = 0
        first_err = None
        for m in macros:
            try:
                m2 = P.JINJA_ENV.from_string(m["macro_sql"]).make_module(shared)
                if callable(getattr(m2, m["name"], None)):
                    ok += 1
            except Exception as e2:
                if first_err is None:
                    first_err = f"{m['name']}: {type(e2).__name__}: {str(e2)[:110]}"
        print(f"   {'':35} one-by-one exported {ok}/{len(macros)}; first err: {first_err}")

print("\nregistry via probe:")
try:
    ctx = P.build_macro_registry(manifest, probe_node)
    ns = {k: v for k, v in ctx.items() if isinstance(v, P.MacroNamespace)}
    print("   namespaces:", sorted(ns))
    print("   total ctx keys:", len(ctx))
except Exception:
    traceback.print_exc()

# try rendering a model that failed
target = sys.argv[2] if len(sys.argv) > 2 else None
for uid, node in models.items():
    if target and node.get("name") != target:
        continue
    node["_adapter"] = adapter
    sql, reason = P.render_raw_code(node, ctx)
    print(f"\nMODEL {node['name']}: rendered={sql is not None} reason={reason}")
    if sql is None:
        print("raw_code head:")
        print((node.get("raw_code") or "")[:600])
    break
