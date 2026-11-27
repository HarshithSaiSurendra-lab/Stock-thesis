"""B14 — tests for scripts/read_fresh_validation_gate.py.

Synthetic fixtures in tmp dirs ONLY. No network, no real Alpaca, no
.env/keys, $0 LLM spend. The reader is never pointed at the frozen
holdout (2026-08-18..2026-11-13) or the real Nov 16-Dec 31 window:
every test builds its own window under tmp_path, and the path-safety
test asserts on the exact paths the reader opened.

Run: .venv/bin/python -m pytest test_b14_gate_reader.py -v
"""
from __future__ import annotations

import hashlib
import json
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import read_fresh_validation_gate as gate  # noqa: E402

FORBIDDEN_PATHS = [
    REPO / "experiments" / "results" / "exp-llm-long-horizon-v2.json",
    REPO / "experiments" / "results" / "exp-llm-long-horizon-v3.json",
    REPO / "experiments" / "results" / "exp-r2-h3-lazy-veto-v3-confirmation.json",
    REPO / "experiments" / "results" / "exp-r2-h7-c1c-reversion-rsi-confirmation.json",
    REPO / "experiments" / "results" / "exp-technical-v2.json",
    REPO / "experiments" / "exp-fresh-validation-v1-scores-haiku.json",
    REPO / "experiments" / "exp-fresh-validation-v1-step0.json",
    REPO / "data" / "edgar_excerpts_v2" / "manifest.json",
]

HOLIDAYS_2026 = {date(2026, 11, 26), date(2026, 12, 25), date(2027, 1, 1)}


