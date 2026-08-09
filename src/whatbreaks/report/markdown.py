"""Markdown rendering, shaped for a pull-request comment.

Two constraints drive every decision here.

**Untrusted input.** Model and column names come from a pull request that anyone
may have authored. They are escaped, never interpolated raw: a model named
`</details><script>` or `@everyone` must render as text, not as markup or a
notification storm. Length is capped for the same reason - a generated project
with 10,000 findings must not produce a comment that breaks the page.

**A reviewer's attention is the scarce resource.** Breaking changes go at the
top, uncollapsed. Everything else is folded away. A comment that buries one real
break under two hundred informational lines has failed even though every line in
it is true.
"""

from __future__ import annotations

import re

from whatbreaks.analysis import CoverageReport
from whatbreaks.diff.classify import Finding, Findings, Severity

MAX_FINDINGS_RENDERED = 50
MAX_IMPACT_ITEMS = 8
MAX_CELL_CHARS = 120

_SEVERITY_ICON = {
    Severity.BREAKING: "🔴",
    Severity.POSSIBLY_BREAKING: "🟡",
    Severity.SAFE: "🟢",
    Severity.INFO: "ℹ️",
}

# The verb phrase for each rule. `_subject` supplies the noun, so these must
# not repeat it - "model `orders` — model could not be analysed" reads badly.
_ACTION = {
    "WB001": "was removed",
    "WB002": "was removed",
    "WB003": "was added",
    "WB900": "could not be compared",
}

# Markdown metacharacters. Angle brackets are handled separately, by entity
# encoding: backslash-escaping `<` still leaves the literal substring `<img`
# in the output, which is not neutralisation, it only looks like it.
_ESCAPE = re.compile(r"([\\`*_{}\[\]()#+\-.!|~])")
_MENTION = re.compile(r"[@]")


