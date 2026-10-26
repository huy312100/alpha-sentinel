"""SEC company facts: annual-only parsing, then a defensible quality score."""

from __future__ import annotations

import pytest

from core.instruments import AssetClass
from core.models import TokenSnapshot
from scanners.base import ScanContext
from scanners.shared.listed.fundamentals import (
    Fundamentals,
    FundamentalsScanner,
    parse_company_facts,
    parse_ticker_index,
    score_fundamentals,
)


def duration(start: str, end: str, value, *, form="10-K", fp="FY"):
    return {"start": start, "end": end, "val": value, "form": form, "fp": fp}


def instant(end: str, value, *, form="10-K", fp="FY"):
    return {"end": end, "val": value, "form": form, "fp": fp}


def facts(**concepts):
    units = {}
    for concept, (unit, rows) in concepts.items():
        units[concept] = {"units": {unit: rows}}
    return {"facts": {"us-gaap": units}}


def annual(start_year: int, end_year: int, values):
    rows = []
    for offset, value in enumerate(values):
        year = start_year + offset
        rows.append(duration(f"{year}-01-01", f"{year}-12-31", value))
    return rows


def sample_facts() -> dict:
    return facts(
        Revenues=("USD", annual(2021, 2024, [100e9, 120e9, 150e9, 200e9])),
        NetIncomeLoss=("USD", annual(2021, 2024, [10e9, 15e9, 25e9, 40e9])),
        StockholdersEquity=("USD", [instant("2024-12-31", 150e9), instant("2023-12-31", 110e9)]),
        Assets=("USD", [instant("2024-12-31", 300e9)]),
        Liabilities=("USD", [instant("2024-12-31", 150e9)]),
        NetCashProvidedByUsedInOperatingActivities=("USD", annual(2021, 2024, [20e9, 30e9, 40e9, 60e9])),
        PaymentsToAcquirePropertyPlantAndEquipment=("USD", annual(2021, 2024, [5e9, 6e9, 8e9, 10e9])),
        EarningsPerShareDiluted=("USD/shares", annual(2021, 2024, [1.0, 1.5, 2.0, 2.6])),
    )


class TestParsing:
    def test_latest_and_prior_year(self):
        parsed = parse_company_facts(sample_facts(), symbol="TEST")
        assert parsed.revenue == 200e9
        assert parsed.revenue_prior == 150e9
        assert parsed.revenue_growth_pct == pytest.approx(100 / 3)
        assert parsed.fiscal_year_end == "2024-12-31"
        assert parsed.net_margin_pct == pytest.approx(20.0)
        assert parsed.roe_pct == pytest.approx(40e9 / 150e9 * 100)
        assert parsed.debt_ratio == pytest.approx(0.5)
        assert parsed.free_cash_flow == pytest.approx(50e9)
        assert parsed.fcf_margin_pct == pytest.approx(25.0)
        assert parsed.eps_growth_pct == pytest.approx(30.0)

    def test_quarterly_rows_are_ignored(self):
        payload = facts(
            Revenues=(
                "USD",
                annual(2022, 2023, [100.0, 120.0])
                + [duration("2024-01-01", "2024-03-31", 40.0, form="10-Q", fp="Q1")],
            )
        )
        parsed = parse_company_facts(payload)
        assert parsed.revenue == 120.0
        assert parsed.fiscal_year_end == "2023-12-31"

    def test_restatement_wins_for_the_same_period(self):
        payload = facts(
            Revenues=(
                "USD",
                [
                    duration("2024-01-01", "2024-12-31", 100.0),
                    duration("2024-01-01", "2024-12-31", 90.0),
                ],
            )
        )
        assert parse_company_facts(payload).revenue == 90.0

    def test_revenue_concept_fallback(self):
        payload = facts(
            RevenueFromContractWithCustomerExcludingAssessedTax=("USD", annual(2023, 2024, [10.0, 12.0]))
        )
        parsed = parse_company_facts(payload)
        assert parsed.revenue == 12.0
        assert parsed.revenue_growth_pct == pytest.approx(20.0)

    def test_no_annual_facts_is_loud(self):
        with pytest.raises(ValueError):
            parse_company_facts({"facts": {"us-gaap": {}}})
        with pytest.raises(ValueError):
            parse_company_facts("not-a-mapping")

    def test_ticker_index(self):
        payload = {
            "0": {"cik_str": 320193, "ticker": "aapl", "title": "Apple Inc."},
            "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft"},
            "2": {"ticker": "BROKEN"},
            "3": "garbage",
        }
        index = parse_ticker_index(payload)
        assert index["AAPL"] == (320193, "Apple Inc.")
        assert "BROKEN" not in index
        assert len(index) == 2

    def test_ticker_index_rejects_bad_shapes(self):
        with pytest.raises(ValueError):
            parse_ticker_index([])


class TestScoring:
    def test_healthy_company_scores_high_without_flags(self):
        parsed = parse_company_facts(sample_facts(), symbol="TEST")
        score, flags, confidence, components = score_fundamentals(parsed)
        assert score >= 70.0
        assert flags == []
        assert confidence == 0.85
        assert "revenue_growth" in components
        assert "leverage" in components

    def test_deteriorating_company_is_flagged_and_penalised(self):
        parsed = Fundamentals(
            symbol="BAD",
            revenue=80.0,
            revenue_prior=100.0,
            net_income=-10.0,
            equity=-5.0,
            assets=100.0,
            liabilities=95.0,
            operating_cash_flow=-20.0,
            eps=-1.0,
            eps_prior=1.0,
        )
        score, flags, confidence, _ = score_fundamentals(parsed)
        assert score < 30.0
        assert {
            "revenue_declining",
            "unprofitable",
            "negative_equity",
            "high_leverage",
            "cash_burn",
            "eps_declining",
        }.issubset(set(flags))
        assert confidence >= 0.65

    def test_without_usable_metrics_it_refuses_to_guess(self):
        score, flags, confidence, components = score_fundamentals(Fundamentals(symbol="EMPTY"))
        assert score == 50.0
        assert flags == ["no_usable_fundamentals"]
        assert confidence < 0.5
        assert components == {}


class TestScannerGating:
    def scanner(self, settings):
        return FundamentalsScanner(settings)

    async def test_skips_without_sec_user_agent(self, settings):
        scanner = self.scanner(settings)
        token = TokenSnapshot("AAPL", chain="us_equity", asset_class="equity", symbol="AAPL")
        signal = await scanner.collect(token, ScanContext(session=None, settings=settings))
        assert signal.status.value == "skipped"
        assert "SEC_USER_AGENT" in signal.summary

    async def test_not_applicable_to_a_crypto_token(self, settings):
        scanner = self.scanner(settings)
        token = TokenSnapshot("0xabc", asset_class="crypto", symbol="WETH")
        ctx = ScanContext(session=None, settings=settings)
        assert not scanner.supports(token)
        signal = await scanner.run(token, ctx)
        assert signal.status.value == "skipped"
        assert signal.weight == 0.0

    def test_only_equities_are_supported(self, settings):
        scanner = self.scanner(settings)
        assert scanner.asset_classes == frozenset({AssetClass.EQUITY})
