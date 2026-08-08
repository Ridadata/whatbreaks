"""
Phase 0 feasibility probe for whatbreaks.

RESEARCH SCRIPT -- not product code. Its only job is to answer, with numbers,
whether a dbt project can be analysed at the column level WITHOUT a warehouse.

It measures four things per project, per the plan's Phase 0 gate:

  M1  compiled_code availability   -- does `dbt parse` give us SQL for free?
  M2  Jinja renderability          -- can we recover parseable SQL offline?
  M3  SELECT * prevalence          -- how often is star expansion required?
  M4  seed-schema quality          -- do sources/models declare columns in YAML?

and then runs the real thing:

  M5  topological schema inference -- what % of models reach EXACT resolution?

M5 is the gate. Plan says: >=70% EXACT -> proceed as designed.
                          50-70%       -> proceed, reposition catalog.json as recommended.
                          <50%         -> the wedge is wrong; revise.

Usage:  python phase0_probe.py --projects-root <dir> --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.qualify import qualify

# --------------------------------------------------------------------------
# dbt adapter type -> sqlglot dialect
# --------------------------------------------------------------------------
DIALECT_MAP = {
    "snowflake": "snowflake",
    "bigquery": "bigquery",
    "duckdb": "duckdb",
    "postgres": "postgres",
    "redshift": "redshift",
    "databricks": "databricks",
    "spark": "spark",
    "trino": "trino",
    "athena": "athena",
    "clickhouse": "clickhouse",
    "sqlserver": "tsql",
    "fabric": "tsql",
}


# --------------------------------------------------------------------------
# Jinja stubbing -- the offline "recover SQL from raw_code" path
# --------------------------------------------------------------------------
class UnresolvedMacro(Exception):
    """Raised when the template touches something we deliberately refuse to fake."""

    def __init__(self, kind: str, name: str):
        self.kind = kind
        self.name = name
        super().__init__(f"{kind}:{name}")


class RecordingUndefined(jinja2.Undefined):
    """Any name we did not stub becomes a hard, *named* failure.

    This is the whole point: we must never silently render an unknown macro to
    empty string, because that produces syntactically-valid but semantically
    wrong SQL -- the exact failure mode whatbreaks exists to avoid.
    """

    def _fail(self, *args: Any, **kwargs: Any):
        raise UnresolvedMacro("undefined_macro", self._undefined_name or "<unknown>")

    __call__ = _fail
    __getattr__ = _fail
    __getitem__ = _fail
    __str__ = _fail
    __iter__ = _fail
    __len__ = _fail
    __bool__ = _fail
    __eq__ = _fail
    __ne__ = _fail
    __hash__ = _fail  # type: ignore[assignment]


def _ident(*parts: str) -> str:
    slug = "_".join(re.sub(r"[^0-9a-zA-Z_]", "_", p) for p in parts if p)
    return f"wb_{slug}"


class _Introspective:
    """Stands in for `adapter` / `run_query` -- always refuses, loudly."""

    def __init__(self, name: str):
        self._name = name

    def __call__(self, *a: Any, **k: Any):
        raise UnresolvedMacro("introspective", self._name)

    def __getattr__(self, item: str):
        raise UnresolvedMacro("introspective", f"{self._name}.{item}")


class _Target(dict):
    def __getattr__(self, item: str):
        return self.get(item, "wb_target")


class _Relation(str):
    """Behaves like an identifier but tolerates dbt's Relation API surface."""

    def __getattr__(self, item: str):
        if item in ("identifier", "name", "schema", "database", "table"):
            return str(self)
        if item in ("create", "quote", "include", "incorporate", "render"):
            return lambda *a, **k: self
        if item.startswith("is_"):
            return False
        raise UnresolvedMacro("undefined_macro", f"Relation.{item}")


