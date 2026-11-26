"""B12 — tests for scripts/run_equibles_rerun.py (December re-run wrapper).

Fake clients only. No network, no real Alpaca, no .env/keys, $0 LLM
spend. The frozen November price files and results are the regression
fixture: the wrapper pointed at them must reproduce the November
numbers EXACTLY, proving the wrapper changed no math.

Run: .venv/bin/python -m pytest test_b12_equibles_rerun.py -v
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import fetch_pilot_prices  # noqa: E402
import run_equibles_rerun as rerun  # noqa: E402
from alpaca.data.enums import Adjustment  # noqa: E402

RAW = REPO / "experiments" / "equibles-pilot-raw"
NOV_JSON = REPO / "experiments" / "exp-equibles-pilot-returns.json"
NOV_SYMBOLS = ["ABBV", "ABT", "AMD", "AMGN", "AMT", "AMZN", "SPY"]


# ---------------------------------------------------------------- fakes

def _frozen_frames():
    """The frozen November price files, as DataFrames (what Alpaca
    would return). Read once per test from disk — never modified."""
    frames = {}
    for sym in NOV_SYMBOLS:
        frames[sym] = pd.read_csv(RAW / f"prices-adjusted-{sym}.csv",
                                  index_col=0, parse_dates=True)
    return frames


class _FakeAlpaca:
    """Records every StockBarsRequest; returns the frozen frames."""

    def __init__(self, frames):
        self.frames = frames
        self.requests = []

    def get_stock_bars(self, req):
        self.requests.append(req)
        return SimpleNamespace(df=self.frames[req.symbol_or_symbols])


class _FakeSheet:
    """Row-count + append recorder standing in for the Sheet CLI."""

    def __init__(self, rows=200):
        self._rows = rows
        self.appends = []

    def row_count(self):
        return self._rows

    def append(self, rows):
        self.appends.append(list(rows))
        self._rows += len(rows)


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _november_hashes():
    return {p.name: _sha256(p) for p in [NOV_JSON] + sorted(RAW.glob("prices-adjusted-*.csv"))}


def _run_wrapper(tmp_path, as_of="2026-11-21", extra_args=(), sheet=None):
    prices_dir = tmp_path / "prices"
    results_dir = tmp_path / "results"
    registry_path = tmp_path / "registry.jsonl"
    argv = ["--as-of", as_of, "--prices-dir", str(prices_dir),
            "--results-dir", str(results_dir), *extra_args]
    rc = rerun.main(argv, fetch_client=_FakeAlpaca(_frozen_frames()),
                    sheet=sheet, repo=REPO, today=date(2026, 11, 21),
                    registry_path=registry_path)
    assert rc == 0
    return prices_dir, results_dir, registry_path


# ------------------------------------------------------- 1. regression

def test_regression_reproduces_november_exactly(tmp_path):
    """KEY ACCEPTANCE TEST: the wrapper on the frozen November prices
    (as-of 2026-11-21) reproduces the November result exactly — same
    n, same exclusions, same ICs, same median split. Any drift here
    means the wrapper changed the math."""
    before = _november_hashes()
    prices_dir, results_dir, _reg = _run_wrapper(tmp_path, extra_args=["--no-sheet"])
    after = _november_hashes()
    assert before == after, "November files were modified by the run"

    nov = json.loads(NOV_JSON.read_text())
    label = "nov2026"
    got = json.loads((results_dir / f"exp-equibles-pilot-returns-{label}.json").read_text())

    assert got["n_scored"] == nov["n_scored"] == 21
    assert got["n_included"] == nov["n_included"] == 16
    assert got["n_excluded"] == nov["n_excluded"] == 5
    assert [(e["ticker"], e["call_date"]) for e in got["excluded"]] == \
        [(e["ticker"], e["call_date"]) for e in nov["excluded"]] == [
            ("ABBV", "2026-10-30"), ("AMD", "2026-11-03"), ("AMGN", "2026-11-03"),
            ("AMT", "2026-10-27"), ("AMZN", "2026-10-29")]
    assert got["verdict"] == nov["verdict"] == "no evidence"
    assert got["last_trading_day"] == nov["last_trading_day"] == "2026-11-20"

    # The whole IC table and median split, bit-for-bit.
    assert got["ic_table"] == nov["ic_table"]
    assert got["median_split"] == nov["median_split"]
    # Per-call returns, bit-for-bit.
    assert got["per_call"] == nov["per_call"]

    # Headline numbers at printed precision.
    score_raw = got["ic_table"]["score__raw"]["pearson"]
    assert f"{score_raw['r']:+.2f}" == "+0.21"
    assert f"{score_raw['p']:.2f}" == "0.43"
    score_excess = got["ic_table"]["score__excess"]["pearson"]
    assert f"{score_excess['r']:+.2f}" == "+0.16"
    assert f"{score_excess['p']:.2f}" == "0.55"
    assert f"{got['median_split']['spread_raw'] * 100:+.2f}" == "+9.76"

    # The drop rule on the November numbers: max |score IC| 0.21 is
    # within one SE of zero (SE ~= 0.28 at n=16) -> DROP.
    decision, reason, max_ic, cleared = rerun.decide_drop(got)
    assert decision == "DROP"
    assert not cleared
    assert abs(max_ic - 0.21310990457864967) < 1e-12

    # Generated .md exists and states the verdict.
    md = (results_dir / f"exp-equibles-pilot-returns-{label}.md").read_text()
    assert "## Verdict: NO EVIDENCE" in md
    assert "DROP" in md
    assert "+0.21" in md


# ------------------------------------------------------- 2. drop rule

def _synthetic_result(pearson_raw, pearson_excess, spearman_raw,
                      spearman_excess, n=21):
    """A minimal result dict with synthetic score ICs only (never real
    data) — for testing the drop-rule logic in isolation."""
    def ic(r, p):
        return {"r": r, "t": 1.0, "p": p}
    return {
        "n_included": n,
        "ic_table": {
            "score__raw": {"n": n, "pearson": ic(pearson_raw[0], pearson_raw[1]),
                           "spearman": ic(spearman_raw[0], spearman_raw[1])},
            "score__excess": {"n": n, "pearson": ic(pearson_excess[0], pearson_excess[1]),
                              "spearman": ic(spearman_excess[0], spearman_excess[1])},
        },
    }


@pytest.mark.parametrize("ics,expected", [
    # IC ~0 on all four -> DROP.
    (((0.05, 0.80), (0.03, 0.90), (-0.04, 0.85), (0.02, 0.95)), "DROP"),
    # Exactly zero -> DROP.
    (((0.0, 1.0), (0.0, 1.0), (0.0, 1.0), (0.0, 1.0)), "DROP"),
    # Bar cleared, positive -> KEEP.
    (((0.35, 0.05), (0.10, 0.60), (0.12, 0.55), (0.08, 0.70)), "KEEP"),
    # Bar cleared, negative direction counts (|IC|) -> KEEP.
    (((-0.45, 0.02), (0.10, 0.60), (0.12, 0.55), (0.08, 0.70)), "KEEP"),
    # Bar cleared on Spearman excess only -> KEEP.
    (((0.10, 0.60), (0.10, 0.60), (0.12, 0.55), (0.33, 0.04)), "KEEP"),
    # Near the bar but p too high -> not cleared; 0.31 exceeds one SE
    # (0.24 at n=21) -> REVIEW, not DROP.
    (((0.31, 0.12), (0.10, 0.60), (0.12, 0.55), (0.08, 0.70)), "REVIEW"),
    # Near-miss in the middle zone -> REVIEW.
    (((0.26, 0.20), (0.10, 0.60), (0.12, 0.55), (0.08, 0.70)), "REVIEW"),
    # |IC| exactly at the bar with p just under 0.10 -> KEEP.
    (((0.30, 0.09), (0.10, 0.60), (0.12, 0.55), (0.08, 0.70)), "KEEP"),
])
def test_decide_drop_on_synthetic_ics(ics, expected):
    result = _synthetic_result(*ics)
    decision, reason, _, _ = rerun.decide_drop(result)
    assert decision == expected, f"{ics} -> {decision} ({reason})"


def test_decide_drop_november_numbers_are_drop():
    """The frozen November headline ICs, as a synthetic dict, drop:
    max |IC| 0.21 < one SE (0.28 at n=16)."""
    result = _synthetic_result((0.2131, 0.43), (0.1599, 0.55),
                               (0.14, 0.60), (0.00, 0.99), n=16)
    decision, _, max_ic, cleared = rerun.decide_drop(result)
    assert decision == "DROP"
    assert not cleared
    assert max_ic == pytest.approx(0.2131)


# ------------------------------------------------------- 3. --no-sheet

def test_no_sheet_mode_writes_files_and_never_touches_sheet(tmp_path):
    """--no-sheet: md/json are written, the Sheet is never called."""
    sheet = _FakeSheet()
    _, results_dir, _reg = _run_wrapper(tmp_path, extra_args=["--no-sheet"], sheet=sheet)
    assert sheet.appends == []
    assert (results_dir / "exp-equibles-pilot-returns-nov2026.json").exists()
    assert (results_dir / "exp-equibles-pilot-returns-nov2026.md").exists()


def test_sheet_mode_appends_per_call_plus_summary(tmp_path):
    """Without --no-sheet, the fake Sheet gets exactly n+1 rows."""
    sheet = _FakeSheet(rows=200)
    _run_wrapper(tmp_path, sheet=sheet)
    assert len(sheet.appends) == 1
    rows = sheet.appends[0]
    assert len(rows) == 17  # 16 per-call rows + 1 summary row
    assert sheet.row_count() == 217
    assert rows[0][0] == "exp-equibles-pilot-returns"
    assert rows[0][1] == "2026-11-21"
    assert rows[-1][3] == "SUMMARY"
    assert rows[-1][2] == "pilot 16 calls, T+20"
    assert "no evidence" in rows[-1][11]


# ------------------------------------------------------- 4. no collision

def test_dated_filenames_never_collide_with_november(tmp_path):
    """Every file the wrapper writes is dated; the November bare
    names are never produced, in any directory the wrapper uses."""
    prices_dir, results_dir, _reg = _run_wrapper(tmp_path, extra_args=["--no-sheet"])
    # Dated price files exist; bare November names do not.
    for sym in NOV_SYMBOLS:
        assert (prices_dir / f"prices-adjusted-2026-11-21-{sym}.csv").exists()
        assert not (prices_dir / f"prices-adjusted-{sym}.csv").exists()
    # Dated results exist; the bare November results name does not.
    assert (results_dir / "exp-equibles-pilot-returns-nov2026.json").exists()
    assert (results_dir / "exp-equibles-pilot-returns-nov2026.md").exists()
    assert not (results_dir / "exp-equibles-pilot-returns.json").exists()
    assert not (results_dir / "exp-equibles-pilot-returns.md").exists()
    # And none of the wrapper's paths equals a November path.
    nov_paths = {NOV_JSON, *(RAW / f"prices-adjusted-{s}.csv" for s in NOV_SYMBOLS),
                 REPO / "experiments" / "exp-equibles-pilot-returns.md"}
    written = set(prices_dir.glob("*")) | set(results_dir.glob("*"))
    assert not (written & nov_paths)


def test_december_label_filenames(tmp_path):
    """A December as-of produces dec2026 results and 2026-12-* prices."""
    prices_dir, results_dir, _reg = _run_wrapper(tmp_path, as_of="2026-12-03",
                                           extra_args=["--no-sheet"])
    assert (results_dir / "exp-equibles-pilot-returns-dec2026.json").exists()
    assert (results_dir / "exp-equibles-pilot-returns-dec2026.md").exists()
    assert (prices_dir / "prices-adjusted-2026-12-03-SPY.csv").exists()


# ------------------------------------------------------- 5. fetch step

def test_fetch_step_writes_expected_csvs_with_fake_client(tmp_path):
    """The fetch step, fake Alpaca only: 7 CSVs, right columns, END
    passed through as the request end date, no real network."""
    frames = _frozen_frames()
    client = _FakeAlpaca(frames)
    out_dir = tmp_path / "prices"
    written = fetch_pilot_prices.main(
        end="2026-11-21", out_dir=out_dir,
        filename_template="prices-adjusted-{date}-{sym}.csv", client=client)
    assert len(written) == 7
    assert len(client.requests) == 7
    for req in client.requests:
        assert str(req.start)[:10] == "2025-10-01"
        assert str(req.end)[:10] == "2026-11-21"
        assert req.adjustment == Adjustment.ALL
    for sym in NOV_SYMBOLS:
        path = out_dir / f"prices-adjusted-2026-11-21-{sym}.csv"
        assert path.exists()
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert len(df) == len(frames[sym])
        assert df.index.max().date() <= date(2026, 11, 21)


def test_fetch_default_filenames_reproduce_november_names(tmp_path):
    """Unparameterized, the fetch still writes the bare November
    filenames (standalone November behaviour, unchanged)."""
    written = fetch_pilot_prices.main(
        out_dir=tmp_path, client=_FakeAlpaca(_frozen_frames()))
    assert {p.name for p in written} == {f"prices-adjusted-{s}.csv" for s in NOV_SYMBOLS}


def test_bad_as_of_is_rejected(tmp_path):
    """A malformed --as-of exits with an argument error, before any
    fetch or file write."""
    with pytest.raises(SystemExit):
        rerun.main(["--as-of", "not-a-date", "--no-sheet",
                    "--prices-dir", str(tmp_path / "p"),
                    "--results-dir", str(tmp_path / "r")],
                   fetch_client=_FakeAlpaca(_frozen_frames()),
                   repo=REPO, today=date(2026, 11, 21))
    assert not (tmp_path / "p").exists()


# ------------------------------------------------------- 6. registry log

def _bar_clearing_result(decision_ic=0.35):
    """A minimal result dict shaped like run_pilot_returns output, for
    testing append_registry without running the full pipeline."""
    ic = lambda r, p: {"r": r, "t": 1.0, "p": p, "n": 21}
    return {
        "run_label": "dec2026",
        "verdict": "no evidence",
        "n_scored": 21,
        "n_included": 21,
        "n_excluded": 0,
        "ic_table": {
            "score__raw": {"n": 21, "pearson": ic(decision_ic, 0.05), "spearman": ic(0.10, 0.60)},
            "score__excess": {"n": 21, "pearson": ic(0.12, 0.50), "spearman": ic(0.05, 0.80)},
        },
        "median_split": {"spread_raw": 0.02, "spread_excess": 0.01},
    }


def test_registry_appended_with_run_summary(tmp_path):
    """The wrapper appends exactly one registry line per run, with the
    run's verdict and drop-rule outcome — the runbook Step 7 log."""
    _, results_dir, registry_path = _run_wrapper(
        tmp_path, extra_args=["--no-sheet"])
    lines = registry_path.read_text().strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["experiment_id"] == "exp-equibles-pilot-returns-nov2026"
    assert entry["status"] == "failed"  # November numbers clear no bar
    assert entry["results_file"].endswith(
        "exp-equibles-pilot-returns-nov2026.json")
    assert entry["spend"] == {"run_usd": 0.0}
    assert "Drop-rule: DROP" in entry["gate"]
    assert "no evidence" in entry["gate"].lower()
    assert entry["design_doc"] == "experiments/exp-equibles-pilot-returns-design.md"


