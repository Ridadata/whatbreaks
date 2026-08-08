"""Turn a graph diff into findings.

Three rules ship first, chosen because they are the highest-frequency,
highest-damage changes AND the only ones detectable with near-certainty from
static analysis. Renames, type changes and expression changes are all inference
and wait for later releases.

  WB001  column removed
  WB002  model removed
  WB003  column added
  WB900  model could not be analysed

Severity and confidence stay orthogonal throughout, and one rule is worth
stating plainly because it is easy to get backwards:

    Absence of evidence is not evidence of absence.

A removed column with no downstream consumers is safe *only if we analysed the
whole project*. If coverage was partial, the consumer may simply be one of the
models we could not read, so the finding is POSSIBLY_BREAKING rather than SAFE.
Reporting it as safe would be exactly the overclaim this tool exists to avoid.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from whatbreaks.analysis import Analysis, CoverageReport
from whatbreaks.diff.graph_diff import GraphDiff
from whatbreaks.impact.blast_radius import BlastRadius, compute_blast_radius, uses_only_star
from whatbreaks.lineage.column_graph import ColumnRef
from whatbreaks.lineage.uncertainty import Confidence


class Severity(str, Enum):
    BREAKING = "breaking"
    POSSIBLY_BREAKING = "possibly_breaking"
    SAFE = "safe"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {
            Severity.BREAKING: 3,
            Severity.POSSIBLY_BREAKING: 2,
            Severity.SAFE: 1,
            Severity.INFO: 0,
        }[self]


# Frozen and all-defaults, so one shared instance is safe and avoids a
# function call in a dataclass default.
_NO_IMPACT = BlastRadius()

RULE_TITLES = {
    "WB001": "column removed",
    "WB002": "model removed",
    "WB003": "column added",
    "WB900": "model could not be analysed",
}


@dataclass(frozen=True, slots=True)
class Finding:
    rule: str
    severity: Severity
    confidence: Confidence
    summary: str
    node_name: str
    node_id: str = ""
    column: str | None = None
    detail: str = ""
    impact: BlastRadius = _NO_IMPACT

    @property
    def title(self) -> str:
        return RULE_TITLES.get(self.rule, self.rule)

    @property
    def sort_key(self) -> tuple[int, int, str, str, str]:
        # most severe first; within a severity, most confident first
        return (
            -self.severity.rank,
            -int(self.confidence),
            self.rule,
            self.node_name,
            self.column or "",
        )


@dataclass(frozen=True, slots=True)
class Findings:
    items: tuple[Finding, ...] = ()
    coverage: CoverageReport | None = None

    def by_severity(self, severity: Severity) -> tuple[Finding, ...]:
        return tuple(f for f in self.items if f.severity is severity)

    @property
    def breaking(self) -> tuple[Finding, ...]:
        return self.by_severity(Severity.BREAKING)

    @property
    def worst(self) -> Severity | None:
        if not self.items:
            return None
        return max((f.severity for f in self.items), key=lambda s: s.rank)


def classify(diff: GraphDiff, head: Analysis, base: Analysis | None = None) -> Findings:
    """Classify a diff.

    `base` is needed for blast radius and is not optional in practice. Once a
    column is gone from its parent in head, nothing resolves against it and the
    head graph shows no consumers at all - so head alone can never answer "what
    depended on this". Base says what depended on it; head says whether the
    author already updated those consumers.
    """
    coverage = head.coverage()
    complete = coverage.is_complete
    findings: list[Finding] = []

    findings.extend(_removed_models(diff, head, complete))
    findings.extend(_removed_columns(diff, head, base, complete))
    findings.extend(_added_columns(diff))
    findings.extend(_unanalysable(diff))

    findings.sort(key=lambda f: f.sort_key)
    return Findings(tuple(findings), coverage)


# ----------------------------------------------------------------- WB002
def _removed_models(diff: GraphDiff, head: Analysis, complete: bool) -> list[Finding]:
    out: list[Finding] = []
    for name in diff.removed_models:
        # Anything in head still referencing a model that no longer exists is
        # broken; dbt itself would fail to compile.
        dependents = sorted(
            node.name
            for node in head.manifest.nodes.values()
            if any(dep.endswith(f".{name}") for dep in node.depends_on)
        )
        if dependents:
            severity = Severity.BREAKING
            detail = "still referenced by: " + ", ".join(dependents[:10])
        elif complete:
            severity = Severity.SAFE
            detail = "nothing references it"
        else:
            severity = Severity.POSSIBLY_BREAKING
            detail = (
                "no references found, but the project was only partially "
                "analysed so a consumer may have been missed"
            )
        out.append(
            Finding(
                rule="WB002",
                severity=severity,
                # Unusually, this one CAN be confirmed: `depends_on` comes from
                # dbt's own resolution of ref(), not from our reconstruction of
                # the SQL, so a dangling reference is a fact rather than an
                # inference. dbt itself would refuse to compile.
                confidence=(
                    Confidence.CONFIRMED if severity is Severity.BREAKING else Confidence.LIKELY
                ),
                summary=f"model `{name}` was removed",
                node_name=name,
                detail=detail,
            )
        )
    return out


# ----------------------------------------------------------------- WB001
def _removed_columns(
    diff: GraphDiff, head: Analysis, base: Analysis | None, complete: bool
) -> list[Finding]:
    out: list[Finding] = []
    # Consumers are only visible in the base graph; see classify()'s docstring.
    source = base if base is not None else head
    for change in diff.changed:
        for column in change.removed_columns:
            ref = ColumnRef(change.node_id, column)
            impact = compute_blast_radius(source.graph, source.manifest, [ref])
            still_named = head.graph.mentions(ref)

            if impact.is_empty and not still_named:
                if complete:
                    severity = Severity.SAFE
                    detail = "no downstream consumer references it"
                else:
                    # Absence of evidence is not evidence of absence.
                    severity = Severity.POSSIBLY_BREAKING
                    detail = (
                        f"no consumer found, but only {coverage_pct(head)}% of models "
                        f"could be analysed - a consumer may be among the rest"
                    )
                confidence = Confidence.LIKELY
            elif still_named:
                # Something in head still names the column that no longer
                # exists. That is a break, and the strongest signal available.
                severity = Severity.BREAKING
                detail = _impact_detail(impact) if not impact.is_empty else ""
                detail = (
                    f"still referenced by: {', '.join(n.split('.')[-1] for n in still_named[:8])}"
                    + (f"; {detail}" if detail else "")
                )
                confidence = impact.confidence if not impact.is_empty else Confidence.LIKELY
            elif uses_only_star(source.graph, ref):
                # `select *` consumers never name the column, so the check
                # above cannot see them - and they do not error either, they
                # silently narrow. Different breakage, reported as such.
                severity = Severity.POSSIBLY_BREAKING
                detail = (
                    "consumers reach it only via SELECT *, so they will not error - "
                    "they will silently produce a narrower result"
                )
                confidence = impact.confidence
            else:
                # Consumers existed in base but none names it now: the change
                # updated them too.
                severity = Severity.SAFE
                detail = "downstream consumers were updated in the same change"
                confidence = Confidence.LIKELY

            out.append(
                Finding(
                    rule="WB001",
                    severity=severity,
                    confidence=confidence,
                    summary=f"column `{change.name}.{column}` was removed",
                    node_name=change.name,
                    node_id=change.node_id,
                    column=column,
                    detail=detail,
                    impact=impact,
                )
            )
    return out


def _impact_detail(impact: BlastRadius) -> str:
    bits = [f"breaks {impact.summary()}"]
    if impact.columns:
        shown = ", ".join(str(c) for c in impact.columns[:6])
        bits.append(f"downstream columns: {shown}")
    if impact.query_breaks:
        bits.append(
            "models that break without a schema change (filter/join only): "
            + ", ".join(impact.query_breaks[:6])
        )
    if impact.exposures:
        bits.append("exposures: " + ", ".join(impact.exposures[:6]))
    return "; ".join(bits)


# ----------------------------------------------------------------- WB003
def _added_columns(diff: GraphDiff) -> list[Finding]:
    return [
        Finding(
            rule="WB003",
            severity=Severity.SAFE,
            confidence=Confidence.LIKELY,
            summary=f"column `{change.name}.{column}` was added",
            node_name=change.name,
            node_id=change.node_id,
            column=column,
        )
        for change in diff.changed
        for column in change.added_columns
    ]


# ----------------------------------------------------------------- WB900
def _unanalysable(diff: GraphDiff) -> list[Finding]:
    """Models we could not compare. Reported, never silently dropped."""
    return [
        Finding(
            rule="WB900",
            severity=Severity.INFO,
            confidence=Confidence.UNKNOWN,
            summary=f"`{name}` could not be compared",
            node_name=name,
            detail=why,
        )
        for name, why in diff.incomparable
    ]


def coverage_pct(analysis: Analysis) -> float:
    return analysis.coverage().analysed_pct