class _Api:
    """`{{ api.Relation.create(...) }}` -- structural, not introspective."""

    class Relation:
        @staticmethod
        def create(*a: Any, **k: Any) -> _Relation:
            parts = [str(v) for v in a if v] or [
                str(k.get("identifier") or k.get("schema") or "wb_rel")
            ]
            return _Relation(_ident(*parts))

    class Column:
        @staticmethod
        def create(*a: Any, **k: Any) -> _Relation:
            return _Relation(_ident(*[str(v) for v in a if v]))

        @staticmethod
        def translate_type(t: Any = "text", *a: Any, **k: Any) -> str:
            return str(t)


def build_stub_context(node: dict[str, Any]) -> dict[str, Any]:
    """The minimum dbt context needed to render a model body offline.

    Deliberately does NOT stub package macros (dbt_utils.*, etc). We want those
    to fail loudly so we can count them.
    """

    def ref(*args: Any, **kwargs: Any) -> str:
        names = [a for a in args if isinstance(a, str)]
        if not names:
            return _ident("ref", "unknown")
        return _ident("model", names[-1])

    def source(*args: Any, **kwargs: Any) -> str:
        names = [a for a in args if isinstance(a, str)]
        return _ident("source", *names)

    def _noop(*a: Any, **k: Any) -> str:
        return ""

    def var(name: Any = None, default: Any = None, *a: Any, **k: Any) -> Any:
        return default if default is not None else ""

    def env_var(name: Any = None, default: Any = None, *a: Any, **k: Any) -> Any:
        return default if default is not None else ""

    return {
        "ref": ref,
        "source": source,
        "config": _noop,
        "log": _noop,
        "print": _noop,
        "var": var,
        "env_var": env_var,
        "this": _ident("this", node.get("name", "x")),
        "target": _Target(
            name="wb", schema="wb", database="wb", type=node.get("_adapter", "duckdb")
        ),
        "is_incremental": lambda: False,
        "should_full_refresh": lambda: False,
        "adapter": _Introspective("adapter"),
        "run_query": _Introspective("run_query"),
        "statement": _Introspective("statement"),
        "model": {"name": node.get("name", "x"), "columns": {}},
        "builtins": {},
        "flags": {"FULL_REFRESH": False, "WHICH": "parse"},
        "modules": {"datetime": __import__("datetime"), "re": re},
        "exceptions": _Introspective("exceptions"),
        "invocation_id": "wb",
        "dbt_version": "1.11.0",
        "selected_resources": [],
        "api": _Api(),
        "load_result": _Introspective("load_result"),
        "graph": {"nodes": {}, "sources": {}},
    }


JINJA_ENV = SandboxedEnvironment(
    undefined=RecordingUndefined,
    extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"],
    keep_trailing_newline=True,
)


class MacroNamespace:
    """Exposes `dbt_utils.star(...)` / `dbt.type_string(...)` style access."""

    def __init__(self, name: str, macros: dict[str, Any]):
        self._name = name
        self._macros = macros

    def __getattr__(self, item: str):
        if item in self._macros:
            return self._macros[item]
        raise UnresolvedMacro("undefined_macro", f"{self._name}.{item}")

    def __getitem__(self, item: str):
        return self.__getattr__(item)