def test_registry_not_written_with_no_registry_flag(tmp_path):
    """--no-registry: no registry file is created at all."""
    _, _, registry_path = _run_wrapper(
        tmp_path, extra_args=["--no-sheet", "--no-registry"])
    assert not registry_path.exists()


def test_registry_status_passed_when_bar_cleared(tmp_path):
    """append_registry directly: a bar-clearing result logs 'passed'."""
    result = _bar_clearing_result(decision_ic=0.35)
    decision, reason, _, bar = rerun.decide_drop(result)
    assert decision == "KEEP" and bar is True
    registry_path = tmp_path / "registry.jsonl"
    rerun.append_registry(result, decision, reason, bar,
                          tmp_path / "out.json", registry_path)
    entry = json.loads(registry_path.read_text().strip().split("\n")[0])
    assert entry["status"] == "passed"
    assert entry["experiment_id"] == "exp-equibles-pilot-returns-dec2026"
    assert "Drop-rule: KEEP" in entry["gate"]


def test_registry_independent_of_sheet_flag(tmp_path):
    """--no-sheet skips the Sheet but still logs the registry."""
    _, _, registry_path = _run_wrapper(
        tmp_path, extra_args=["--no-sheet"], sheet=_FakeSheet())
    assert json.loads(registry_path.read_text().strip().split("\n")[0])[
        "experiment_id"] == "exp-equibles-pilot-returns-nov2026"
