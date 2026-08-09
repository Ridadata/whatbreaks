from __future__ import annotations

from whatbreaks.analysis import CoverageReport
from whatbreaks.diff.classify import Finding, Findings, Severity
from whatbreaks.impact.blast_radius import BlastRadius
from whatbreaks.lineage.column_graph import ColumnRef
from whatbreaks.lineage.uncertainty import Confidence
from whatbreaks.report.markdown import escape, render_markdown, short_ref


def coverage(*, total: int = 10, exact: int = 10, partial: int = 0, unknown: int = 0):
    return CoverageReport(
        total_models=total,
        exact=exact,
        partial=partial,
        unknown=unknown,
        reasons={},
        macro_stats={},
        unanalysable=(),
    )


def finding(**kw) -> Finding:
    base = {
        "rule": "WB001",
        "severity": Severity.BREAKING,
        "confidence": Confidence.LIKELY,
        "summary": "column up.a was removed",
        "node_name": "up",
        "column": "a",
    }
    base.update(kw)
    return Finding(**base)  # type: ignore[arg-type]


# ------------------------------------------------------------- security
def test_markdown_metacharacters_in_names_are_escaped() -> None:
    """Model names come from a pull request anyone may have authored."""
    out = render_markdown(
        Findings(
            (finding(node_name="</details><script>alert(1)</script>", column="x"),), coverage()
        )
    )
    assert "<script>" not in out
    assert "</details><script>" not in out


def test_at_mentions_cannot_notify_anyone() -> None:
    """A model named `@everyone` must not page a whole org from a PR comment."""
    out = render_markdown(Findings((finding(node_name="@everyone", column="x"),), coverage()))
    assert "@everyone" not in out
    assert "&#64;" in out


def test_backticks_in_names_cannot_break_out_of_code_spans() -> None:
    out = render_markdown(Findings((finding(node_name="a`b", column="c`d"),), coverage()))
    # the injected backticks are stripped, so the span cannot be escaped
    assert "a`b" not in out


def test_detail_text_is_escaped() -> None:
    out = render_markdown(
        Findings((finding(detail="see <img src=x onerror=alert(1)>"),), coverage())
    )
    assert "<img" not in out


def test_output_is_bounded_for_pathological_projects() -> None:
    """A generated project must not produce a comment that breaks the page."""
    many = tuple(finding(node_name=f"m{i}", column=f"c{i}") for i in range(500))
    out = render_markdown(Findings(many, coverage()))
    assert out.count("WB001") <= 60


# -------------------------------------------------------------- content
def test_breaking_changes_lead_and_are_not_collapsed() -> None:
    out = render_markdown(Findings((finding(),), coverage()))
    head = out.partition("<details>")[0]
    assert "🔴" in head
    assert "up.a" in head, "a breaking change must be visible without expanding anything"
    assert "Breaking" in head


def test_informational_findings_are_folded_away() -> None:
    """A reviewer's attention is the scarce resource."""
    out = render_markdown(
        Findings(
            (
                finding(),
                finding(rule="WB003", severity=Severity.SAFE, column="added_one"),
            ),
            coverage(),
        )
    )
    assert "<details>" in out
    breaking_section = out.split("<details>")[0]
    assert "added_one" not in breaking_section


def test_identifiers_render_as_code_not_escaped_punctuation() -> None:
    """Regression: escaping a pre-formatted summary produced `\\`up\\.a\\``."""
    out = render_markdown(Findings((finding(),), coverage()))
    assert "`up.a`" in out
    assert "\\`" not in out
    assert "up\\.a" not in out


def test_subject_is_not_duplicated_in_the_table() -> None:
    """Regression: "model `orders` — model could not be analysed"."""
    out = render_markdown(
        Findings(
            (finding(rule="WB900", severity=Severity.INFO, node_name="orders", column=None),),
            coverage(),
        )
    )
    assert "model `orders` could not be compared" in out
    assert "model `orders` — model" not in out


def test_downstream_columns_use_short_names() -> None:
    impact = BlastRadius(columns=(ColumnRef("model.pkg.orders", "status"),))
    out = render_markdown(Findings((finding(impact=impact),), coverage()))
    assert "`orders.status`" in out
    assert "model.pkg.orders.status" not in out


def test_short_ref_handles_unqualified_input() -> None:
    assert short_ref("orders.status") == "orders.status"
    assert short_ref("model.pkg.orders.status") == "orders.status"
    assert short_ref("plain") == "plain"


# ------------------------------------------------------------- coverage
def test_incomplete_coverage_warning_is_outside_the_fold() -> None:
    """A reader who never expands the section must still see the caveat."""
    out = render_markdown(Findings((), coverage(exact=8, partial=2)))
    warning_pos = out.index("could not be fully analysed")
    fold_pos = out.index("<details>")
    assert warning_pos < fold_pos


def test_incomplete_coverage_warns_about_further_impact() -> None:
    """Wording regression: "absence of findings is not proof of safety" reads as
    nonsense when findings are on screen. The caveat is that there may be MORE."""
    out = render_markdown(Findings((finding(),), coverage(exact=8, partial=2)))
    assert "further" in out


def test_complete_coverage_has_no_warning() -> None:
    out = render_markdown(Findings((), coverage()))
    assert "could not be fully analysed" not in out


def test_coverage_summary_counts_fully_resolved_not_analysed() -> None:
    """Regression: "100.0% analysed" printed next to "analysis was incomplete"."""
    out = render_markdown(Findings((), coverage(total=5, exact=4, partial=1)))
    assert "4/5 models fully resolved" in out
    assert "1 partial" in out


def test_clean_run_says_so_plainly() -> None:
    out = render_markdown(Findings((), coverage()))
    assert "no changes to column contracts" in out


def test_no_breaking_changes_headline_when_only_safe_findings() -> None:
    out = render_markdown(Findings((finding(rule="WB003", severity=Severity.SAFE),), coverage()))
    assert "no breaking changes" in out


def test_render_is_deterministic() -> None:
    findings = Findings((finding(), finding(node_name="z", column="q")), coverage())
    assert render_markdown(findings) == render_markdown(findings)


def test_escape_is_idempotent_on_plain_text() -> None:
    assert escape("plain text") == "plain text"
