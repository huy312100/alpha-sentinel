"""The coverage report's own arithmetic.

The report exists to be acted on - "crypto technicals returned nothing" is the kind of
line that ends up justifying a code change - so the counting and the classification are
tested without a vendor. Only the pure half is testable offline; the network half is
``ScanPipeline.collect_many``, which has its own tests.
"""

from __future__ import annotations

from core.models import Layer, LayerSignal, SignalStatus
from scripts.coverage_probe import STATUS_COLUMNS, _reasons, denominator_note, format_table, tally


def _signal(layer: Layer, status: SignalStatus, *, summary: str = "", error: str = "") -> LayerSignal:
    return LayerSignal(
        layer=layer,
        score=50.0,
        confidence=0.0,
        weight=1.0,
        status=status,
        summary=summary,
        error=error,
    )


BATCH = [
    _signal(Layer.DEX, SignalStatus.OK),
    _signal(Layer.DEX, SignalStatus.OK),
    _signal(Layer.TECHNICALS, SignalStatus.DEGRADED),
    _signal(
        Layer.TECHNICALS,
        SignalStatus.UNAVAILABLE,
        error="layer timed out after 20s",
        summary="technicals unavailable: layer timed out after 20s",
    ),
    _signal(Layer.SOCIAL, SignalStatus.SKIPPED, summary="social skipped: no social provider configured"),
]


def test_tally_separates_layer_from_status():
    counts = tally(BATCH)

    assert counts[("dex", "ok")] == 2
    assert counts[("technicals", "degraded")] == 1
    assert counts[("technicals", "unavailable")] == 1
    assert counts[("social", "skipped")] == 1


def test_tally_of_nothing_is_empty_rather_than_zeroed_rows():
    # A batch that collected nothing must not print as "every layer returned 0":
    # that is the shape a report lies with.
    assert not tally([])


def test_reasons_gives_one_line_per_losing_layer_and_never_a_winning_one():
    lines = _reasons(BATCH)

    layers = [line.split(" [", 1)[0] for line in lines]
    assert layers == ["social", "technicals"], lines
    assert any("timed out" in line for line in lines), lines
    assert all(not line.startswith("dex") for line in lines), lines


def test_table_columns_lead_with_ok_and_count_rows():
    table = format_table(tally(BATCH), rows=len(BATCH))
    header = table.splitlines()[0]

    assert header.index("ok") < header.index("degraded") < header.index("unavailable")
    assert tuple(STATUS_COLUMNS) == ("ok", "degraded", "unavailable", "skipped")
    assert "signals tallied: 5" in table
    # "layers with usable data" counts layers with at least one ok, so technicals
    # (degraded + unavailable) and social (skipped) must not be reported as covered.
    assert "layers with any usable data: 1 of 3" in table


def test_denominator_states_what_the_matrix_is_a_fraction_of():
    # collect_many drops an instrument whose collection fails. The counts below stay
    # true while describing a smaller batch than was asked for, so the shortfall has
    # to be printed rather than inferred.
    assert denominator_note(24, 24) == ["  instruments collected: 24 of 24"]
    short = denominator_note(11, 24)
    assert "11 of 24" in short[0]
    assert "WARNING" in short[1] and "13" in short[1]


def test_an_empty_batch_is_reported_as_a_failure_not_as_zero_coverage():
    lines = denominator_note(0, 24)
    assert any("failed run" in line for line in lines), lines
