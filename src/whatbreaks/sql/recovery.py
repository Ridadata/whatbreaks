"""Recover analysable SQL for every model in a manifest.

Three sources, best first:

1. `manifest.nodes[*].compiled_code` - present only if the manifest came from
   `dbt compile`/`dbt build`. Phase 0 measured this at **0%** for `dbt parse`
   output, so it is a bonus, never the plan.
2. `target/compiled/**` on disk - same fidelity, same precondition.
3. Offline Jinja rendering against compiled manifest macros - the actual path
   for almost everyone.

The failure taxonomy matters as much as the success path. "parse error" tells a
user nothing; `introspective:run_query` tells them their model needs a warehouse
and no tool of this kind can ever analyse it, while
`undefined_macro:dbt_utils.star` tells them something is missing that could be
fixed. Coverage reporting depends on this distinction being precise.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from whatbreaks.manifest.models import Manifest, Node
from whatbreaks.project import read_project_vars
from whatbreaks.sql.jinja_stub import (
    MAX_RENDERED_CHARS,
    IntrospectiveMacroError,
    MacroResolutionError,
    RenderTooLargeError,
    UndefinedMacroError,
    build_environment,
    build_stub_context,
)
from whatbreaks.sql.macros import MacroRegistry


class SqlSource(str, Enum):
    """Where the SQL came from. Feeds directly into finding confidence."""

    MANIFEST_COMPILED = "manifest_compiled"
    DISK_COMPILED = "disk_compiled"
    RENDERED = "rendered"


class FailureKind(str, Enum):
    NO_SQL = "no_sql"
    UNDEFINED_MACRO = "undefined_macro"
    INTROSPECTIVE = "introspective"
    JINJA_SYNTAX = "jinja_syntax"
    JINJA_RUNTIME = "jinja_runtime"
    TOO_LARGE = "too_large"
    RECURSION = "recursion"

    @property
    def is_fixable(self) -> bool:
        """Could better inputs plausibly resolve this?

        `introspective` never can - the model genuinely needs a warehouse. The
        distinction drives what we tell the user to do about it.
        """
        return self is not FailureKind.INTROSPECTIVE


@dataclass(frozen=True, slots=True)
class RecoveryFailure:
    kind: FailureKind
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.kind.value}:{self.detail}" if self.detail else self.kind.value

    @property
    def explanation(self) -> str:
        """A sentence a human can act on."""
        if self.kind is FailureKind.INTROSPECTIVE:
            return (
                f"needs a live warehouse ({self.detail}); it cannot be analysed "
                f"statically by any tool"
            )
        if self.kind is FailureKind.UNDEFINED_MACRO:
            return f"uses a macro we could not resolve ({self.detail})"
        if self.kind is FailureKind.JINJA_SYNTAX:
            return f"has a Jinja syntax error ({self.detail})"
        if self.kind is FailureKind.TOO_LARGE:
            return "rendered to more output than the safety cap allows"
        if self.kind is FailureKind.RECURSION:
            return "hit infinite recursion while rendering"
        if self.kind is FailureKind.NO_SQL:
            return "has no SQL body"
        return f"failed to render ({self.detail})"


@dataclass(frozen=True, slots=True)
class RecoveredSql:
    node_id: str
    sql: str | None = None
    source: SqlSource | None = None
    failure: RecoveryFailure | None = None

    @property
    def ok(self) -> bool:
        return self.sql is not None

    @property
    def is_high_fidelity(self) -> bool:
        """True when the SQL came from dbt itself rather than our rendering.

        Rendered SQL is good enough to analyse but is our reconstruction, so
        findings derived from it never claim the top confidence tier.
        """
        return self.source in (SqlSource.MANIFEST_COMPILED, SqlSource.DISK_COMPILED)


class SqlRecovery:
    """Recovers SQL for a manifest's models. Build once, reuse across models."""

    def __init__(self, manifest: Manifest, project_root: Path | None = None) -> None:
        self._manifest = manifest
        self._project_root = project_root
        self._env = build_environment()
        # dbt resolves var() at compile time, so the manifest does not carry
        # project vars. Reading them offline is what makes var() accurate
        # instead of a guess.
        self._project_vars = read_project_vars(project_root)
        # One shared registry: compiling ~1,700 macros twice is not per-model work.
        self._registry = MacroRegistry.build(
            manifest,
            self._context_for(node_name="__whatbreaks__", node_config={}),
            self._env,
        )
        self._macro_context = self._registry.as_context()

    def _context_for(self, node_name: str, node_config: dict[str, object]) -> dict[str, object]:
        return build_stub_context(
            node_name=node_name,
            adapter_type=self._manifest.adapter_type,
            project_name=self._manifest.project_name,
            project_vars=self._project_vars,
            node_config=node_config,
        )

    @property
    def manifest(self) -> Manifest:
        return self._manifest

    @property
    def macro_stats(self) -> dict[str, int]:
        return self._registry.stats

    def recover(self, node: Node) -> RecoveredSql:
        if node.compiled_code:
            return RecoveredSql(node.unique_id, node.compiled_code, SqlSource.MANIFEST_COMPILED)

        on_disk = self._read_compiled_from_disk(node)
        if on_disk:
            return RecoveredSql(node.unique_id, on_disk, SqlSource.DISK_COMPILED)

        return self._render(node)

    def recover_all(self) -> dict[str, RecoveredSql]:
        return {uid: self.recover(node) for uid, node in self._manifest.models.items()}

    # ----------------------------------------------------------------
    def _read_compiled_from_disk(self, node: Node) -> str | None:
        if self._project_root is None or not node.original_file_path:
            return None
        base = self._project_root / "target" / "compiled"
        if not base.is_dir():
            return None
        candidate = base / node.package_name / node.original_file_path
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return None

    def _render(self, node: Node) -> RecoveredSql:
        raw = node.raw_code
        if not raw.strip():
            return RecoveredSql(node.unique_id, failure=RecoveryFailure(FailureKind.NO_SQL))

        context = dict(self._macro_context)
        # Node-specific values must win over anything the registry seeded, or
        # every model would render with the placeholder node's ref()/this.
        context.update(
            self._context_for(
                node_name=node.name,
                node_config={
                    "materialized": node.materialized,
                    "unique_key": node.unique_key,
                },
            )
        )

        try:
            rendered = self._env.from_string(raw).render(context)
        except IntrospectiveMacroError as exc:
            return RecoveredSql(
                node.unique_id,
                failure=RecoveryFailure(FailureKind.INTROSPECTIVE, exc.name),
            )
        except UndefinedMacroError as exc:
            return RecoveredSql(
                node.unique_id,
                failure=RecoveryFailure(FailureKind.UNDEFINED_MACRO, exc.name),
            )
        except MacroResolutionError as exc:  # future subclasses
            return RecoveredSql(
                node.unique_id,
                failure=RecoveryFailure(FailureKind.UNDEFINED_MACRO, exc.name),
            )
        except RenderTooLargeError as exc:
            return RecoveredSql(
                node.unique_id, failure=RecoveryFailure(FailureKind.TOO_LARGE, str(exc))
            )
        except RecursionError:
            return RecoveredSql(node.unique_id, failure=RecoveryFailure(FailureKind.RECURSION))
        except Exception as exc:
            kind = (
                FailureKind.JINJA_SYNTAX
                if type(exc).__name__ == "TemplateSyntaxError"
                else FailureKind.JINJA_RUNTIME
            )
            return RecoveredSql(node.unique_id, failure=RecoveryFailure(kind, type(exc).__name__))

        if len(rendered) > MAX_RENDERED_CHARS:
            return RecoveredSql(
                node.unique_id,
                failure=RecoveryFailure(FailureKind.TOO_LARGE, f"{len(rendered)} chars"),
            )
        if not rendered.strip():
            # Rendering to nothing is the silent-wrongness case this tool exists
            # to avoid. Treat it as a failure, never as an empty model.
            return RecoveredSql(
                node.unique_id,
                failure=RecoveryFailure(FailureKind.NO_SQL, "rendered to empty"),
            )
        return RecoveredSql(node.unique_id, rendered, SqlSource.RENDERED)
