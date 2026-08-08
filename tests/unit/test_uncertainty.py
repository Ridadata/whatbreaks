from __future__ import annotations

import pytest

from whatbreaks.lineage import Confidence, Resolution, Uncertainty, UnknownReason, confidence_for


def test_confidence_is_ordered() -> None:
    assert Confidence.UNKNOWN < Confidence.LIKELY < Confidence.CONFIRMED


def test_weakest_input_wins() -> None:
    assert Confidence.weakest([Confidence.CONFIRMED, Confidence.LIKELY]) is Confidence.LIKELY
    assert Confidence.weakest([Confidence.LIKELY, Confidence.UNKNOWN]) is Confidence.UNKNOWN
    assert Confidence.weakest([Confidence.CONFIRMED]) is Confidence.CONFIRMED


def test_combining_evidence_never_strengthens_it() -> None:
    """More evidence must never raise confidence above its weakest part."""
    assert (
        Confidence.weakest([Confidence.LIKELY, Confidence.CONFIRMED, Confidence.CONFIRMED])
        is Confidence.LIKELY
    )


def test_weakest_of_nothing_is_unknown() -> None:
    assert Confidence.weakest([]) is Confidence.UNKNOWN


def test_resolution_weakest() -> None:
    assert Resolution.weakest([Resolution.EXACT, Resolution.PARTIAL]) is Resolution.PARTIAL
    assert Resolution.weakest([Resolution.PARTIAL, Resolution.UNKNOWN]) is Resolution.UNKNOWN
    assert Resolution.weakest([Resolution.EXACT, Resolution.EXACT]) is Resolution.EXACT


def test_only_unknown_is_unusable() -> None:
    assert Resolution.EXACT.is_usable
    assert Resolution.PARTIAL.is_usable
    assert not Resolution.UNKNOWN.is_usable


# ------------------------------------------------------- confidence_for
def test_exact_schema_from_dbt_compiled_sql_is_confirmed() -> None:
    assert (
        confidence_for(resolution=Resolution.EXACT, sql_was_compiled_by_dbt=True)
        is Confidence.CONFIRMED
    )


def test_rendered_sql_caps_confidence_at_likely() -> None:
    """Rendered SQL is our reconstruction, not dbt's output."""
    assert (
        confidence_for(resolution=Resolution.EXACT, sql_was_compiled_by_dbt=False)
        is Confidence.LIKELY
    )


def test_partial_schema_caps_confidence_at_likely() -> None:
    assert (
        confidence_for(resolution=Resolution.PARTIAL, sql_was_compiled_by_dbt=True)
        is Confidence.LIKELY
    )


def test_unknown_schema_yields_unknown_confidence() -> None:
    assert (
        confidence_for(resolution=Resolution.UNKNOWN, sql_was_compiled_by_dbt=True)
        is Confidence.UNKNOWN
    )


def test_heuristics_cap_confidence_however_good_the_inputs() -> None:
    """An inferred rename is never CONFIRMED, even from perfect evidence."""
    assert (
        confidence_for(
            resolution=Resolution.EXACT, sql_was_compiled_by_dbt=True, used_heuristic=True
        )
        is Confidence.LIKELY
    )


@pytest.mark.parametrize("resolution", list(Resolution))
def test_confidence_never_exceeds_what_resolution_allows(resolution: Resolution) -> None:
    ceiling = {
        Resolution.EXACT: Confidence.CONFIRMED,
        Resolution.PARTIAL: Confidence.LIKELY,
        Resolution.UNKNOWN: Confidence.UNKNOWN,
    }[resolution]
    for compiled in (True, False):
        for heuristic in (True, False):
            got = confidence_for(
                resolution=resolution,
                sql_was_compiled_by_dbt=compiled,
                used_heuristic=heuristic,
            )
            assert got <= ceiling


# ------------------------------------------------------- reasons
def test_catalog_fixable_reasons_are_marked_honestly() -> None:
    """This is what justifies calling catalog.json optional rather than required."""
    assert UnknownReason.SURVIVING_STAR.is_fixable_with_catalog
    assert UnknownReason.UPSTREAM_UNKNOWN.is_fixable_with_catalog
    # a warehouse-dependent macro is not fixable by any static tool
    assert not UnknownReason.NEEDS_WAREHOUSE.is_fixable_with_catalog
    assert not UnknownReason.UNPARSEABLE_JINJA.is_fixable_with_catalog


@pytest.mark.parametrize("reason", [r for r in UnknownReason if r is not UnknownReason.NONE])
def test_every_reason_explains_itself(reason: UnknownReason) -> None:
    assert reason.explanation


def test_uncertainty_constructors() -> None:
    assert Uncertainty.exact().is_exact
    assert Uncertainty.partial(UnknownReason.SURVIVING_STAR).resolution is Resolution.PARTIAL
    assert Uncertainty.unknown(UnknownReason.NO_SQL).resolution is Resolution.UNKNOWN