def _neutralise_html(text: str) -> str:
    """Entity-encode the characters that could start raw HTML.

    GitHub sanitises rendered HTML, but this output is also written to files,
    posted through other renderers, and read in terminals. Relying on the host
    to clean up after us is not a security model.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def escape(text: str) -> str:
    """Render arbitrary text as literal, inert markdown.

    Applied to everything that came from the analysed project. Model and column
    names are attacker-controlled in a fork pull request; being thorough here is
    cheap, and being wrong once means a comment that executes someone else's
    markup or pages an entire organisation.
    """
    if not text:
        return ""
    escaped = _neutralise_html(text)
    escaped = _ESCAPE.sub(r"\\\1", escaped)
    escaped = _MENTION.sub("&#64;", escaped)
    if len(escaped) > MAX_CELL_CHARS:
        escaped = escaped[:MAX_CELL_CHARS] + "…"
    return escaped


def code(text: str) -> str:
    """Wrap in backticks as an inert code span.

    A code span suppresses mentions and HTML on GitHub, but this does not rely
    on that: backticks are stripped so the span cannot be closed early, and
    angle brackets and `@` are neutralised anyway. Defence in depth is one line
    here and the failure mode is somebody else's markup in your PR.
    """
    if not text:
        return ""
    inner = text.replace("`", "")
    inner = _neutralise_html(inner)
    inner = _MENTION.sub("&#64;", inner)
    return f"`{inner[:MAX_CELL_CHARS]}`"


def short_ref(text: str) -> str:
    """`model.jaffle_shop.orders.status` -> `orders.status`.

    dbt unique_ids carry a resource type and package prefix that a reviewer
    already knows and did not ask for. In a PR comment the noise crowds out the
    part that matters.
    """
    parts = text.split(".")
    return ".".join(parts[-2:]) if len(parts) > 2 else text


def render_markdown(findings: Findings, *, title: str = "whatbreaks") -> str:
    lines: list[str] = []
    breaking = findings.by_severity(Severity.BREAKING)
    possibly = findings.by_severity(Severity.POSSIBLY_BREAKING)
    other = [f for f in findings.items if f.severity in (Severity.SAFE, Severity.INFO)]

    lines.append(_headline(title, findings))
    lines.append("")

    if breaking:
        lines.append("### 🔴 Breaking")
        lines.append("")
        lines.extend(_finding_block(f) for f in breaking[:MAX_FINDINGS_RENDERED])
        lines.append("")

    if possibly:
        lines.append("### 🟡 Possibly breaking")
        lines.append("")
        lines.extend(_finding_block(f) for f in possibly[:MAX_FINDINGS_RENDERED])
        lines.append("")

    if other:
        lines.append(_collapsed(f"Other changes ({len(other)})", _table(other)))
        lines.append("")

    if findings.coverage is not None:
        lines.append(_coverage_block(findings.coverage))

    lines.append("")
    lines.append(
        "<sub>Reported by "
        "[whatbreaks](https://github.com/Ridadata/whatbreaks) — static analysis, "
        "no warehouse queried.</sub>"
    )
    return "\n".join(lines).rstrip() + "\n"


def _headline(title: str, findings: Findings) -> str:
    breaking = len(findings.by_severity(Severity.BREAKING))
    possibly = len(findings.by_severity(Severity.POSSIBLY_BREAKING))
    if breaking:
        return f"## 🔴 {escape(title)}: {breaking} breaking change{_s(breaking)}"
    if possibly:
        return f"## 🟡 {escape(title)}: {possibly} possible break{'s' if possibly != 1 else ''}"
    if findings.items:
        return f"## 🟢 {escape(title)}: no breaking changes"
    return f"## 🟢 {escape(title)}: no changes to column contracts"


def _s(count: int) -> str:
    return "s" if count != 1 else ""


def _subject(finding: Finding) -> str:
    """Format the thing that changed from STRUCTURED fields.

    Composing here rather than escaping a pre-formatted summary is what keeps
    `orders.status` rendering as code instead of `\\`orders\\.status\\``: the
    renderer knows which parts are identifiers, so it can quote them and escape
    everything else.
    """
    if finding.column:
        return f"column {code(f'{finding.node_name}.{finding.column}')}"
    return f"model {code(finding.node_name)}"


def _finding_block(finding: Finding) -> str:
    icon = _SEVERITY_ICON[finding.severity]
    action = _ACTION.get(finding.rule, escape(finding.summary))
    parts = [f"{icon} **{_subject(finding)}** {action}"]
    if finding.detail:
        parts.append(f"  {escape(finding.detail)}")

    impact = finding.impact
    if impact.columns:
        shown = ", ".join(code(short_ref(str(c))) for c in impact.columns[:MAX_IMPACT_ITEMS])
        extra = len(impact.columns) - MAX_IMPACT_ITEMS
        parts.append(f"  Downstream columns: {shown}" + (f" and {extra} more" if extra > 0 else ""))
    if impact.query_breaks:
        shown = ", ".join(code(m.split(".")[-1]) for m in impact.query_breaks[:MAX_IMPACT_ITEMS])
        parts.append(f"  Breaks without a schema change (filter/join only): {shown}")
    if impact.tests:
        shown = ", ".join(code(t) for t in impact.tests[:MAX_IMPACT_ITEMS])
        parts.append(f"  Failing tests: {shown}")
    if impact.exposures:
        shown = ", ".join(code(e) for e in impact.exposures[:MAX_IMPACT_ITEMS])
        parts.append(f"  Exposures: {shown}")

    parts.append(f"  <sub>{finding.rule} · confidence: {finding.confidence.label}</sub>")
    return "\n".join(parts) + "\n"


def _table(items: list[Finding]) -> str:
    rows = [
        "| | Rule | Change | Note |",
        "|---|---|---|---|",
    ]
    for finding in items[:MAX_FINDINGS_RENDERED]:
        rows.append(
            f"| {_SEVERITY_ICON[finding.severity]} "
            f"| {finding.rule} "
            f"| {_subject(finding)} {escape(_ACTION.get(finding.rule, finding.title))} "
            f"| {escape(finding.detail)} |"
        )
    if len(items) > MAX_FINDINGS_RENDERED:
        rows.append(f"| | | …and {len(items) - MAX_FINDINGS_RENDERED} more | |")
    return "\n".join(rows)


def _coverage_block(coverage: CoverageReport) -> str:
    """Coverage is never omitted and never collapsed away when incomplete.

    "No breaking changes found" without saying how much was analysed is a lie by
    omission, and it is the specific failure this project exists to avoid.
    """
    body = [
        f"Analysed **{coverage.analysed}/{coverage.total_models}** models "
        f"({coverage.analysed_pct}%) — {coverage.exact} exact, "
        f"{coverage.partial} partial, {coverage.unknown} not analysed.",
        "",
    ]
    if coverage.reasons:
        body.append("| Reason | Models |")
        body.append("|---|---:|")
        for reason, count in coverage.reasons.items():
            body.append(f"| {escape(reason)} | {count} |")
        body.append("")
    if coverage.catalog_would_help:
        body.append(
            f"{coverage.catalog_would_help} of these would resolve with a "
            "`catalog.json` (`dbt docs generate`)."
        )
        body.append("")
    if coverage.unanalysable:
        body.append("Not analysed:")
        for name, why in coverage.unanalysable[:20]:
            body.append(f"- {code(name)} — {escape(why)}")
        if len(coverage.unanalysable) > 20:
            body.append(f"- …and {len(coverage.unanalysable) - 20} more")

    # The fold's summary reports FULLY RESOLVED models, not "analysed".
    # `analysed_pct` counts partials, so a project with one partial model read
    # "Coverage: 100.0% analysed" directly above "analysis was incomplete" -
    # true on both counts and confusing on sight.
    fully = coverage.exact
    summary = f"Coverage: {fully}/{coverage.total_models} models fully resolved"
    if coverage.partial:
        summary += f", {coverage.partial} partial"
    if coverage.unknown:
        summary += f", {coverage.unknown} not analysed"
    detail = _collapsed(summary, "\n".join(body))
    if coverage.is_complete:
        return detail
    # Surfaced OUTSIDE the fold: a reader who never expands the section still
    # has to see that absence of findings is not proof of safety.
    return (
        "> ⚠️ Analysis was incomplete, so the absence of findings is not proof "
        "that nothing breaks.\n\n" + detail
    )


def _collapsed(summary: str, body: str) -> str:
    return f"<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>"
