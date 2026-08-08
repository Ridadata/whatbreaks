"""The confidence algebra. All of it, in one place, deliberately.

Every rule that needs to weaken a claim calls into this module. Scattering that
logic is how "never overclaim" quietly dies: one forgotten downgrade somewhere
and the tool starts reporting a guess as a fact.

Two orthogonal axes, and keeping them orthogonal matters:

* `Resolution` describes **a model's schema** - did we work out its output
  columns exactly, partially, or not at all?
* `Confidence` describes **a claim we make** - are we certain, is it a
  heuristic, or do we genuinely not know?

They are related but not the same. A model can have an EXACT schema while a
claim about it is only LIKELY, because the SQL it came from was our own
reconstruction rather than dbt's compiled output.

The combining rule everywhere is *the weakest input wins*. Evidence never
strengthens by being combined with more evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum, IntEnum


class Confidence(IntEnum):
    """How much we trust a claim. Ordered, so `min()` is the combining rule."""

    UNKNOWN = 0
    LIKELY = 1
    CONFIRMED = 2

    @classmethod
    def weakest(cls, values: Iterable[Confidence]) -> Confidence:
        """Combine evidence. Never returns more than its weakest input."""
        return min(values, default=cls.UNKNOWN)

    @property
    def label(self) -> str:
        return self.name.lower()


class Resolution(str, Enum):
    """How completely a model's output columns were determined."""

    EXACT = "exact"
    PARTIAL = "partial"
    UNKNOWN = "unknown"

    @property
    def is_usable(self) -> bool:
        """Can downstream inference build on this at all?"""
        return self is not Resolution.UNKNOWN

    @classmethod
    def weakest(cls, values: Iterable[Resolution]) -> Resolution:
        order = {cls.EXACT: 2, cls.PARTIAL: 1, cls.UNKNOWN: 0}
        worst = min((order[v] for v in values), default=0)
        return {2: cls.EXACT, 1: cls.PARTIAL, 0: cls.UNKNOWN}[worst]


class UnknownReason(str, Enum):
    """Why a schema is not EXACT. Drives the coverage report's per-model detail.

    These are not interchangeable: `SURVIVING_STAR` is fixable by supplying a
    catalog, while `NEEDS_WAREHOUSE` is not fixable by any static tool.
    """

    NONE = ""
    NO_SQL = "no_sql"
    PYTHON_MODEL = "python_model"
    UNPARSEABLE_JINJA = "unparseable_jinja"
    NEEDS_WAREHOUSE = "needs_warehouse"
    SQL_PARSE_ERROR = "sql_parse_error"
    QUALIFY_ERROR = "qualify_error"
    SURVIVING_STAR = "surviving_star"
    UPSTREAM_UNKNOWN = "upstream_unknown"
    NO_OUTPUT_COLUMNS = "no_output_columns"

    @property
    def is_fixable_with_catalog(self) -> bool:
        """Would `catalog.json` (i.e. a warehouse) resolve this?

        This is what justifies calling catalog.json an *optional accuracy
        upgrade* rather than a requirement, so it needs to be honest.
        """
        return self in (
            UnknownReason.SURVIVING_STAR,
            UnknownReason.UPSTREAM_UNKNOWN,
        )

    @property
    def explanation(self) -> str:
        return {
            UnknownReason.NONE: "",
            UnknownReason.NO_SQL: "has no SQL body",
            UnknownReason.PYTHON_MODEL: "is a Python model, which is out of scope",
            UnknownReason.UNPARSEABLE_JINJA: "its Jinja could not be rendered offline",
            UnknownReason.NEEDS_WAREHOUSE: "it needs a live warehouse to compile",
            UnknownReason.SQL_PARSE_ERROR: "its SQL could not be parsed",
            UnknownReason.QUALIFY_ERROR: "its columns could not be resolved",
            UnknownReason.SURVIVING_STAR: (
                "it selects * from something whose columns we do not know"
            ),
            UnknownReason.UPSTREAM_UNKNOWN: "an upstream model's columns are unknown",
            UnknownReason.NO_OUTPUT_COLUMNS: "it produced no resolvable output columns",
        }[self]


@dataclass(frozen=True, slots=True)
class Uncertainty:
    """A resolution paired with the reason it is not better than it is."""

    resolution: Resolution
    reason: UnknownReason = UnknownReason.NONE

    @classmethod
    def exact(cls) -> Uncertainty:
        return cls(Resolution.EXACT, UnknownReason.NONE)

    @classmethod
    def partial(cls, reason: UnknownReason) -> Uncertainty:
        return cls(Resolution.PARTIAL, reason)

    @classmethod
    def unknown(cls, reason: UnknownReason) -> Uncertainty:
        return cls(Resolution.UNKNOWN, reason)

    @property
    def is_exact(self) -> bool:
        return self.resolution is Resolution.EXACT


def confidence_for(
    *,
    resolution: Resolution,
    sql_was_compiled_by_dbt: bool,
    used_heuristic: bool = False,
) -> Confidence:
    """The single mapping from evidence quality to a claim's confidence.

    Three independent things can weaken a claim, and the weakest wins:

    1. **Schema resolution.** A PARTIAL schema cannot support a CONFIRMED claim,
       because the column we are reasoning about might be one we never resolved.
    2. **Where the SQL came from.** Rendered SQL is our reconstruction of what
       dbt would produce. It is good enough to analyse, but a claim built on it
       is not the same as one built on dbt's own compiled output.
    3. **Whether a heuristic was involved.** Inferred renames and similar
       guesses are capped at LIKELY however good the inputs were.
    """
    candidates = [
        {
            Resolution.EXACT: Confidence.CONFIRMED,
            Resolution.PARTIAL: Confidence.LIKELY,
            Resolution.UNKNOWN: Confidence.UNKNOWN,
        }[resolution],
        Confidence.CONFIRMED if sql_was_compiled_by_dbt else Confidence.LIKELY,
    ]
    if used_heuristic:
        candidates.append(Confidence.LIKELY)
    return Confidence.weakest(candidates)