def _trading_days(start, end):
    """Weekdays minus US market holidays, as ISO strings."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in HOLIDAYS_2026:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


BASE_DAYS = _trading_days(date(2026, 11, 16), date(2026, 12, 31))
assert len(BASE_DAYS) == 32, f"fixture base window has {len(BASE_DAYS)} days, expected 32"
EXT2_DAYS = _trading_days(date(2026, 11, 16), date(2027, 1, 15))
assert len(EXT2_DAYS) == 42, f"fixture ext-2 window has {len(EXT2_DAYS)} days, expected 42"
EXT3_DAYS = _trading_days(date(2026, 11, 16), date(2027, 1, 29))
assert len(EXT3_DAYS) == 52, f"fixture ext-3 window has {len(EXT3_DAYS)} days, expected 52"


def _costs(net10, net20, trades, sharpe=1.0, dsr=0.98, net0=None):
    return {
        "cost_0bps": {"total_return": net0 if net0 is not None else net10 + 0.01,
                      "sharpe": sharpe, "max_drawdown": -0.05, "hit_rate": 0.55,
                      "trades": trades, "avg_daily_turnover": 0.10,
                      "deflated_sharpe": dsr, "sharpe_null_max": 0.4,
                      "alpha_annual_vs_spy": 0.10, "beta_vs_spy": 0.5,
                      "r_squared_vs_spy": 0.2},
        "cost_10bps": {"total_return": net10,
                       "sharpe": sharpe, "max_drawdown": -0.05, "hit_rate": 0.55,
                       "trades": trades, "avg_daily_turnover": 0.10,
                       "deflated_sharpe": dsr, "sharpe_null_max": 0.4,
                       "alpha_annual_vs_spy": 0.10, "beta_vs_spy": 0.5,
                       "r_squared_vs_spy": 0.2},
        "cost_20bps": {"total_return": net20,
                       "sharpe": sharpe, "max_drawdown": -0.05, "hit_rate": 0.55,
                       "trades": trades, "avg_daily_turnover": 0.10,
                       "deflated_sharpe": dsr, "sharpe_null_max": 0.4,
                       "alpha_annual_vs_spy": 0.10, "beta_vs_spy": 0.5,
                       "r_squared_vs_spy": 0.2},
    }


def _observations(days, n_symbols=25, mode="null", seed=7):
    """Carried-forward similarity panel rows: one row per (day, symbol).

    mode 'null': similarity and fwd20 independent -> IC ~ 0 (SG4 passes).
    mode 'negative': fwd20 perfectly anti-ranked vs similarity every day
      -> daily IC -1.0, std 0 (SG4 fails; also exercises the std==0 path).
    """
    rng = random.Random(seed)
    obs = []
    for day in days:
        sims = [rng.random() for _ in range(n_symbols)]
        if mode == "negative":
            order = sorted(range(n_symbols), key=lambda i: sims[i])
            fwds = [0.0] * n_symbols
            for rank, i in enumerate(order):
                fwds[i] = -rank / n_symbols
        else:
            fwds = [rng.uniform(-0.03, 0.03) for _ in range(n_symbols)]
        for i in range(n_symbols):
            obs.append({"date": day, "ticker": f"SYM{i:02d}",
                        "similarity": round(sims[i], 4),
                        "fwd20_return": round(fwds[i], 5)})
    return obs


def _write_window(tmp_path, *, days=BASE_DAYS, n_filings=45, extension=0,
                  v4_net10=-0.02, v4_net20=-0.03,
                  design_nets=None, h3_mode="null", h7_sharpe=2.0,
                  h7_trades=30, window_end=None):
    """Build a synthetic window dir. design_nets maps design id ->
    (net10, net20, trades, dsr). Defaults make every design PASS."""
    window_dir = tmp_path / "window"
    (window_dir / "designs").mkdir(parents=True)
    end = window_end or days[-1]
    filings = []
    for i in range(n_filings):
        filings.append({"ticker": f"SYM{i % 25:02d}",
                        "filing_date": days[i % len(days)],
                        "form": "10-Q" if i % 3 else "10-K",
                        "haiku_score": round(0.1 + (i % 9) * 0.1, 2)})
    (window_dir / "window.json").write_text(json.dumps({
        "window_start": "2026-11-16", "window_end": end,
        "base_end": "2026-12-31", "extension": extension,
        "trading_days": days, "filings": filings,
    }, indent=2))
    v4 = {"experiment_id": "exp-technical-v2",
          "window_start": "2026-11-16", "window_end": end}
    v4.update(_costs(v4_net10, v4_net20, trades=40, sharpe=-0.5, dsr=None))
    (window_dir / "v4_base.json").write_text(json.dumps(v4, indent=2))
    nets = {"v2": (0.03, 0.02, 40, 0.97), "v3": (0.05, 0.04, 50, 0.98),
            "h3": (0.045, 0.035, 45, 0.96), "h7": (0.04, 0.03, h7_trades, 0.99),
            "ml-ranker-v1": (0.035, 0.025, 35, 0.90)}
    if design_nets:
        nets.update(design_nets)
    for did, (n10, n20, tr, dsr) in nets.items():
        sharpe = h7_sharpe if did == "h7" else 1.0
        d = {"design_id": did,
             "design_doc": f"experiments/{did}-design.md",
             "window_start": "2026-11-16", "window_end": end}
        d.update(_costs(n10, n20, trades=tr, sharpe=sharpe, dsr=dsr))
        (window_dir / "designs" / f"{did}.json").write_text(json.dumps(d, indent=2))
    (window_dir / "h3_similarity.json").write_text(json.dumps(
        {"observations": _observations(days, mode=h3_mode)}, indent=2))
    return window_dir


def _run(window_dir, out_dir, extra_args=()):
    return gate.main(["--window-dir", str(window_dir),
                      "--out-dir", str(out_dir), *extra_args])


def _read_report(out_dir):
    paths = sorted(out_dir.glob("exp-fresh-validation-v1-gate-*.json"))
    assert len(paths) == 1, f"expected exactly one gate report, got {[p.name for p in paths]}"
    return json.loads(paths[0].read_text()), paths[0]


def _hash_tree(root):
    return {str(p.relative_to(root)): hashlib.md5(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------- (a) all pass

def test_all_pass_candidate_is_best_net10(tmp_path):
    window_dir = _write_window(tmp_path)
    out_dir = tmp_path / "out"
    rc = _run(window_dir, out_dir)
    assert rc == gate.EXIT_OK
    result, path = _read_report(out_dir)
    assert result["window_start"] == "2026-11-16"
    assert result["window_end"] == "2026-12-31"
    assert result["extension"] == 0
    assert result["minimums"]["met"] is True
    assert result["v4_base"]["net10"] == pytest.approx(-0.02)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert len(by_id) == 5
    assert by_id["v2"]["verdict"] == "PASS"
    assert by_id["v3"]["verdict"] == "PASS"
    assert by_id["h3"]["verdict"] == "PASS"
    assert by_id["h3"]["sg4"] is True
    assert by_id["h7"]["verdict"] == "PASS"
    assert by_id["h7"]["audit"] is False
    assert by_id["ml-ranker-v1"]["verdict"] == "PASS"
    # ml-ranker DSR 0.90 < 0.95 -> "pass, weak"; the others are clean passes.
    assert by_id["ml-ranker-v1"]["pass_weak"] is True
    assert "DSR 0.9 < 0.95" in by_id["ml-ranker-v1"]["weak_reason"]
    assert by_id["v3"]["pass_weak"] is False
    assert result["candidate"] == "v3"  # best passing net10 (+5%)
    assert result["n_passed"] == 5
    assert result["dsr_n_trials_basis"] == 129 + 3 * 5
    # Dated report: md sibling exists; no shortfall report was written.
    assert path.with_suffix(".md").exists()
    assert list(out_dir.glob("exp-fresh-validation-v1-shortfall-*")) == []


# ------------------------------------------------------------- (b) SG1 fail

def test_sg1_fail_is_fail_and_excluded_from_candidacy(tmp_path):
    window_dir = _write_window(
        tmp_path, design_nets={"v2": (-0.03, -0.04, 40, 0.97)})
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert by_id["v2"]["verdict"] == "FAIL"
    assert by_id["v2"]["sg1"] is False  # net10 -3% < v4 -2%
    assert by_id["v2"]["sg3"] is False  # net10 < 0
    assert by_id["v3"]["verdict"] == "PASS"
    assert result["candidate"] == "v3"
    assert result["n_passed"] == 4
    assert result["dsr_n_trials_basis"] == 129 + 3 * 4


def test_sg2_only_fail_and_sg3_only_fail(tmp_path):
    # net10 beats v4 but net20 does not -> SG2 fails.
    window_dir = _write_window(
        tmp_path, design_nets={"v3": (0.05, -0.04, 50, 0.98)})
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert by_id["v3"]["sg1"] is True
    assert by_id["v3"]["sg2"] is False
    assert by_id["v3"]["verdict"] == "FAIL"
    # net10 exactly 0 -> SG3 fails (needs > 0), even though it beats v4.
    window_dir = _write_window(
        tmp_path / "second",
        design_nets={"v2": (0.0, -0.01, 40, 0.97)})
    out_dir = tmp_path / "out2"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert by_id["v2"]["sg1"] is True
    assert by_id["v2"]["sg3"] is False
    assert by_id["v2"]["verdict"] == "FAIL"


def test_fewer_than_10_trades_is_inconclusive_not_fail(tmp_path):
    window_dir = _write_window(
        tmp_path, design_nets={"h7": (0.20, 0.18, 5, 0.99)})
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert by_id["h7"]["verdict"] == "INCONCLUSIVE"
    assert by_id["h7"]["sg1"] is True  # SGs still computed + reported
    assert result["candidate"] == "v3"  # INCONCLUSIVE is never a candidate


def test_no_pass_means_no_candidate(tmp_path):
    window_dir = _write_window(tmp_path, design_nets={
        did: (-0.05, -0.06, 40, 0.9) for did in gate.DESIGN_IDS})
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    assert all(e["verdict"] == "FAIL" for e in result["evaluations"])
    assert result["candidate"] is None
    assert result["n_passed"] == 0
    assert result["dsr_n_trials_basis"] == 129


# ------------------------------------------------------------- (c) shortfall

def test_minimums_unmet_shortfall_stop_exit_2(tmp_path):
    # Declared end is still Dec 31 (extension 0); the trading-day list
    # is partial (20 days) and only 25 filings are scored.
    window_dir = _write_window(tmp_path, days=BASE_DAYS[:20], n_filings=25,
                               window_end="2026-12-31")
    out_dir = tmp_path / "out"
    rc = _run(window_dir, out_dir)
    assert rc == gate.EXIT_SHORTFALL == 2
    shorts = sorted(out_dir.glob("exp-fresh-validation-v1-shortfall-*.json"))
    assert len(shorts) == 1
    shortfall = json.loads(shorts[0].read_text())
    assert shortfall["gate_read"] is False
    assert shortfall["minimums"]["trading_days"] == {"have": 20, "required": 30, "ok": False}
    assert shortfall["minimums"]["filings"] == {"have": 25, "required": 40, "ok": False}
    assert "Extend the window by 10 trading days (extension 1 of 3)" in shortfall["next_step"]
    assert shorts[0].with_suffix(".md").exists()
    # No gate read, no verdict: no gate report was written.
    assert list(out_dir.glob("exp-fresh-validation-v1-gate-*")) == []


def test_minimums_unmet_after_third_extension_reports_stop(tmp_path):
    window_dir = _write_window(tmp_path, days=EXT3_DAYS[:20], n_filings=25,
                               extension=3, window_end="2027-01-29")
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_SHORTFALL
    shorts = sorted(out_dir.glob("exp-fresh-validation-v1-shortfall-*.json"))
    shortfall = json.loads(shorts[0].read_text())
    assert shortfall["extension"] == 3
    assert "Already at extension 3 of 3" in shortfall["next_step"]
    assert "do NOT run the gate" in shortfall["next_step"]


def test_minimums_boundary_exactly_met(tmp_path):
    window_dir = _write_window(tmp_path, days=BASE_DAYS[:30], n_filings=40,
                               window_end="2026-12-31")
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    assert result["minimums"]["met"] is True
    assert result["n_trading_days"] == 30
    assert result["n_filings_scored"] == 40


def test_unscored_filings_do_not_count(tmp_path):
    window_dir = _write_window(tmp_path, n_filings=45)
    manifest_path = window_dir / "window.json"
    manifest = json.loads(manifest_path.read_text())
    for f in manifest["filings"][:10]:
        f["haiku_score"] = None
    manifest_path.write_text(json.dumps(manifest, indent=2))
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_SHORTFALL
    shortfall = json.loads(sorted(
        out_dir.glob("exp-fresh-validation-v1-shortfall-*.json"))[0].read_text())
    assert shortfall["minimums"]["filings"]["have"] == 35
    assert shortfall["minimums"]["unscored_in_window"] == 10


# ------------------------------------------------------------- (d) H7 audit

def test_h7_sharpe_over_5_is_audit_not_pass(tmp_path):
    window_dir = _write_window(tmp_path, h7_sharpe=9.22)
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, path = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    h7 = by_id["h7"]
    assert h7["sg1"] is True and h7["sg2"] is True and h7["sg3"] is True
    assert h7["audit"] is True
    assert h7["verdict"] == "AUDIT"  # NOT reported as a pass
    assert h7["verdict"] != "PASS"
    assert result["candidate"] == "v3"  # AUDIT is never a candidate
    assert result["n_passed"] == 4  # AUDIT does not count as confirmed
    md = path.with_suffix(".md").read_text()
    assert "AUDIT" in md
    assert "data/construction audit required" in md


def test_h7_sharpe_over_5_with_sg_fail_is_fail_with_audit_flag(tmp_path):
    window_dir = _write_window(tmp_path, h7_sharpe=9.22,
                               design_nets={"h7": (-0.03, -0.04, 30, 0.99)})
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert by_id["h7"]["verdict"] == "FAIL"  # the SG failure dominates
    assert by_id["h7"]["audit"] is True  # ...but the audit flag is still recorded


def test_h7_sharpe_exactly_5_does_not_trigger_audit(tmp_path):
    window_dir = _write_window(tmp_path, h7_sharpe=5.0)
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    assert by_id["h7"]["verdict"] == "PASS"
    assert by_id["h7"]["audit"] is False


# ------------------------------------------------------------- (e) extensions

def test_extension_reported_and_second_extension_reads_gate(tmp_path):
    window_dir = _write_window(tmp_path, days=EXT2_DAYS, extension=2,
                               window_end="2027-01-15")
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    assert result["extension"] == 2
    assert result["window_end"] == "2027-01-15"
    assert result["candidate"] == "v3"


def test_fourth_extension_refused(tmp_path):
    window_dir = _write_window(tmp_path, days=EXT2_DAYS, extension=4,
                               window_end="2027-01-15")
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID == 3
    assert list(out_dir.glob("exp-fresh-validation-v1-*")) == []


def test_extension_zero_requires_base_end(tmp_path):
    window_dir = _write_window(tmp_path, days=EXT2_DAYS, extension=0,
                               window_end="2027-01-15")
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID
    assert list(out_dir.glob("exp-fresh-validation-v1-*")) == []


# ------------------------------------------------------------- SG4

def test_sg4_significantly_negative_fails_h3(tmp_path):
    window_dir = _write_window(tmp_path, h3_mode="negative")
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    by_id = {e["design_id"]: e for e in result["evaluations"]}
    h3 = by_id["h3"]
    assert h3["sg1"] is True and h3["sg2"] is True and h3["sg3"] is True
    assert h3["sg4"] is False
    assert h3["verdict"] == "FAIL"  # SG4 fails H3 regardless of SG1-SG3
    assert "SG4 fails H3" in h3["reason"]
    ic = result["h3_similarity_ic"]
    assert ic["mean_ic"] == pytest.approx(-1.0)
    assert ic["significant_negative"] is True
    # The -inf t-stat (std 0 across days) is sanitized to null in the
    # written JSON, with the note explaining why.
    assert ic["ic_tstat"] is None
    assert "std=0" in (ic["ic_tstat_note"] or "")
    assert result["candidate"] == "v3"


def test_sg4_ic_matches_pandas_rank_corr(tmp_path):
    """Cross-check the reader's stdlib rank-IC against pandas on the
    same synthetic panels (independent code path, same math)."""
    import pandas as pd
    days = BASE_DAYS
    obs = _observations(days, mode="null", seed=11)
    got = gate.similarity_ic(obs)
    feat = pd.DataFrame(index=days, columns=[f"SYM{i:02d}" for i in range(25)], dtype=float)
    fwd = feat.copy()
    for o in obs:
        feat.at[o["date"], o["ticker"]] = o["similarity"]
        fwd.at[o["date"], o["ticker"]] = o["fwd20_return"]
    ics = []
    for day in days:
        f, r = feat.loc[day].dropna(), fwd.loc[day].dropna()
        both = f.index.intersection(r.index)
        if len(both) < 20:
            continue
        ic = float(f.loc[both].rank().corr(r.loc[both].rank(), method="pearson"))
        if not math.isnan(ic):
            ics.append(ic)
    assert got["n_days"] == len(ics)
    assert got["mean_ic"] == pytest.approx(sum(ics) / len(ics), abs=1e-3)
    mean = sum(ics) / len(ics)
    std = (sum((x - mean) ** 2 for x in ics) / (len(ics) - 1)) ** 0.5
    assert got["ic_tstat"] == pytest.approx(mean / (std / math.sqrt(len(ics))), abs=1e-6)


def test_sg4_identical_daily_ics_do_not_crash(tmp_path):
    """Every usable day IC == -1.0 (std 0): the t-stat is +/-infinity,
    handled without crashing, and the negative IC is significant."""
    obs = _observations(BASE_DAYS, mode="negative")
    ic = gate.similarity_ic(obs)
    assert ic["mean_ic"] == pytest.approx(-1.0)
    assert ic["ic_tstat"] == -math.inf  # Python-side: maximal evidence
    assert ic["significant_negative"] is True
    assert "std=0" in (ic["ic_tstat_note"] or "")
    # ...and the report writer sanitizes the -inf to strict-JSON null.
    assert gate._sanitize(ic)["ic_tstat"] is None


def test_sg4_too_few_usable_days_is_invalid_window(tmp_path):
    obs = _observations(BASE_DAYS[:1], mode="null")  # a single day only
    with pytest.raises(gate.InsufficientSG4Data):
        gate.similarity_ic(obs)
    # ...and through main() it is exit 3, with no report written.
    window_dir = _write_window(tmp_path)
    (window_dir / "h3_similarity.json").write_text(json.dumps(
        {"observations": _observations(BASE_DAYS[:1], mode="null")}))
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID
    assert list(out_dir.glob("exp-fresh-validation-v1-*")) == []


# ------------------------------------------------------------- staleness guards

def test_frozen_holdout_window_is_refused(tmp_path):
    window_dir = _write_window(tmp_path)
    manifest_path = window_dir / "window.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["window_start"] = "2026-08-18"
    manifest["window_end"] = "2026-11-13"
    manifest["trading_days"] = _trading_days(date(2026, 8, 18), date(2026, 11, 13))
    manifest_path.write_text(json.dumps(manifest, indent=2))
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID
    assert list(out_dir.glob("exp-fresh-validation-v1-*")) == []


def test_stale_v4_base_window_mismatch_is_refused(tmp_path):
    window_dir = _write_window(tmp_path)
    v4_path = window_dir / "v4_base.json"
    v4 = json.loads(v4_path.read_text())
    v4["window_end"] = "2026-11-13"  # the frozen holdout's end
    v4_path.write_text(json.dumps(v4, indent=2))
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID
    assert list(out_dir.glob("exp-fresh-validation-v1-*")) == []


def test_design_file_window_mismatch_is_refused(tmp_path):
    window_dir = _write_window(tmp_path)
    d_path = window_dir / "designs" / "v2.json"
    d = json.loads(d_path.read_text())
    d["window_start"] = "2026-08-18"
    d_path.write_text(json.dumps(d, indent=2))
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID


def test_missing_design_file_is_refused(tmp_path):
    window_dir = _write_window(tmp_path)
    (window_dir / "designs" / "ml-ranker-v1.json").unlink()
    out_dir = tmp_path / "out"
    assert _run(window_dir, out_dir) == gate.EXIT_INVALID
    assert list(out_dir.glob("exp-fresh-validation-v1-*")) == []


# ------------------------------------------------------------- path safety

def test_reader_only_opens_paths_inside_the_window(tmp_path):
    window_dir = _write_window(tmp_path)
    out_dir = tmp_path / "out"
    before = _hash_tree(window_dir)
    assert _run(window_dir, out_dir) == gate.EXIT_OK
    result, _ = _read_report(out_dir)
    # Every path the reader opened is inside the tmp window dir.
    for p in result["opened_paths"]:
        assert str(p).startswith(str(window_dir.resolve())), p
    # None is a forbidden real path (frozen holdout results, real
    # window data, scores, EDGAR manifest).
    opened = {Path(p).resolve() for p in result["opened_paths"]}
    for forbidden in FORBIDDEN_PATHS:
        assert forbidden.resolve() not in opened
    # The reader never wrote into the window dir it read.
    assert _hash_tree(window_dir) == before
    # ...and never wrote anywhere except the out dir.
    assert list(out_dir.glob("exp-fresh-validation-v1-gate-*"))


def test_out_dir_inside_window_dir_is_refused(tmp_path):
    window_dir = _write_window(tmp_path)
    before = _hash_tree(window_dir)
    rc = gate.main(["--window-dir", str(window_dir),
                    "--out-dir", str(window_dir / "out")])
    assert rc == gate.EXIT_INVALID
    assert _hash_tree(window_dir) == before


def test_data_dir_alias_and_disagreement(tmp_path):
    window_dir = _write_window(tmp_path)
    out_dir = tmp_path / "out"
    assert gate.main(["--data-dir", str(window_dir),
                      "--out-dir", str(out_dir)]) == gate.EXIT_OK
    other = _write_window(tmp_path / "other")
    with pytest.raises(SystemExit) as exc:
        gate.main(["--window-dir", str(window_dir),
                   "--data-dir", str(other), "--out-dir", str(out_dir)])
    assert exc.value.code == gate.EXIT_INVALID


def test_missing_window_dir_is_invalid(tmp_path):
    assert gate.main(["--window-dir", str(tmp_path / "nope"),
                      "--out-dir", str(tmp_path / "out")]) == gate.EXIT_INVALID
