"""Compile the macros a manifest already contains.

This module is the reason whatbreaks is viable at all. Phase 0 measured it
(ADR 000 F6): rendering model bodies against a hand-written stub alone resolves
**34%** of models; compiling the macros out of `manifest.json` first resolves
**80%**. Package macros do not need to be faked, because dbt already wrote their
full source into the artifact we are reading.

Two non-obvious things are load-bearing, both learned the hard way:

1. **`manifest.macros` is not all macros.** It also holds `{% materialization %}`,
   `{% test %}` and `{% snapshot %}` blocks - dbt Jinja extensions that plain
   Jinja2 cannot parse. A single one raises `TemplateSyntaxError` for the whole
   bulk compile, silently costing every macro in that package.

2. **Macros must be compiled twice.** A macro compiled against a context that
   does not yet contain the other packages' namespaces sees `dbt_utils` as
   undefined *from inside its own body*, even though models can see it fine.
   Pass 1 discovers what exists; pass 2 recompiles against a context containing
   pass 1's output so macros can call each other across packages.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from jinja2.sandbox import SandboxedEnvironment

from whatbreaks.manifest.models import Macro, Manifest
from whatbreaks.sql.jinja_stub import UndefinedMacroError

# Dispatch resolution order is adapter-specific then default, mirroring dbt.
_DISPATCH_SUFFIX = "__{name}"


class MacroNamespace:
    """Attribute access for one package: `dbt_utils.star(...)`."""

    __slots__ = ("_macros", "_package")

    def __init__(self, package: str, macros: dict[str, Any]) -> None:
        self._package = package
        self._macros = macros

    def __getattr__(self, item: str) -> Any:
        try:
            return self._macros[item]
        except KeyError:
            raise UndefinedMacroError(f"{self._package}.{item}") from None

    def __getitem__(self, item: str) -> Any:
        return self.__getattr__(item)

    def __contains__(self, item: str) -> bool:
        return item in self._macros


class MacroRegistry:
    """Every callable macro in a manifest, ready to drop into a render context.

    Build once per manifest - compiling ~1,700 macros twice is not something to
    repeat per model.
    """

    __slots__ = ("_by_package", "_flat", "_stats")

    def __init__(
        self,
        flat: dict[str, Any],
        by_package: dict[str, dict[str, Any]],
        stats: dict[str, int],
    ) -> None:
        self._flat = flat
        self._by_package = by_package
        self._stats = stats

    @property
    def stats(self) -> dict[str, int]:
        """Counts for the coverage report: total, compiled, skipped, failed."""
        return dict(self._stats)

    def as_context(self) -> dict[str, Any]:
        """Macro names for a render context: bare names plus package namespaces."""
        ctx: dict[str, Any] = dict(self._flat)
        for package, macros in self._by_package.items():
            ctx[package] = MacroNamespace(package, macros)
        return ctx

    @classmethod
    def build(
        cls,
        manifest: Manifest,
        base_context: dict[str, Any],
        env: SandboxedEnvironment,
    ) -> MacroRegistry:
        plain = manifest.plain_macros()
        by_pkg: dict[str, list[Macro]] = defaultdict(list)
        for macro in plain:
            by_pkg[macro.package_name or "?"].append(macro)

        stats = {
            "total": len(manifest.macros),
            "plain": len(plain),
            "skipped_non_macro": len(manifest.macros) - len(plain),
            "compiled": 0,
            "failed": 0,
        }

        flat: dict[str, Any] = {}
        namespaces: dict[str, dict[str, Any]] = {}

        target = base_context.get("target") or {}
        adapter_type = str(target.get("type", "") or "")

        def dispatch(name: str, *args: Any, **kwargs: Any) -> Any:
            """Stand in for `adapter.dispatch`, mirroring dbt's resolution order."""
            candidates = [f"{adapter_type}__{name}"] if adapter_type else []
            candidates += [f"default__{name}", name]
            for candidate in candidates:
                if candidate in flat:
                    return flat[candidate]
            raise UndefinedMacroError(f"dispatch({name})")

        class _DispatchingAdapter:
            def __getattr__(self, item: str) -> Any:
                if item == "dispatch":
                    return dispatch
                # everything else on `adapter` needs a live connection
                return getattr(base_context["adapter"], item)

        def compile_pass(globals_: dict[str, Any]) -> None:
            flat.clear()
            namespaces.clear()
            compiled = failed = 0
            for package, macros in by_pkg.items():
                exported = cls._compile_package(macros, globals_, env)
                if exported is None:
                    failed += len(macros)
                    continue
                bucket = namespaces.setdefault(package, {})
                for name, fn in exported.items():
                    flat.setdefault(name, fn)
                    bucket[name] = fn
                compiled += len(exported)
                failed += len(macros) - len(exported)
            stats["compiled"] = compiled
            stats["failed"] = failed

        seed = dict(base_context)
        seed["adapter"] = _DispatchingAdapter()

        # pass 1: discover
        compile_pass(seed)
        # pass 2: recompile so macros can call each other across packages
        second = dict(seed)
        second.update(flat)
        for package, macros_ in namespaces.items():
            second[package] = MacroNamespace(package, macros_)
        compile_pass(second)

        return cls(dict(flat), {k: dict(v) for k, v in namespaces.items()}, stats)

    @staticmethod
    def _compile_package(
        macros: list[Macro],
        globals_: dict[str, Any],
        env: SandboxedEnvironment,
    ) -> dict[str, Any] | None:
        """Compile one package, falling back to per-macro on failure.

        Bulk compilation is ~100x faster, but one malformed macro takes the
        whole package with it, so a failure retries individually rather than
        writing off hundreds of usable macros.
        """
        try:
            module = env.from_string("\n".join(m.macro_sql for m in macros)).make_module(
                dict(globals_)
            )
        except Exception:
            out: dict[str, Any] = {}
            for macro in macros:
                try:
                    single = env.from_string(macro.macro_sql).make_module(dict(globals_))
                except Exception:
                    continue
                fn = getattr(single, macro.name, None)
                if callable(fn):
                    out[macro.name] = fn
            return out

        exported: dict[str, Any] = {}
        for macro in macros:
            fn = getattr(module, macro.name, None)
            if callable(fn):
                exported[macro.name] = fn
        return exported
