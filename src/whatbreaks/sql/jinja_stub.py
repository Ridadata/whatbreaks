"""A deliberately incomplete stand-in for dbt's Jinja context.

The governing rule, and the reason this module is careful rather than clever:

    An unresolvable macro must NEVER render to the empty string.

Rendering `{{ dbt_utils.star(ref('x')) }}` to `""` produces SQL that parses
cleanly and means something entirely different from what the author wrote. That
is worse than failing, because the failure is silent and downstream lineage
looks confident. Everything unknown here raises a *named* error instead, so the
model is reported UNPARSEABLE with a reason a human can act on.

Two categories are distinguished, because they have different remedies:

* `undefined_macro` - we do not know this name. Often fixable (load more macros,
  widen the stub).
* `introspective`   - the macro needs to query the warehouse (`run_query`,
  `adapter.get_relation`, `load_result`). Not fixable offline, by definition.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

import jinja2
from jinja2.sandbox import SandboxedEnvironment

from whatbreaks.sql.dialect import model_relation_key, relation_key, source_relation_key

# A hostile or generated template could emit unbounded output. Rendering happens
# on untrusted PR content, so the result is capped.
MAX_RENDERED_CHARS = 4 * 1024 * 1024
# `{% for i in range(10**9) %}` would otherwise hang a CI job.
MAX_RANGE = 100_000


class MacroResolutionError(Exception):
    """Base for "we could not resolve something the template asked for"."""

    kind = "unresolved"

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"{self.kind}:{name}")


class UndefinedMacroError(MacroResolutionError):
    """A name the template used that we have no definition for."""

    kind = "undefined_macro"


class IntrospectiveMacroError(MacroResolutionError):
    """Needs a live warehouse. Unresolvable offline by construction."""

    kind = "introspective"


class RenderTooLargeError(Exception):
    """Rendered output exceeded the cap."""


class StrictUndefined(jinja2.Undefined):
    """Every access to an unknown name is a named, fatal error.

    jinja2's default `Undefined` renders to `""`, and `ChainableUndefined`
    silently swallows attribute chains. Both are exactly wrong here.
    """

    __slots__ = ()

    def _fail(self, *args: Any, **kwargs: Any) -> Any:
        raise UndefinedMacroError(self._undefined_name or "<unknown>")

    __call__ = _fail
    __getattr__ = _fail
    __getitem__ = _fail
    __str__ = _fail
    __repr__ = _fail
    __iter__ = _fail
    __len__ = _fail
    __bool__ = _fail
    __eq__ = _fail
    __ne__ = _fail
    __hash__ = _fail
    __add__ = _fail
    __contains__ = _fail


class Introspective:
    """Anything requiring warehouse access. Refuses loudly, with a precise name."""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise IntrospectiveMacroError(self._name)

    def __getattr__(self, item: str) -> Any:
        raise IntrospectiveMacroError(f"{self._name}.{item}")


class Relation(str):
    """A relation identifier that tolerates dbt's Relation API surface.

    Subclasses `str` so it interpolates into SQL as a plain identifier, while
    still answering `.identifier`, `.schema`, `.is_table` and friends that
    package macros routinely reach for.
    """

    __slots__ = ()

    def __getattr__(self, item: str) -> Any:
        if item in ("identifier", "name", "table", "schema", "database"):
            return str(self)
        if item in ("render", "quote", "include", "incorporate", "replace_path"):
            return lambda *a, **k: self
        if item.startswith("is_"):
            return False
        raise UndefinedMacroError(f"Relation.{item}")


class _RelationApi:
    @staticmethod
    def create(*args: Any, **kwargs: Any) -> Relation:
        parts = [str(a) for a in args if a]
        if not parts:
            parts = [
                str(kwargs.get(k)) for k in ("database", "schema", "identifier") if kwargs.get(k)
            ]
        return Relation(relation_key(*parts) if parts else relation_key("rel"))


class _ColumnApi:
    @staticmethod
    def create(*args: Any, **kwargs: Any) -> Relation:
        return Relation(relation_key(*[str(a) for a in args if a]))

    @staticmethod
    def translate_type(dtype: Any = "text", *args: Any, **kwargs: Any) -> str:
        return str(dtype)


class Api:
    """`{{ api.Relation.create(...) }}` - structural, so it is safe to model."""

    Relation = _RelationApi
    Column = _ColumnApi


class Target(dict):  # type: ignore[type-arg]
    """`target.name` / `target['name']` both work, as they do in dbt."""

    def __getattr__(self, item: str) -> Any:
        if item in self:
            return self[item]
        raise UndefinedMacroError(f"target.{item}")


class Config:
    """dbt's `config`, which is both callable and an object.

    `{{ config(materialized='view') }}` sets configuration and renders nothing,
    while `{% set k = config.get('unique_key') %}` reads it. Modelling this as a
    plain no-op function looks harmless but costs real coverage: `config.get`
    then resolves to an undefined named `get`, which is both a failure and an
    unhelpful diagnostic.

    Values come from the node's own manifest config, so reads are accurate
    rather than invented.
    """

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._values = values or {}

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return ""

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def require(self, key: str, *args: Any, **kwargs: Any) -> Any:
        if key in self._values:
            return self._values[key]
        raise UndefinedMacroError(f"config.require({key!r})")

    def set(self, key: str, value: Any) -> str:
        self._values[key] = value
        return ""


def _bounded_range(*args: int) -> range:
    r = range(*args)
    if len(r) > MAX_RANGE:
        raise RenderTooLargeError(f"range() of {len(r)} exceeds the {MAX_RANGE} cap")
    return r


def _pick_ref_name(args: tuple[Any, ...]) -> str:
    """dbt allows ref('m'), ref('pkg', 'm') and ref('m', version=2).

    The model name is always the last positional string.
    """
    names = [a for a in args if isinstance(a, str)]
    return names[-1] if names else "unknown"


def build_stub_context(
    *,
    node_name: str,
    adapter_type: str,
    project_name: str = "",
    project_vars: dict[str, Any] | None = None,
    node_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The dbt context needed to render a model body offline.

    Deliberately does NOT define package macros. Those come from
    `sql.macros.MacroRegistry`, compiled from the manifest, so that an
    unresolvable one is reported by name rather than guessed at.
    """

    def ref(*args: Any, **kwargs: Any) -> Relation:
        return Relation(model_relation_key(_pick_ref_name(args)))

    def source(*args: Any, **kwargs: Any) -> Relation:
        names = [a for a in args if isinstance(a, str)]
        if len(names) >= 2:
            return Relation(source_relation_key(names[0], names[1]))
        return Relation(source_relation_key("unknown", names[0] if names else "unknown"))

    def noop(*args: Any, **kwargs: Any) -> str:
        return ""

    def dbt_return(value: Any = None) -> Any:
        """dbt's `{{ return(x) }}`.

        Used pervasively inside package macros. Omitting it costs far more than
        its size suggests: without it, 96 of 164 models in the validation sample
        failed, and one project dropped from 91.7% to 0%.
        """
        return value

    known_vars = project_vars or {}

    def var(name: Any = None, default: Any = None, *args: Any, **kwargs: Any) -> Any:
        # Prefer the project's real value, read offline from dbt_project.yml.
        # Falling back to a caller default is fine; inventing a value is not,
        # so an unknown var with no default is reported rather than rendered
        # to "" -- that would silently change the SQL's meaning.
        if name in known_vars:
            return known_vars[name]
        if default is None:
            raise UndefinedMacroError(f"var({name!r}) has no default")
        return default

    def env_var(name: Any = None, default: Any = None, *args: Any, **kwargs: Any) -> Any:
        if default is None:
            raise UndefinedMacroError(f"env_var({name!r}) has no default")
        return default

    return {
        "ref": ref,
        "source": source,
        "this": Relation(model_relation_key(node_name)),
        "config": Config(dict(node_config or {})),
        "log": noop,
        "print": noop,
        "return": dbt_return,
        "var": var,
        "env_var": env_var,
        "target": Target(
            name="whatbreaks",
            schema="whatbreaks",
            database="whatbreaks",
            type=adapter_type or "duckdb",
            threads=1,
            profile_name=project_name,
        ),
        "model": {
            "name": node_name,
            "columns": {},
            "config": dict(node_config or {}),
            "alias": node_name,
        },
        "is_incremental": lambda: False,
        "should_full_refresh": lambda: False,
        "execute": False,
        "flags": {"FULL_REFRESH": False, "WHICH": "parse", "STORE_FAILURES": False},
        "invocation_id": "whatbreaks",
        "run_started_at": datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc),
        "modules": {"datetime": datetime, "re": re},
        "api": Api,
        "builtins": {},
        "selected_resources": [],
        "graph": {"nodes": {}, "sources": {}, "exposures": {}},
        "range": _bounded_range,
        # everything below needs a live warehouse
        "adapter": Introspective("adapter"),
        "run_query": Introspective("run_query"),
        "load_result": Introspective("load_result"),
        "statement": Introspective("statement"),
        "exceptions": Introspective("exceptions"),
    }


def build_environment() -> SandboxedEnvironment:
    """Sandboxed Jinja environment.

    Sandboxed because this renders SQL authored in a pull request. Note that
    running `dbt parse` to produce the manifest already executes the project's
    Jinja unsandboxed, so this adds no new execution surface - but it is not a
    reason to be careless.
    """
    return SandboxedEnvironment(
        undefined=StrictUndefined,
        extensions=["jinja2.ext.do", "jinja2.ext.loopcontrols"],
        keep_trailing_newline=True,
        autoescape=False,
    )
