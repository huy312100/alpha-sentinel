"""LLM output is untrusted: every coercion and fallback is pinned here."""

from __future__ import annotations

import pytest

from ai_engine.parsing import (
    ResponseParseError,
    extract_json,
    normalise_score,
    parse_alpha,
    parse_risk,
    to_bool,
    to_confidence,
    to_float,
    to_str_list,
)
from core.models import Severity


class TestExtractJson:
    def test_plain_object(self):
        assert extract_json('{"a": 1}') == {"a": 1}

    def test_markdown_fence(self):
        text = 'Here you go:\n```json\n{"alpha_score": 77}\n```\nThanks!'
        assert extract_json(text) == {"alpha_score": 77}

    def test_trailing_prose_after_object(self):
        assert extract_json('{"veto": false} - as requested') == {"veto": False}

    def test_object_embedded_in_sentence(self):
        text = 'Verdict follows {"risk_score": 12} and that is final.'
        assert extract_json(text) == {"risk_score": 12}

    @pytest.mark.parametrize("text", ["", "   ", "no json here", "[1, 2, 3]"])
    def test_unrecoverable_payloads_raise(self, text):
        with pytest.raises(ResponseParseError):
            extract_json(text)


class TestCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(82, 82.0), (82.4, 82.4), ("77.5", 77.5), ("63%", 63.0), ("1,024", 1024.0), ("score: 55", 55.0)],
    )
    def test_to_float(self, raw, expected):
        assert to_float(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "n/a", True, False, {}, []])
    def test_to_float_falls_back(self, raw):
        assert to_float(raw, default=-1.0) == -1.0

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(0.9, 0.9), ("0.82", 0.82), (0.0, 0.0), (1.0, 1.0), (7, 7.0), (10, 10.0),
         (100, 100.0), (150, 100.0), (-5, 0.0), (12.345, 12.35)],
    )
    def test_normalise_score_preserves_prompt_scale(self, raw, expected):
        assert normalise_score(raw) == expected

    def test_normalise_score_default_on_garbage(self):
        assert normalise_score("nonsense") == 50.0

    @pytest.mark.parametrize("raw", [float("nan"), float("inf"), "nan", "inf", "Infinity"])
    def test_non_finite_values_are_not_scores(self, raw):
        assert to_float(raw, default=-1.0) == -1.0
        assert normalise_score(raw) == 50.0
        assert to_confidence(raw) == 0.5

    @pytest.mark.parametrize(("raw", "expected"), [(0.85, 0.85), (85, 0.85), ("0.4", 0.4), ("120", 1.0), (-1, 0.0)])
    def test_to_confidence(self, raw, expected):
        assert to_confidence(raw) == expected

    @pytest.mark.parametrize("raw", ["true", "YES", "y", "1", "veto", "block"])
    def test_to_bool_true(self, raw):
        assert to_bool(raw) is True

    @pytest.mark.parametrize("raw", ["false", "No", "n", "0", "", "maybe", None])
    def test_to_bool_false(self, raw):
        assert to_bool(raw) is False

    def test_to_bool_numbers_and_bools(self):
        assert to_bool(1) is True
        assert to_bool(0) is False
        assert to_bool(True) is True

    def test_to_str_list_splits_and_trims(self):
        assert to_str_list("listing\nupgrade; audit") == ["listing", "upgrade", "audit"]
        assert to_str_list([" a ", "", "b"]) == ["a", "b"]


class TestParseAlpha:
    def test_required_field_only(self):
        alpha = parse_alpha({"alpha_score": 71}, model="test-alpha")
        assert alpha.alpha_score == 71.0
        assert alpha.confidence == 0.5
        assert alpha.thesis == ""
        assert alpha.model == "test-alpha"

    def test_falls_back_to_score_key(self):
        assert parse_alpha({"score": 8.5}, model="m").alpha_score == 8.5

    def test_lists_and_map_are_coerced(self):
        alpha = parse_alpha(
            {
                "alpha_score": 80,
                "thesis": "x" * 5000,
                "catalysts": ["CEX listing"],
                "red_flags": "unlock; insider selling",
                "layer_highlights": {"social": "3x mentions"},
                "usage": {"total_tokens": 10},
            },
            model="m",
            latency_ms=12.345,
        )
        assert len(alpha.thesis) == 4000
        assert alpha.catalysts == ["CEX listing"]
        assert alpha.red_flags == ["unlock", "insider selling"]
        assert alpha.layer_highlights == {"social": "3x mentions"}
        assert alpha.latency_ms == 12.3

    def test_empty_payload_never_raises(self):
        alpha = parse_alpha({}, model="m")
        assert alpha.alpha_score == 50.0
        assert alpha.momentum_horizon == "swing"


class TestParseRisk:
    def test_critical_severity_forces_veto(self):
        risk = parse_risk({"risk_score": 30, "severity": "critical", "veto": False}, model="m")
        assert risk.severity is Severity.CRITICAL
        assert risk.veto is True

    def test_explicit_veto_honoured(self):
        risk = parse_risk({"risk_score": 20, "veto": "true"}, model="m")
        assert risk.veto is True

    def test_critical_finding_forces_veto(self):
        payload = {
            "risk_score": 40,
            "findings": [{"category": "honeypot", "severity": "critical", "description": "cannot sell"}],
        }
        risk = parse_risk(payload, model="m")
        assert risk.veto is True
        assert risk.findings[0].category == "honeypot"

    def test_string_findings_use_fallback_severity(self):
        risk = parse_risk({"risk_score": 88, "risks": ["unlocked treasury"]}, model="m")
        assert risk.findings[0].description == "unlocked treasury"
        assert risk.findings[0].category == "unclassified"
        assert risk.findings[0].severity is Severity.CRITICAL

    def test_bad_finding_shapes_are_dropped(self):
        risk = parse_risk({"risk_score": 10, "findings": [None, 5, {"category": "x"}]}, model="m")
        assert len(risk.findings) == 1
        assert risk.findings[0].severity is Severity.MEDIUM

    def test_non_list_findings_are_ignored(self):
        assert parse_risk({"risk_score": 10, "findings": "none"}, model="m").findings == []

    def test_missing_risk_score_is_rejected_not_defaulted(self):
        with pytest.raises(ResponseParseError, match="risk_score"):
            parse_risk({}, model="m")
        with pytest.raises(ResponseParseError, match="risk_score"):
            parse_risk({"risk": "high"}, model="m")

        assert to_str_list(None) == []
        assert to_str_list("x", limit=2) == ["x"]

    def test_to_str_list_respects_limit(self):
        assert to_str_list([str(index) for index in range(20)], limit=5) == ["0", "1", "2", "3", "4"]