def build_macro_registry(manifest: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """Compile every macro in the manifest into callables.

    KEY INSIGHT (ADR 000 F6): manifest.json carries the full source of every
    macro -- first-party AND package (`macro_sql`). Package macros therefore do
    not need to be faked; they can be compiled and executed offline. This is the
    difference between "we cannot resolve dbt_utils" and "we can".
    """
    by_pkg: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in (manifest.get("macros") or {}).values():
        sql = (m.get("macro_sql") or "").lstrip()
        if not sql or not m.get("name"):
            continue
        # dbt's macro_sql also carries {% materialization %}, {% test %} and
        # {% snapshot %} blocks -- dbt Jinja extensions that plain Jinja2
        # cannot parse. Keep only real macros; the rest are not callable from
        # a model body anyway.
        if not (sql.startswith("{% macro") or sql.startswith("{%- macro")):
            continue
        by_pkg[m.get("package_name") or "?"].append(m)

    flat: dict[str, Any] = {}
    namespaces: dict[str, dict[str, Any]] = defaultdict(dict)

    def dispatch(macro_name: str, macro_namespace: Any = None, *a: Any, **k: Any):
        # dbt resolves adapter.dispatch('x') to <adapter>__x, then default__x
        for cand in (
            f"duckdb__{macro_name}", f"postgres__{macro_name}",
            f"default__{macro_name}", macro_name,
        ):
            if cand in flat:
                return flat[cand]
        raise UnresolvedMacro("dispatch", macro_name)

    class _Adapter:
        def __getattr__(self, item: str):
            if item == "dispatch":
                return dispatch
            raise UnresolvedMacro("introspective", f"adapter.{item}")

    shared: dict[str, Any] = dict(build_stub_context(node))
    shared["adapter"] = _Adapter()
    shared["execute"] = False
    shared["return"] = lambda v=None: v

    def compile_pass(globals_: dict[str, Any]) -> None:
        """Compile every package's macros against `globals_`."""
        flat.clear()
        namespaces.clear()
        for pkg, macros in by_pkg.items():
            try:
                mod = JINJA_ENV.from_string(
                    "\n".join(m["macro_sql"] for m in macros)
                ).make_module(globals_)
                got = [(m["name"], getattr(mod, m["name"], None)) for m in macros]
            except Exception:  # noqa: BLE001
                # one bad macro must not poison an entire package
                got = []
                for m in macros:
                    try:
                        m2 = JINJA_ENV.from_string(m["macro_sql"]).make_module(globals_)
                        got.append((m["name"], getattr(m2, m["name"], None)))
                    except Exception:  # noqa: BLE001
                        continue
            for nm, fn in got:
                if callable(fn):
                    flat.setdefault(nm, fn)
                    namespaces[pkg][nm] = fn

    def assemble() -> dict[str, Any]:
        ctx = dict(shared)
        ctx.update(flat)  # bare-name access: {{ star(...) }}
        for pkg, ms in namespaces.items():
            ctx[pkg] = MacroNamespace(pkg, ms)
        return ctx

    # Pass 1 discovers what exists. Pass 2 recompiles against a context that
    # CONTAINS those macros, so a macro can call another package's macro --
    # e.g. elementary.x() calling dbt_utils.y(). Without this second pass the
    # namespaces exist for models but are invisible inside macro bodies.
    compile_pass(shared)
    compile_pass(assemble())
    return assemble()


def render_raw_code(
    node: dict[str, Any], macro_ctx: dict[str, Any] | None = None
) -> tuple[str | None, str | None]:
    """-> (rendered_sql, failure_reason). Exactly one is non-None."""
    raw = node.get("raw_code") or ""
    if not raw.strip():
        return None, "empty_raw_code"
    if macro_ctx:
        ctx = dict(macro_ctx)
        node_ctx = build_stub_context(node)
        # node-specific values (ref/this/model) win; adapter/execute/macros stay
        for k in ("ref", "source", "this", "model", "config", "var", "env_var",
                  "is_incremental", "target"):
            ctx[k] = node_ctx[k]
    else:
        ctx = build_stub_context(node)
    try:
        tmpl = JINJA_ENV.from_string(raw)
        return tmpl.render(ctx), None
    except UnresolvedMacro as e:
        return None, f"{e.kind}:{e.name}"
    except jinja2.TemplateSyntaxError:
        return None, "jinja_syntax"
    except RecursionError:
        return None, "jinja_recursion"
    except Exception as e:  # noqa: BLE001 - probe must never crash
        return None, f"jinja_runtime:{type(e).__name__}"


# --------------------------------------------------------------------------
# SQL analysis
# --------------------------------------------------------------------------
def parse_sql(sql: str, dialect: str) -> tuple[Any, str | None]:
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
        if tree is None:
            return None, "parse_empty"
        return tree, None
    except Exception as e:  # noqa: BLE001
        return None, f"parse_error:{type(e).__name__}"


def star_profile(tree: Any) -> dict[str, bool]:
    """Where do stars appear? Top-level projection stars are the ones that
    actually force catalog knowledge for *our* output schema."""
    has_any = bool(list(tree.find_all(exp.Star)))
    top_level = False
    if isinstance(tree, exp.Select):
        for proj in tree.expressions:
            if isinstance(proj, exp.Star):
                top_level = True
            elif isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
                top_level = True
    elif isinstance(tree, exp.Query):
        sel = tree.find(exp.Select)
        if sel is not None:
            for proj in sel.expressions:
                if isinstance(proj, exp.Star) or (
                    isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)
                ):
                    top_level = True
    return {"any_star": has_any, "top_level_star": top_level}


