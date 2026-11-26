"""Tests for the EDGAR point-in-time dataset builder.

Fixture-based and offline: no SEC calls, no API keys. The real shards
are validated by ``build_edgar_dataset.py --validate``, not by pytest.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

import build_edgar_dataset as bed
from experiments import list_experiments, register_experiment

# Each builder receives its own date window explicitly (2026-11-25
# refactor): v1's WINDOW_START / WINDOW_END are constants, and v2
# passes its own window to every v1 call instead of mutating the
# globals at import time. This file used to pin the v1 window per test
# with an autouse fixture — that fixture is gone now, and the test
# below is the regression guard for the refactor.


def _sample_record(**overrides):
    rec = bed.build_record(
        ticker="AAPL",
        cik=320193,
        form="10-K",
        accession="0000320193-24-000123",
        filing_date=date(2024, 11, 1),
        period_end_date=date(2024, 9, 28),
        mda_raw="Item 7. Management discussion text.",
        risk_raw="Item 1A. Risk factors text.",
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/",
        edgartools_version="5.62.1",
    )
    rec.update(overrides)
    return rec


def test_clean_text_collapses_whitespace():
    assert bed.clean_text("Item 7.\u00a0\u00a0\u00a0  MD&A\n\n  text") == "Item 7. MD&A text"
    assert bed.clean_text("") == ""
    assert bed.clean_text(None) == ""


def test_trim_section_caps_and_flags():
    text, trimmed = bed.trim_section("x" * 50, 10)
    assert text == "x" * 10 and trimmed is True
    text, trimmed = bed.trim_section("short", 10)
    assert text == "short" and trimmed is False


def test_build_excerpt_respects_cap_and_order():
    excerpt, trimmed = bed.build_excerpt("M" * 100_000, "R" * 100_000)
    assert len(excerpt) == bed.EXCERPT_MAX_CHARS
    assert trimmed is True
    assert excerpt.startswith("M" * 100_000)
    excerpt, trimmed = bed.build_excerpt("mda", "risk")
    assert excerpt == "mda\n\nrisk" and trimmed is False


def test_record_carries_filing_date_and_hash():
    rec = _sample_record()
    assert rec["filing_date"] == "2024-11-01"
    assert rec["period_end_date"] == "2024-09-28"
    assert rec["fiscal_year"] == 2024
    assert rec["excerpt_sha256"] == bed.excerpt_sha256(rec["excerpt_text"])
    assert rec["mda_chars"] == len(rec["mda_text"])
    assert rec["excerpt_chars"] == len(rec["excerpt_text"])
    assert rec["missing_sections"] == []


def test_record_flags_missing_sections():
    rec = _sample_record()
    rec2 = bed.build_record(
        ticker="ZZZ", cik=1, form="10-Q", accession="0000000001-24-000001",
        filing_date=date(2024, 5, 3), period_end_date=date(2024, 3, 30),
        mda_raw="Some MD&A.", risk_raw=None,
        source_url="https://example.com/", edgartools_version="5.62.1",
    )
    assert rec2["missing_sections"] == ["risk"]
    assert rec2["risk_chars"] == 0


def test_validate_record_accepts_good_record():
    errors: list[str] = []
    assert bed.validate_record(_sample_record(), errors) is True
    assert errors == []


def test_validate_record_rejects_future_filing_date():
    rec = _sample_record(filing_date="2027-01-15")
    errors: list[str] = []
    assert bed.validate_record(rec, errors) is False
    assert any("outside window" in e for e in errors)


def test_importing_v2_does_not_change_v1_window():
    """Regression (2026-11-25 refactor): importing the v2 builder must
    not change the window v1 validates against. A Dec 2026 record is
    outside the v1 window (2023-01-01..2026-11-13) but inside the v2
    window (2026-11-13..2026-12-31) — it must FAIL under v1's default
    and PASS with the v2 window passed explicitly, regardless of
    import order.
    """
    import build_edgar_dataset_v2 as v2

    assert bed.WINDOW_START == date(2023, 1, 1)
    assert bed.WINDOW_END == date(2026, 11, 13)
    rec = _sample_record(filing_date="2026-12-01", period_end_date="2026-09-30")
    errors: list[str] = []
    assert bed.validate_record(rec, errors) is False
    assert any("outside window" in e for e in errors)
    errors = []
    assert bed.validate_record(rec, errors, window=v2.V2_WINDOW) is True
    assert errors == []


def test_validate_record_rejects_amendment_form():
    rec = _sample_record(form="10-K/A")
    errors: list[str] = []
    assert bed.validate_record(rec, errors) is False


def test_validate_record_rejects_bad_hash():
    rec = _sample_record(excerpt_sha256="0" * 64)
    errors: list[str] = []
    assert bed.validate_record(rec, errors) is False


def test_validate_record_rejects_period_after_filing():
    rec = _sample_record(period_end_date="2024-12-31")
    errors: list[str] = []
    assert bed.validate_record(rec, errors) is False
    assert any("after filing_date" in e for e in errors)


def test_shard_key_is_quarterly_and_under_api_blob_ceiling():
    # Half-year shards run ~48 MB; the Git Data API rejects blobs that
    # large, so shards are quarterly (~18-25 MB each).
    assert bed._shard_key("2023-01-05") == (2023, 1)
    assert bed._shard_key("2023-04-28") == (2023, 2)
    assert bed._shard_key("2026-11-12") == (2026, 4)
    assert bed.shard_path(2024, 3).name == "excerpts_2024_Q3.jsonl"


def test_shard_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(bed, "DATA_DIR", tmp_path)
    recs = [_sample_record(), _sample_record(
        ticker="MSFT", accession="0000789019-24-000001",
        filing_date="2023-07-25")]
    bed.append_records(recs)
    loaded = bed.load_all_records()
    assert len(loaded) == 2
    assert {r["accession"] for r in loaded} == {
        "0000320193-24-000123", "0000789019-24-000001"}
    assert bed.load_existing_accessions() == {
        "0000320193-24-000123", "0000789019-24-000001"}


def test_coverage_summary_counts_windows():
    recs = [
        _sample_record(),  # 2024-11-01 train
        _sample_record(ticker="MSFT", accession="a2", filing_date="2026-09-15"),  # holdout
        _sample_record(ticker="MSFT", accession="a3", filing_date="2023-02-10"),  # warm-up
    ]
    summary = bed.coverage_summary(recs)
    assert summary["n_records"] == 3
    assert summary["n_tickers"] == 2
    assert summary["train_window_records"] == 1
    assert summary["holdout_window_records"] == 1
    assert summary["per_year"] == {"2024": 1, "2026": 1, "2023": 1}
    assert summary["filing_date_min"] == "2023-02-10"
    assert summary["filing_date_max"] == "2026-09-15"


def test_validate_dataset_catches_duplicate_and_manifest_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(bed, "DATA_DIR", tmp_path)
    rec = _sample_record()
    bed.append_records([rec, rec])  # deliberate duplicate
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"coverage": {"n_records": 99}}))
    ok, report = bed.validate_dataset()
    assert ok is False
    assert any("duplicate" in e for e in report["errors"])
    assert any("manifest" in e for e in report["errors"])


def test_html_to_text_keeps_split_words_intact():
    raw = "<p>MANAGEM<span>ENT'S</span> DISCUSSION</p><div>next <b>para</b>graph</div>"
    assert bed.html_to_text(raw) == "MANAGEMENT'S DISCUSSION next paragraph"


def _fallback_doc(body: str) -> str:
    """Minimal plain-text filing body for fallback tests."""
    return " ".join(body.split())


def test_mda_fallback_recovers_section_between_headings():
    prose = "Our MD&A begins with an overview. " + "Revenue grew. " * 400
    text = _fallback_doc(f"""
        Table of Contents Item 7. Management's Discussion and Analysis 12
        Item 7A. Quantitative Disclosures 40
        Management's Discussion and Analysis Overview {prose}
        Quantitative and Qualitative Disclosures About Market Risk
        We are affected by currency moves.
    """)
    got = bed.extract_mda_fallback(text, "10-K")
    assert got is not None
    assert got.startswith("Management's Discussion and Analysis Overview")
    assert "Revenue grew." in got
    assert "currency moves" not in got


def test_mda_fallback_skips_toc_and_glossary():
    # TOC hit is short (glossary span < 5k chars) so the next candidate wins.
    prose = "Real MD&A prose here. " + "Operations continued. " * 400
    text = _fallback_doc(f"""
        Management's Discussion and Analysis of Financial Condition BSBY
        Bloomberg Short-Term Bank Yield Index MSR Mortgage servicing right
        CARES Act Coronavirus Aid, Relief and Economic Security Act NSFR
        Net Stable Funding Ratio CCAR Comprehensive Capital Analysis and
        Review OCC Office of the Comptroller of the Currency
        Management's Discussion and Analysis Overview {prose}
        Part II Other Information
    """)
    got = bed.extract_mda_fallback(text, "10-Q")
    assert got is not None
    assert got.startswith("Management's Discussion and Analysis Overview")
    assert "BSBY" not in got


def test_mda_fallback_returns_none_when_no_plausible_span():
    text = _fallback_doc("""
        Table of Contents Management's Discussion and Analysis 8
        Item 7A Quantitative and Qualitative Disclosures 9
    """)
    assert bed.extract_mda_fallback(text, "10-K") is None
    assert bed.extract_mda_fallback("no headings at all here", "10-K") is None


def test_mda_fallback_pnc_financial_review_heading():
    prose = "PNC financial review prose. " + "Net income rose. " * 400
    text = _fallback_doc(f"""
        Table of Contents Management's Discussion and Analysis (MD&A).
        Financial Review 1 Executive Summary
        Financial Review Executive Summary {prose}
        Quantitative and Qualitative Disclosures About Market Risk
        Interest rate risk discussion.
    """)
    got = bed.extract_mda_fallback(text, "10-Q", ticker="PNC")
    assert got is not None
    assert got.startswith("Financial Review Executive Summary")
    assert "Interest rate risk" not in got
    # Without the PNC alternate start, the same text yields nothing.
    assert bed.extract_mda_fallback(text, "10-Q", ticker="ZZZ") is None


def test_record_marks_fallback_mda_source():
    rec = bed.build_record(
        ticker="INTC", cik=50863, form="10-K", accession="0000050863-26-000011",
        filing_date=date(2026, 1, 23), period_end_date=date(2025, 12, 27),
        mda_raw="Recovered MD&A.", risk_raw="Risk text.",
        source_url="https://example.com/", edgartools_version="5.62.1",
        mda_source="fallback",
    )
    assert rec["mda_source"] == "fallback"
    assert rec["missing_sections"] == []
    errors: list[str] = []
    assert bed.validate_record(rec, errors) is True


def test_dataset_registered_before_build(tmp_path):
    experiments = list_experiments()
    ids = [e["experiment_id"] for e in experiments]
    assert "dataset-edgar-point-in-time-v1" in ids
    rec = next(e for e in experiments if e["experiment_id"] == "dataset-edgar-point-in-time-v1")
    assert rec["parameters"]["forms"] == ["10-K", "10-Q"]
    assert rec["parameters"]["amendments"] == "excluded (10-K/A, 10-Q/A)"
    assert rec["holdout_rule"].startswith("holdout 2026-08-18..2026-11-13")
    # Registration is append-only and unique.
    with pytest.raises(ValueError):
        register_experiment({
            "experiment_id": "dataset-edgar-point-in-time-v1",
            "strategy": "dup", "parameters": {}, "cost_model": {},
            "horizon": "x", "benchmark": "x", "holdout_rule": "x",
        })