# --------------------------------------------------------------------------
# Per-project run
# --------------------------------------------------------------------------
@dataclass
class ModelResult:
    unique_id: str
    name: str
    has_compiled_code: bool = False
    rendered: bool = False
    rendered_naive: bool = False  # Tier A: stub only, no manifest macros
    render_fail: str | None = None
    render_fail_naive: str | None = None
    parsed: bool = False
    parse_fail: str | None = None
    any_star: bool = False
    top_level_star: bool = False
    declared_columns: int = 0
    resolution: str = "UNKNOWN"  # EXACT | PARTIAL | UNKNOWN
    resolution_reason: str = ""
    inferred_columns: int = 0


@dataclass
class ProjectResult:
    name: str
    subdir: str
    adapter: str = ""
    dialect: str = ""
    dbt_parse_ok: bool = False
    dbt_parse_error: str = ""
    n_models: int = 0
    n_sources: int = 0
    n_sources_with_columns: int = 0
    models: list[ModelResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# Per-project dbt vars needed only to make `dbt parse` succeed. These enable
# optional model groups that are disabled by default; without them dbt aborts
# with "depends on a node named X which is disabled". Enabling them makes the
# sample LARGER and more representative, not cherry-picked.
PROJECT_VARS: dict[str, str] = {}

# whatbreaks' target user is an analytics engineer with a FIRST-PARTY dbt
# project. dbt *packages* are libraries: macro-heavy by construction, because
# being reusable across adapters is their whole job. Mixing the two produces a
# number that describes neither population, so results are reported split.
PROJECT_KIND: dict[str, str] = {
    "jaffle_shop_duckdb": "analytics",
    "jaffle_shop_modern": "analytics",
    "dbt_bootcamp": "analytics",
    "tuva_core": "analytics",
    "tuva_input": "analytics",
    "dbt_artifacts": "package",
    "elementary": "package",
    "dbt_expectations": "package",
    "snowflake_monitoring": "package",
    "fivetran_shopify": "package",
    "fivetran_netsuite": "package",
    "fivetran_hubspot": "package",
    "velir_ga4": "package",
    "gitlab_snowflake": "package",
}

# Which adapter to parse each project against. DuckDB is a convenient default
# but it is OUR choice, not the project's -- packages written for Postgres /
# Snowflake dispatch macros like `assert_not_null` that dbt-duckdb has no
# implementation for, and parsing fails for reasons that have nothing to do
# with whatbreaks. Parsing never connects, so the adapter only needs installing.
PROJECT_ADAPTER: dict[str, str] = {
    "fivetran_shopify": "postgres",
    "fivetran_netsuite": "postgres",
    "fivetran_hubspot": "postgres",
    "velir_ga4": "postgres",
    "dbt_artifacts": "postgres",
}

_PROFILE_BODY = {
    "duckdb": '      type: duckdb\n      path: "{path}"\n      threads: 1\n',
    "postgres": (
        "      type: postgres\n      host: localhost\n      port: 5432\n"
        "      user: wb\n      password: wb\n      dbname: wb\n"
        "      schema: wb\n      threads: 1\n"
    ),
}


def write_profiles(project_dir: Path, profile_name: str, tmp: Path, adapter: str) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    body = _PROFILE_BODY[adapter].format(path=(tmp / "wb.duckdb").as_posix())
    (tmp / "profiles.yml").write_text(
        f"{profile_name}:\n  target: wb\n  outputs:\n    wb:\n{body}",
        encoding="utf-8",
    )
    return tmp


def read_profile_name(project_dir: Path) -> str:
    txt = (project_dir / "dbt_project.yml").read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*profile\s*:\s*(.+)$", txt, re.M)
    if not m:
        return "default"
    val = m.group(1)
    val = re.sub(r"\s+#.*$", "", val)          # strip inline comment
    val = val.strip().strip("'\"").strip()      # strip quotes
    return val or "default"


def run_dbt(args: list[str], cwd: Path, env: dict[str, str], timeout: int = 900):
    return subprocess.run(
        args, cwd=str(cwd), env=env, capture_output=True, text=True,
        timeout=timeout, errors="replace",
    )


def probe_project(
    name: str,
    project_dir: Path,
    dbt_exe: Path,
    workdir: Path,
    extra_vars: str = "",
) -> ProjectResult:
    subdir = project_dir.name
    res = ProjectResult(name=name, subdir=str(subdir))

    # Some dbt *packages* omit `profile:` entirely. dbt refuses to parse without
    # one, so we always pass --profile explicitly rather than relying on the key.
    profile_name = read_profile_name(project_dir)
    tmp = workdir / name
    write_profiles(project_dir, profile_name, tmp, PROJECT_ADAPTER.get(name, "duckdb"))

    env = dict(os.environ)
    env["DBT_PROFILES_DIR"] = str(tmp)
    env["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "False"
    env["DO_NOT_TRACK"] = "1"

    # NOTE: we deliberately do NOT run `dbt deps`. Two reasons:
    #   1. hub.getdbt.com is unreachable here (Avast TLS interception, see ADR 000 F4)
    #   2. `dbt deps` CLEARS dbt_packages/ before installing -- so calling it
    #      destroys the packages tools/vendor_packages.py just vendored, and
    #      then fails, leaving the project worse off than if we had done nothing.
    # Packages must be vendored via tools/vendor_packages.py before probing.

    cmd = [str(dbt_exe), "parse", "--no-version-check", "--profile", profile_name]
    if extra_vars:
        cmd += ["--vars", extra_vars]
    try:
        p = run_dbt(cmd, project_dir, env, 900)
    except subprocess.TimeoutExpired:
        res.dbt_parse_error = "timeout"
        return res
    except Exception as e:  # noqa: BLE001
        res.dbt_parse_error = f"{type(e).__name__}: {e}"
        return res

    manifest_path = project_dir / "target" / "manifest.json"
    if not manifest_path.exists():
        res.dbt_parse_error = (p.stderr or p.stdout or "")[-1500:]
        return res
    res.dbt_parse_ok = True

    manifest = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    res.adapter = (manifest.get("metadata") or {}).get("adapter_type", "") or ""
    res.dialect = DIALECT_MAP.get(res.adapter, "")

    nodes = manifest.get("nodes") or {}
    sources = manifest.get("sources") or {}
    res.n_sources = len(sources)
    res.n_sources_with_columns = sum(1 for s in sources.values() if s.get("columns"))

    models = {k: v for k, v in nodes.items() if v.get("resource_type") == "model"}
    res.n_models = len(models)

    # ---- schema seed --------------------------------------------------
    # sqlglot schema keyed by the identifiers our stub's ref()/source() emit
    schema: dict[str, dict[str, str]] = {}
    resolution: dict[str, str] = {}

    for s in sources.values():
        ident = _ident("source", s.get("source_name", ""), s.get("name", ""))
        cols = list((s.get("columns") or {}).keys())
        if cols:
            schema[ident] = {c: "UNKNOWN" for c in cols}
            resolution[s["unique_id"]] = "EXACT"
        else:
            resolution[s["unique_id"]] = "UNKNOWN"

    # Seeds are free schema: the CSV header IS the column list, and it is on
    # disk. No warehouse needed. Same for any node that declares columns in YAML.
    for uid, n2 in nodes.items():
        rt = n2.get("resource_type")
        if rt not in ("seed", "snapshot"):
            continue
        cols = list((n2.get("columns") or {}).keys())
        if not cols and rt == "seed":
            csv = project_dir / (n2.get("original_file_path") or "")
            try:
                if csv.is_file():
                    header = csv.open("r", encoding="utf-8", errors="replace").readline()
                    cols = [h.strip().strip('"').strip("'") for h in header.split(",") if h.strip()]
            except Exception:  # noqa: BLE001
                cols = []
        ident = _ident("model", n2.get("name", ""))
        if cols:
            schema[ident] = {c: "UNKNOWN" for c in cols}
            resolution[uid] = "EXACT"
        else:
            resolution[uid] = "UNKNOWN"

    # ---- topological order over model deps ----------------------------
    deps = {
        uid: [d for d in (n.get("depends_on") or {}).get("nodes", []) if d in models]
        for uid, n in models.items()
    }
    order: list[str] = []
    seen: dict[str, int] = {}

    def visit(uid: str) -> None:
        st = seen.get(uid, 0)
        if st == 1 or st == 2:
            return
        seen[uid] = 1
        for d in deps.get(uid, []):
            visit(d)
        seen[uid] = 2
        order.append(uid)

    for uid in models:
        visit(uid)

    # Compile every macro in the manifest ONCE per project (see F6).
    try:
        probe_node = dict(next(iter(models.values()))) if models else {}
        probe_node["_adapter"] = res.adapter
        macro_ctx: dict[str, Any] | None = build_macro_registry(manifest, probe_node)
        res.notes.append(f"macro registry: {len(macro_ctx)} names")
    except Exception as e:  # noqa: BLE001
        macro_ctx = None
        res.notes.append(f"macro registry failed: {type(e).__name__}")

    # ---- per model ----------------------------------------------------
    for uid in order:
        node = models[uid]
        node["_adapter"] = res.adapter
        mr = ModelResult(unique_id=uid, name=node.get("name", ""))
        mr.declared_columns = len(node.get("columns") or {})
        mr.has_compiled_code = bool(node.get("compiled_code"))

        # Tier A vs Tier B: measure what compiling manifest macros actually buys
        naive_sql, naive_reason = render_raw_code(node, None)
        mr.rendered_naive = naive_sql is not None
        mr.render_fail_naive = naive_reason

        sql = node.get("compiled_code") or None
        if sql is None:
            sql, reason = render_raw_code(node, macro_ctx)
            if sql is None:
                mr.render_fail = reason
                mr.resolution = "UNKNOWN"
                mr.resolution_reason = f"render:{reason}"
                resolution[uid] = "UNKNOWN"
                res.models.append(mr)
                continue
        mr.rendered = True

        tree, perr = parse_sql(sql, res.dialect or "")
        if tree is None:
            mr.parse_fail = perr
            mr.resolution = "UNKNOWN"
            mr.resolution_reason = f"parse:{perr}"
            resolution[uid] = "UNKNOWN"
            res.models.append(mr)
            continue
        mr.parsed = True

        sp = star_profile(tree)
        mr.any_star = sp["any_star"]
        mr.top_level_star = sp["top_level_star"]

        # Parent state does NOT gate resolution on its own. A star over a CTE
        # is perfectly resolvable; only a star that SURVIVES qualification is
        # evidence that we are missing schema. Parent state is used to explain
        # a surviving star, not to predict one.
        parent_states = [
            resolution.get(d, "UNKNOWN")
            for d in (node.get("depends_on") or {}).get("nodes", [])
        ]
        parents_all_known = all(s == "EXACT" for s in parent_states) if parent_states else True

        try:
            qualified = qualify(
                tree.copy(),
                schema=schema,
                dialect=res.dialect or None,
                infer_schema=True,
                validate_qualify_columns=False,
            )
            sel = qualified if isinstance(qualified, exp.Select) else qualified.find(exp.Select)
            out_cols: list[str] = []
            unresolved_star = False
            if sel is not None:
                for e in sel.expressions:
                    if isinstance(e, exp.Star) or (
                        isinstance(e, exp.Column) and isinstance(e.this, exp.Star)
                    ):
                        unresolved_star = True
                    else:
                        alias = e.alias_or_name
                        if alias:
                            out_cols.append(alias)

            mr.inferred_columns = len(out_cols)
            if not out_cols:
                mr.resolution = "UNKNOWN"
                mr.resolution_reason = "no_output_columns"
            elif unresolved_star:
                # some columns known, but a star we could not expand remains
                mr.resolution = "PARTIAL"
                mr.resolution_reason = (
                    "surviving_star_over_unknown_parent"
                    if not parents_all_known
                    else "surviving_star"
                )
            else:
                mr.resolution = "EXACT"
                mr.resolution_reason = (
                    "star_resolved_via_cte" if mr.any_star else ""
                )
        except Exception as e:  # noqa: BLE001
            mr.resolution = "UNKNOWN"
            mr.resolution_reason = f"qualify:{type(e).__name__}"

        resolution[uid] = mr.resolution
        if mr.resolution in ("EXACT", "PARTIAL") and mr.inferred_columns:
            ident = _ident("model", node.get("name", ""))
            schema[ident] = {c: "UNKNOWN" for c in out_cols}

        res.models.append(mr)

    return res


# --------------------------------------------------------------------------
def summarise(results: list[ProjectResult]) -> dict[str, Any]:
    rows = []
    agg = Counter()
    render_fails = Counter()
    parse_fails = Counter()

    for r in results:
        if not r.dbt_parse_ok:
            rows.append({"project": r.name, "status": "PARSE_FAILED", "error": r.dbt_parse_error[:200]})
            continue
        n = len(r.models)
        c = Counter(m.resolution for m in r.models)
        stars = sum(1 for m in r.models if m.any_star)
        tl_stars = sum(1 for m in r.models if m.top_level_star)
        rendered = sum(1 for m in r.models if m.rendered)
        rendered_naive = sum(1 for m in r.models if m.rendered_naive)
        parsed = sum(1 for m in r.models if m.parsed)
        compiled = sum(1 for m in r.models if m.has_compiled_code)
        declared = sum(1 for m in r.models if m.declared_columns > 0)

        for m in r.models:
            if m.render_fail:
                render_fails[m.render_fail.split(":")[0] + (":" + m.render_fail.split(":")[1] if ":" in m.render_fail else "")] += 1
            if m.parse_fail:
                parse_fails[m.parse_fail] += 1

        agg["models"] += n
        agg["EXACT"] += c["EXACT"]
        agg["PARTIAL"] += c["PARTIAL"]
        agg["UNKNOWN"] += c["UNKNOWN"]
        agg["rendered"] += rendered
        agg["rendered_naive"] += rendered_naive
        agg["parsed"] += parsed
        agg["compiled"] += compiled
        agg["any_star"] += stars
        agg["top_level_star"] += tl_stars
        agg["declared_cols"] += declared

        pct = lambda x: round(100.0 * x / n, 1) if n else 0.0
        rows.append({
            "project": r.name,
            "kind": PROJECT_KIND.get(r.name, "?"),
            "adapter": r.adapter,
            "models": n,
            "compiled_code_%": pct(compiled),
            "rend_naive_%": pct(rendered_naive),
            "rendered_%": pct(rendered),
            "parsed_%": pct(parsed),
            "any_star_%": pct(stars),
            "top_star_%": pct(tl_stars),
            "declared_cols_%": pct(declared),
            "EXACT_%": pct(c["EXACT"]),
            "PARTIAL_%": pct(c["PARTIAL"]),
            "UNKNOWN_%": pct(c["UNKNOWN"]),
            "sources_with_cols": f"{r.n_sources_with_columns}/{r.n_sources}",
        })

    n = agg["models"] or 1
    overall = {k: round(100.0 * agg[k] / n, 1) for k in
               ("compiled", "rendered_naive", "rendered", "parsed", "any_star",
                "top_level_star", "declared_cols", "EXACT", "PARTIAL", "UNKNOWN")}
    overall["total_models"] = agg["models"]

    # unweighted mean across projects -- guards against one huge project
    ok = [r for r in rows if "EXACT_%" in r]
    if ok:
        overall["EXACT_mean_per_project"] = round(sum(r["EXACT_%"] for r in ok) / len(ok), 1)
        overall["any_star_mean_per_project"] = round(sum(r["any_star_%"] for r in ok) / len(ok), 1)

    # THE decision-relevant cut: analytics projects are the target population.
    by_kind: dict[str, dict[str, Any]] = {}
    for kind in ("analytics", "package"):
        sel = [r for r in rows if r.get("kind") == kind and "EXACT_%" in r]
        if not sel:
            continue
        tot = sum(r["models"] for r in sel)
        wmean = lambda k: round(
            sum(r[k] * r["models"] for r in sel) / tot, 1
        ) if tot else 0.0
        by_kind[kind] = {
            "projects": len(sel),
            "models": tot,
            "EXACT_weighted_%": wmean("EXACT_%"),
            "EXACT_mean_per_project_%": round(
                sum(r["EXACT_%"] for r in sel) / len(sel), 1
            ),
            "rendered_weighted_%": wmean("rendered_%"),
            "per_project": {r["project"]: r["EXACT_%"] for r in sel},
        }

    return {
        "per_project": rows,
        "overall": overall,
        "by_kind": by_kind,
        "render_failure_reasons": render_fails.most_common(25),
        "parse_failure_reasons": parse_fails.most_common(25),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects-root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dbt", required=True)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    root = Path(args.projects_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    workdir = out / "_work"

    targets: list[tuple[str, Path]] = []
    for repo in sorted(root.iterdir()):
        if not repo.is_dir():
            continue
        best: tuple[int, Path] | None = None
        for pj in repo.rglob("dbt_project.yml"):
            # never analyse a vendored dependency as if it were the project,
            # and prefer the real project over any integration_tests fixture
            if "dbt_packages" in pj.parts:
                continue
            md = pj.parent / "models"
            if not md.exists():
                continue
            cnt = len(list(md.rglob("*.sql")))
            depth = len(pj.parent.relative_to(repo).parts)
            score = (0 if depth <= 1 else 1, -cnt)  # shallower first, then bigger
            if cnt >= 5 and (best is None or score < best[0]):
                best = (score, pj.parent)  # type: ignore[assignment]
        if best:
            targets.append((repo.name, best[1]))

    if args.only:
        wanted = set(args.only.split(","))
        targets = [t for t in targets if t[0] in wanted]

    print(f"probing {len(targets)} projects", flush=True)
    results: list[ProjectResult] = []
    for name, pdir in targets:
        print(f"  -> {name} ({pdir})", flush=True)
        try:
            results.append(
                probe_project(
                    name, pdir, Path(args.dbt), workdir, PROJECT_VARS.get(name, "")
                )
            )
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            r = ProjectResult(name=name, subdir=str(pdir))
            r.dbt_parse_error = "probe crashed"
            results.append(r)

    (out / "raw.json").write_text(
        json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8"
    )
    summary = summarise(results)
    (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print("\n=== PER PROJECT ===")
    for row in summary["per_project"]:
        print(json.dumps(row))
    print("\n=== OVERALL ===")
    print(json.dumps(summary["overall"], indent=2))
    print("\n=== BY PROJECT KIND (the decision-relevant cut) ===")
    print(json.dumps(summary["by_kind"], indent=2))
    print("\n=== RENDER FAILURES ===")
    for k, v in summary["render_failure_reasons"]:
        print(f"{v:6}  {k}")
    print("\n=== PARSE FAILURES ===")
    for k, v in summary["parse_failure_reasons"]:
        print(f"{v:6}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
