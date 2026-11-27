#!/usr/bin/env python3
"""B14 — January fresh-validation gate reader.

Reads the gate for exp-fresh-validation-v1 (registered design:
experiments/exp-fresh-validation-v1-design.md; completion runbook:
experiments/exp-fresh-validation-v1-runbook-jan2027.md) from a
fresh-validation WINDOW DIRECTORY produced by the January run
(Steps 1-5 of the runbook), and writes a dated gate report. It does
NOT run any backtest, score any filing, or touch the frozen holdout.

Gate rules (design doc, quoted thresholds — no number invented here):

  Minimums (checked FIRST; both must hold, else STOP, no gate read):
    >= 30 trading days AND >= 40 in-scope filings with fresh Haiku
    scores (filing date inside the window). Per-design: < 10 trades
    is INCONCLUSIVE, not PASS/FAIL.
  Extension rule: extend by 10 trading days, max 3 extensions
    (latest end ~= 2027-01-29). Still unmet after the third ->
    report the shortfall and STOP. This reader does not extend the
    window itself; it reads the extension number from the window
    manifest, reports it, and refuses a 4th (extension > 3 is an
    invalid window).
  SG1: fresh net return @10bps > fresh v4 base @10bps.
  SG2: fresh net return @20bps > fresh v4 base @20bps.
  SG3: fresh net return @10bps > 0.
  SG4 (H3 only): Item 1A similarity IC @20d significantly negative
    (IC < 0 with |t| >= 2) -> H3 FAILS regardless of SG1-SG3.
  H7 audit rule (NOT a gate per the design doc): H7 Sharpe > 5
    triggers a data/construction audit BEFORE any paper-trading
    decision. See the AUDIT verdict below — this reader reports it
    as a separate verdict, not a PASS and not a FAIL.
  Deflated Sharpe: n_trials continues from 129, +3 per design
    confirmed on the fresh window. A pass with DSR < 0.95 or net20
    <= 0 is flagged "pass, weak".

What "recompute the v4 base fresh" means here (read this before
assuming the reader re-runs the backtest): the runbook assigns the
v4 recomputation to Step 4 (the backtest run, same construction as
rounds 1/2, extended to the fresh window) and the gate computation
to Step 6 (this reader). The reader therefore loads the fresh-window
v4 result from the window directory and VERIFIES it is fresh — its
window must match the window manifest exactly, and its
experiment_id must be exp-technical-v2. A v4 file carrying the
frozen-holdout window (2026-08-18..2026-11-13) or the frozen numbers
as a stale file is rejected. The reader never re-runs the panel
backtest; re-running it here would duplicate backtest.py and risk
diverging from the Step 4 construction. The SG4 IC, by contrast, IS
recomputed by this reader from the window's similarity observations
(the statistic is small, self-contained, and the design doc says
"recompute").

H7 audit verdict (judgment call — the design doc and the build brief
do not agree in wording, so this is stated, not silently resolved):
the design doc calls the audit rule "not a gate", while the brief
requires Sharpe > 5 to be "NOT reported as a pass". This reader
resolves it as a fourth verdict, AUDIT: SG1-SG3 are still evaluated
and reported truthfully, but the design's overall verdict is AUDIT
(it is neither a PASS nor a FAIL — a FAIL would mean "dropped per
the kill rule", which the doc does not say), it is excluded from
paper-trading candidacy, and the audit flag is set. If H7 fails any
of SG1-SG3, the verdict is FAIL (the SG failure dominates) and the
audit flag is still recorded.

SG4 when the IC cannot be computed (judgment call — the design doc
is silent): if fewer than 2 days have >= 20 symbols with both a
similarity and a complete 20d forward return, the t-stat does not
exist and SG4 cannot be evaluated. The reader treats that as an
invalid window (exit 3) rather than passing or failing H3 on no
evidence: SG4 is a required gate for H3, so an unreadable SG4 means
the window is not gate-ready.

Window directory contract (produced by the January run, Steps 1-5):

  <window-dir>/
    window.json          manifest: window_start, window_end, base_end,
                         extension (0-3), trading_days (ISO dates),
                         filings ([{ticker, filing_date, form,
                         haiku_score|null}])
    v4_base.json         {experiment_id: "exp-technical-v2",
                         window_start, window_end,
                         cost_0bps, cost_10bps, cost_20bps}
    designs/<id>.json    one per registered design (v2, v3, h3, h7,
                         ml-ranker-v1): {design_id, design_doc,
                         window_start, window_end,
                         cost_0bps, cost_10bps, cost_20bps}
    h3_similarity.json   {observations: [{date, ticker, similarity,
                         fwd20_return}]} — similarity is the YoY
                         Jaccard of Item 1A Risk Factors text vs the
                         prior same-form filing (1.0 = no-change
                         default), carried forward to each trading
                         day; fwd20_return is the engine-consistent
                         20d forward return from d+1 (same definition
                         as analyze_sprint_patterns.fwd_returns).

Per-cost dicts use the backtest results shape (same keys as
experiments/results/exp-llm-long-horizon-v2.json): total_return,
sharpe, max_drawdown, hit_rate, trades, avg_daily_turnover,
deflated_sharpe, sharpe_null_max, alpha_annual_vs_spy, beta_vs_spy,
r_squared_vs_spy. Only total_return and trades are required; the
rest are reported when present. DSR values are read from the design
files as computed by the backtest runner — this reader does not
recompute DSR (it has no daily returns series); it reports the
n_trials basis (129 + 3 per PASS) alongside.

The reader NEVER writes into the window directory it reads: all
outputs go to --out-dir (default experiments/results). It opens only
files under the window directory (recorded in the result as
opened_paths, so tests can assert the boundary). It never opens the
frozen-holdout results, the EDGAR dataset, or the scores files.

Exit codes: 0 = gate read complete, report written. 2 = minimums
unmet: shortfall report written, STOP, no gate read, no verdict.
3 = invalid window / data / invocation (bad extension, window
mismatch, missing files, uncomputable SG4, out-dir inside the
window dir). Usage errors also exit 3, so 2 means exactly
"shortfall STOP" and nothing else.

Usage:
  .venv/bin/python scripts/read_fresh_validation_gate.py \
      --window-dir <dir> [--out-dir <dir>]

$0 LLM spend. No orders, no live trading, ALLOW_LIVE stays 0.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# --- Registered constants (design doc; do not change) -------------------
WINDOW_START = "2026-11-16"          # first trading day after the frozen holdout
BASE_END = "2026-12-31"              # default window end, fixed before any gate read
FROZEN_HOLDOUT_START = "2026-08-18"  # the frozen holdout — never read, only rejected
FROZEN_HOLDOUT_END = "2026-11-13"
MIN_TRADING_DAYS = 30
MIN_FILINGS = 40
MIN_TRADES_PER_DESIGN = 10
MAX_EXTENSIONS = 3
EXTENSION_TRADING_DAYS = 10
N_TRIALS_BASE = 129
N_TRIALS_PER_CONFIRMED = 3
H7_AUDIT_SHARPE = 5.0
SG4_MAX_P_T = 2.0                    # |t| >= 2 with IC < 0 -> SG4 fails H3
DSR_WEAK_BELOW = 0.95
V4_EXPERIMENT_ID = "exp-technical-v2"

# Registered designs, in runbook order. sg4 applies to H3 only;
# the audit rule applies to H7 only.
DESIGNS = [
    {"id": "v2", "name": "exp-llm-long-horizon-v2",
     "design_doc": "experiments/exp-llm-long-horizon-v2-design.md"},
    {"id": "v3", "name": "exp-llm-long-horizon-v3",
     "design_doc": "experiments/exp-llm-long-horizon-v3-design.md"},
    {"id": "h3", "name": "H3 lazy-veto-v3",
     "design_doc": "experiments/exp-llm-long-horizon-r2-design.md"},
    {"id": "h7", "name": "H7 C1c reversion+RSI",
     "design_doc": "experiments/exp-llm-long-horizon-r2-design.md"},
    {"id": "ml-ranker-v1", "name": "exp-ml-ranker-v1",
     "design_doc": "experiments/exp-ml-ranker-v1-design.md"},
]
DESIGN_IDS = [d["id"] for d in DESIGNS]

EXIT_OK = 0
EXIT_SHORTFALL = 2
EXIT_INVALID = 3

# Minimum cross-section per day for the SG4 rank IC — same as
# analyze_sprint_patterns.rank_ic_and_spread (days with fewer are skipped).
SG4_MIN_SYMBOLS_PER_DAY = 20


class InvalidWindow(Exception):
    """The window directory is not gate-ready (exit 3)."""


class InsufficientSG4Data(InvalidWindow):
    """SG4 cannot be computed from the window's similarity data."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Usage errors exit 3, so exit 2 means exactly "shortfall STOP".
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        raise SystemExit(EXIT_INVALID)


# ------------------------------------------------------------------ helpers

def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) \
        and math.isfinite(float(x))


def _sanitize(obj):
    """Recursively replace non-finite floats (inf/nan) with None so
    the JSON output is strict JSON. The significance decision is
    always carried in a separate boolean, never in the number."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _parse_date(s, what):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise InvalidWindow(f"{what} is not a YYYY-MM-DD date: {s!r}")


def _rank(values):
    """Average ranks (1-based), ties share the mean rank. Pure stdlib;
    identical semantics to pandas Series.rank() (default average)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def similarity_ic(observations):
    """Item 1A similarity IC @20d, recomputed exactly as rounds 1/2
    (analyze_sprint_patterns.rank_ic_and_spread): per day, the
    Spearman rank correlation between carried-forward Jaccard
    similarity and the 20d forward return across that day's symbols
    (days with < 20 symbols skipped); mean IC across usable days and
    t = mean / (std(ddof=1) / sqrt(n_days)).

    observations: [{date, ticker, similarity, fwd20_return}]; rows
    with a null similarity or fwd20_return are dropped pairwise.

    Returns {n_days, mean_ic, ic_tstat, mean_qspread, significant_negative}.
    Raises InsufficientSG4Data when fewer than 2 usable days exist
    (the t-stat does not exist, so SG4 cannot be evaluated).
    """
    by_day = {}
    for obs in observations:
        sim = obs.get("similarity")
        fwd = obs.get("fwd20_return")
        if sim is None or fwd is None:
            continue
        if not _is_number(sim) or not _is_number(fwd):
            raise InvalidWindow(
                f"h3_similarity observation has a non-numeric value: {obs!r}")
        by_day.setdefault(str(obs.get("date")), []).append((float(sim), float(fwd)))
    ics, spreads = [], []
    for day in sorted(by_day):
        pairs = by_day[day]
        if len(pairs) < SG4_MIN_SYMBOLS_PER_DAY:
            continue
        sims = [p[0] for p in pairs]
        fwds = [p[1] for p in pairs]
        ic = _pearson(_rank(sims), _rank(fwds))
        if ic is None:
            continue
        ics.append(ic)
        order = sorted(range(len(pairs)), key=lambda i: sims[i])
        q = max(1, len(order) // 5)
        top = [fwds[i] for i in order[-q:]]
        bot = [fwds[i] for i in order[:q]]
        spreads.append(sum(top) / len(top) - sum(bot) / len(bot))
    if len(ics) < 2:
        raise InsufficientSG4Data(
            f"SG4 IC needs >= 2 usable days (>= {SG4_MIN_SYMBOLS_PER_DAY} symbols "
            f"with similarity + complete fwd20 each); found {len(ics)}. The window "
            f"is not gate-ready for H3.")
    mean_ic = sum(ics) / len(ics)
    if len(ics) > 1:
        var = sum((x - mean_ic) ** 2 for x in ics) / (len(ics) - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    if std == 0.0:
        # Every usable day gave the same IC: the evidence is maximal,
        # not absent — the t-stat is +/-infinity, not undefined.
        tstat = (math.inf if mean_ic > 0 else -math.inf) if mean_ic != 0 else 0.0
    else:
        tstat = mean_ic / (std / math.sqrt(len(ics)))
    significant_negative = mean_ic < 0 and abs(tstat) >= SG4_MAX_P_T
    return {
        "n_days": len(ics),
        "mean_ic": round(mean_ic, 4),
        "ic_tstat": tstat,
        "ic_tstat_note": ("all usable days gave the identical IC "
                          "(std=0); t-stat is +/-infinity, reported as null "
                          "in JSON") if std == 0.0 and mean_ic != 0 else None,
        "mean_qspread": round(sum(spreads) / len(spreads), 5),
        "significant_negative": significant_negative,
    }


# ------------------------------------------------------------------ loading

class WindowReader:
    """Loads a window directory, recording every path opened so tests
    can assert the reader stayed inside the window dir (and never
    touched the frozen holdout or the real window)."""

    def __init__(self, window_dir):
        self.window_dir = Path(window_dir).resolve()
        self.opened_paths = []

    def _open_json(self, rel):
        path = (self.window_dir / rel).resolve()
        if not str(path).startswith(str(self.window_dir)):
            raise InvalidWindow(f"refusing to open a path outside the window dir: {rel}")
        self.opened_paths.append(str(path))
        try:
            with open(path) as f:
                return json.load(f)
        except FileNotFoundError:
            raise InvalidWindow(f"missing required window file: {rel}")
        except json.JSONDecodeError as e:
            raise InvalidWindow(f"window file {rel} is not valid JSON: {e}")

    def load(self):
        manifest = self._open_json("window.json")
        for field in ("window_start", "window_end", "base_end", "extension",
                      "trading_days", "filings"):
            if field not in manifest:
                raise InvalidWindow(f"window.json is missing required field: {field}")
        w_start = _parse_date(manifest["window_start"], "window_start")
        w_end = _parse_date(manifest["window_end"], "window_end")
        base_end = _parse_date(manifest["base_end"], "base_end")
        if manifest["window_start"] != WINDOW_START:
            raise InvalidWindow(
                f"window_start is {manifest['window_start']}, not the registered "
                f"{WINDOW_START} — this is not the exp-fresh-validation-v1 window")
        if manifest["base_end"] != BASE_END:
            raise InvalidWindow(
                f"base_end is {manifest['base_end']}, not the registered {BASE_END}")
        if (manifest["window_start"] == FROZEN_HOLDOUT_START
                and manifest["window_end"] == FROZEN_HOLDOUT_END):
            raise InvalidWindow(
                "window is the frozen holdout "
                f"({FROZEN_HOLDOUT_START}..{FROZEN_HOLDOUT_END}) — the gate reader "
                "never reads the frozen holdout")
        ext = manifest["extension"]
        if not isinstance(ext, int) or isinstance(ext, bool) \
                or ext < 0 or ext > MAX_EXTENSIONS:
            raise InvalidWindow(
                f"extension is {ext!r}; the design allows 0..{MAX_EXTENSIONS} "
                f"(a 4th extension is refused — see the design doc)")
        days = manifest["trading_days"]
        if not isinstance(days, list) or not days \
                or any(not isinstance(d, str) for d in days):
            raise InvalidWindow("trading_days must be a non-empty list of ISO dates")
        parsed_days = [_parse_date(d, "trading_days entry") for d in days]
        if parsed_days != sorted(parsed_days) or len(set(days)) != len(days):
            raise InvalidWindow("trading_days must be sorted and unique")
        if days[0] != manifest["window_start"]:
            raise InvalidWindow("trading_days must start at window_start")
        if parsed_days[-1] > w_end:
            raise InvalidWindow(
                "trading_days must not run past window_end "
                f"({manifest['window_end']}); last listed day is {days[-1]}. "
                f"A partial list (last day before window_end) is allowed — "
                f"it is what makes the minimums check fail with a shortfall "
                f"instead of an error.")
        if ext == 0 and manifest["window_end"] != BASE_END:
            raise InvalidWindow(
                f"extension 0 requires window_end == base_end ({BASE_END}), "
                f"got {manifest['window_end']}")
        if ext > 0 and w_end <= base_end:
            raise InvalidWindow(
                f"extension {ext} requires window_end after base_end ({BASE_END}), "
                f"got {manifest['window_end']}")
        filings = manifest["filings"]
        if not isinstance(filings, list):
            raise InvalidWindow("filings must be a list")
        n_filings = 0
        n_unscored = 0
        n_outside = 0
        for fl in filings:
            try:
                fdate = _parse_date(fl["filing_date"], "filing filing_date")
            except (KeyError, InvalidWindow):
                raise InvalidWindow(f"filing record is missing a valid filing_date: {fl!r}")
            if not (w_start <= fdate <= w_end):
                n_outside += 1
                continue
            if fl.get("haiku_score") is None:
                n_unscored += 1
                continue
            if not _is_number(fl["haiku_score"]):
                raise InvalidWindow(f"filing haiku_score is not a number: {fl!r}")
            n_filings += 1
        window = {
            "manifest": manifest,
            "window_start": manifest["window_start"],
            "window_end": manifest["window_end"],
            "base_end": manifest["base_end"],
            "extension": ext,
            "trading_days": days,
            "n_trading_days": len(days),
            "n_filings_scored": n_filings,
            "n_filings_unscored_in_window": n_unscored,
            "n_filings_outside_window": n_outside,
            "opened_paths": self.opened_paths,
        }
        return window


def _require_costs(obj, what):
    """Validate a per-cost result dict; return the three cost dicts."""
    costs = {}
    for key in ("cost_0bps", "cost_10bps", "cost_20bps"):
        c = obj.get(key)
        if not isinstance(c, dict):
            raise InvalidWindow(f"{what} is missing {key}")
        if not _is_number(c.get("total_return")):
            raise InvalidWindow(f"{what} {key} is missing a numeric total_return")
        if not isinstance(c.get("trades"), int) or isinstance(c.get("trades"), bool):
            raise InvalidWindow(f"{what} {key} is missing an integer trades count")
        costs[key] = c
    return costs


def load_v4_base(reader, window):
    v4 = reader._open_json("v4_base.json")
    if v4.get("experiment_id") != V4_EXPERIMENT_ID:
        raise InvalidWindow(
            f"v4_base.json experiment_id is {v4.get('experiment_id')!r}, not "
            f"{V4_EXPERIMENT_ID!r} — this is not the fresh v4 technical base")
    if v4.get("window_start") != window["window_start"] \
            or v4.get("window_end") != window["window_end"]:
        raise InvalidWindow(
            f"v4_base.json covers {v4.get('window_start')}..{v4.get('window_end')}, "
            f"not the window {window['window_start']}..{window['window_end']} — "
            f"a stale or frozen-holdout base is refused; the base must be "
            f"recomputed fresh on this window (runbook Step 4)")
    if "trading_days" in v4 and v4["trading_days"] != window["n_trading_days"]:
        raise InvalidWindow(
            f"v4_base.json trading_days ({v4['trading_days']}) does not match the "
            f"window ({window['n_trading_days']})")
    return _require_costs(v4, "v4_base.json")


def load_design(reader, window, design_id):
    d = reader._open_json(f"designs/{design_id}.json")
    if d.get("design_id") != design_id:
        raise InvalidWindow(
            f"designs/{design_id}.json design_id is {d.get('design_id')!r}")
    if d.get("window_start") != window["window_start"] \
            or d.get("window_end") != window["window_end"]:
        raise InvalidWindow(
            f"designs/{design_id}.json covers {d.get('window_start')}.."
            f"{d.get('window_end')}, not the window "
            f"{window['window_start']}..{window['window_end']}")
    return _require_costs(d, f"designs/{design_id}.json")


# ------------------------------------------------------------------ gates

def check_minimums(window):
    """Both minimums must hold, else the window is extended, not run."""
    days_ok = window["n_trading_days"] >= MIN_TRADING_DAYS
    filings_ok = window["n_filings_scored"] >= MIN_FILINGS
    return {
        "met": days_ok and filings_ok,
        "trading_days": {"have": window["n_trading_days"],
                         "required": MIN_TRADING_DAYS, "ok": days_ok},
        "filings": {"have": window["n_filings_scored"],
                    "required": MIN_FILINGS, "ok": filings_ok},
        "unscored_in_window": window["n_filings_unscored_in_window"],
        "outside_window": window["n_filings_outside_window"],
    }


def evaluate_design(design_id, costs, v4_costs, h3_ic):
    """SG1-SG3 (+SG4 for H3) against the fresh v4 base. Returns the
    per-design verdict dict. Verdicts: PASS, FAIL, INCONCLUSIVE
    (< 10 trades), AUDIT (H7 only: SG1-SG3 pass but Sharpe > 5)."""
    net0 = costs["cost_0bps"]["total_return"]
    net10 = costs["cost_10bps"]["total_return"]
    net20 = costs["cost_20bps"]["total_return"]
    v4_net10 = v4_costs["cost_10bps"]["total_return"]
    v4_net20 = v4_costs["cost_20bps"]["total_return"]
    trades = costs["cost_10bps"]["trades"]
    sharpe10 = costs["cost_10bps"].get("sharpe")
    dsr = costs["cost_10bps"].get("deflated_sharpe")
    sg1 = net10 > v4_net10
    sg2 = net20 > v4_net20
    sg3 = net10 > 0
    sg4 = None
    sg4_reason = None
    if design_id == "h3":
        if h3_ic is None:
            raise InvalidWindow("H3 requires the SG4 similarity IC, which was not computed")
        sg4 = not h3_ic["significant_negative"]
        sg4_reason = (
            f"Item 1A similarity IC @20d {h3_ic['mean_ic']:+.4f} "
            f"(t {h3_ic['ic_tstat']}, n_days {h3_ic['n_days']}) — "
            f"{'significantly negative: veto premise contradicted again' if h3_ic['significant_negative'] else 'not significantly negative'}")
    audit = False
    audit_reason = None
    if design_id == "h7" and _is_number(sharpe10) and sharpe10 > H7_AUDIT_SHARPE:
        audit = True
        audit_reason = (
            f"H7 fresh-window Sharpe {sharpe10} exceeds {H7_AUDIT_SHARPE}: "
            f"data/construction audit required (re-verify the panel, the price "
            f"join, and the trade list) BEFORE any paper-trading decision")
    if trades < MIN_TRADES_PER_DESIGN:
        verdict = "INCONCLUSIVE"
        reason = (f"only {trades} trades in the window (< {MIN_TRADES_PER_DESIGN}); "
                  f"too few observations — not PASS/FAIL")
    elif design_id == "h3" and not sg4:
        verdict = "FAIL"
        reason = f"SG4 fails H3 regardless of SG1-SG3: {sg4_reason}"
    elif not (sg1 and sg2 and sg3):
        failed = [n for n, ok in (("SG1", sg1), ("SG2", sg2), ("SG3", sg3)) if not ok]
        verdict = "FAIL"
        reason = f"{', '.join(failed)} failed"
    elif audit:
        verdict = "AUDIT"
        reason = audit_reason
    else:
        verdict = "PASS"
        reason = "SG1-SG3 all pass" + ("; SG4 passes" if design_id == "h3" else "")
    pass_weak = False
    weak_reason = None
    if verdict == "PASS":
        weak_bits = []
        if _is_number(dsr) and dsr < DSR_WEAK_BELOW:
            weak_bits.append(f"DSR {dsr} < {DSR_WEAK_BELOW}")
        if net20 <= 0:
            weak_bits.append(f"net20 {net20:+.2%} <= 0")
        if weak_bits:
            pass_weak = True
            weak_reason = "pass, weak: " + "; ".join(weak_bits) + \
                " — needs a longer validation or reduced size"
    return {
        "design_id": design_id,
        "net0": net0, "net10": net10, "net20": net20,
        "v4_net10": v4_net10, "v4_net20": v4_net20,
        "sg1": sg1, "sg2": sg2, "sg3": sg3, "sg4": sg4,
        "sg4_detail": sg4_reason,
        "trades": trades,
        "sharpe10": sharpe10,
        "sharpe": sharpe10,
        "max_drawdown10": costs["cost_10bps"].get("max_drawdown"),
        "hit_rate10": costs["cost_10bps"].get("hit_rate"),
        "turnover10": costs["cost_10bps"].get("avg_daily_turnover"),
        "deflated_sharpe": dsr,
        "alpha_annual_vs_spy": costs["cost_10bps"].get("alpha_annual_vs_spy"),
        "beta_vs_spy": costs["cost_10bps"].get("beta_vs_spy"),
        "audit": audit, "audit_reason": audit_reason,
        "verdict": verdict, "reason": reason,
        "pass_weak": pass_weak, "weak_reason": weak_reason,
    }


def decide_candidate(evaluations):
    """The passing design with the best fresh-window net10 becomes the
    paper-trading CANDIDATE. Candidacy is not authorization. AUDIT,
    INCONCLUSIVE and FAIL designs are never candidates."""
    passing = [e for e in evaluations if e["verdict"] == "PASS"]
    if not passing:
        return None
    return max(passing, key=lambda e: e["net10"])["design_id"]


# ------------------------------------------------------------------ reports

def write_gate_report(result, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"exp-fresh-validation-v1-gate-{result['window_end'].replace('-', '')}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    result["files"] = {"json": str(json_path), "md": str(md_path)}
    json_path.write_text(json.dumps(_sanitize(result), indent=2) + "\n")
    md_path.write_text(gate_report_md(result))
    return json_path, md_path


def write_shortfall_report(shortfall, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"exp-fresh-validation-v1-shortfall-{shortfall['window_end'].replace('-', '')}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    shortfall["files"] = {"json": str(json_path), "md": str(md_path)}
    json_path.write_text(json.dumps(_sanitize(shortfall), indent=2) + "\n")
    lines = [
        f"# Fresh-validation shortfall — window {shortfall['window_start']}..{shortfall['window_end']}",
        "",
        "Minimums NOT met — the gate was NOT read. Per the registered design, "
        "the window is extended, not run.",
        "",
        f"- Trading days: {shortfall['minimums']['trading_days']['have']} / "
        f"{shortfall['minimums']['trading_days']['required']} "
        f"({'met' if shortfall['minimums']['trading_days']['ok'] else 'SHORT by ' + str(shortfall['minimums']['trading_days']['required'] - shortfall['minimums']['trading_days']['have'])})",
        f"- Filings with fresh Haiku scores: {shortfall['minimums']['filings']['have']} / "
        f"{shortfall['minimums']['filings']['required']} "
        f"({'met' if shortfall['minimums']['filings']['ok'] else 'SHORT by ' + str(shortfall['minimums']['filings']['required'] - shortfall['minimums']['filings']['have'])})",
        f"- Extension: {shortfall['extension']} of {MAX_EXTENSIONS} "
        f"(extend by {EXTENSION_TRADING_DAYS} trading days at a time).",
        "",
        shortfall["next_step"],
        "",
        "Machine-readable: `" + json_path.name + "`.",
        "",
    ]
    md_path.write_text("\n".join(lines))
    return json_path, md_path


def gate_report_md(result):
    lines = [
        f"# Fresh-validation gate — window {result['window_start']}..{result['window_end']}",
        "",
        f"Generated {result['generated_utc']}. Machine-readable: "
        f"`{result['files']['json']}`. Design (registered BEFORE running): "
        f"`experiments/exp-fresh-validation-v1-design.md`. Reader: "
        f"`scripts/read_fresh_validation_gate.py`.",
        "",
        f"Window: {result['window_start']}..{result['window_end']} "
        f"({result['n_trading_days']} trading days, "
        f"{result['n_filings_scored']} filings with fresh Haiku scores; "
        f"extension {result['extension']} of {MAX_EXTENSIONS}). "
        f"Minimums met: {result['minimums']['trading_days']['have']}/"
        f"{MIN_TRADING_DAYS} days, {result['minimums']['filings']['have']}/"
        f"{MIN_FILINGS} filings.",
        "",
        "## v4 base (recomputed fresh on this window)",
        "",
        f"exp-technical-v2, long-hold: net0 {result['v4_base']['net0']:+.2%}, "
        f"net10 {result['v4_base']['net10']:+.2%}, "
        f"net20 {result['v4_base']['net20']:+.2%}. The frozen-holdout v4 numbers "
        f"(-4.33% @10bps, -8.45% @20bps) are reference only and were NOT used.",
        "",
        "## Verdicts",
        "",
        "| Design | net10 | net20 | v4 net10 | SG1 | SG2 | SG3 | SG4 | Trades | Sharpe | DSR | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for e in result["evaluations"]:
        sg4 = "—" if e["sg4"] is None else ("pass" if e["sg4"] else "FAIL")
        lines.append(
            f"| {e['design_id']} | {e['net10']:+.2%} | {e['net20']:+.2%} | "
            f"{e['v4_net10']:+.2%} | {'pass' if e['sg1'] else 'FAIL'} | "
            f"{'pass' if e['sg2'] else 'FAIL'} | {'pass' if e['sg3'] else 'FAIL'} | "
            f"{sg4} | {e['trades']} | "
            f"{e['sharpe10'] if e['sharpe10'] is not None else 'n/a'} | "
            f"{e['deflated_sharpe'] if e['deflated_sharpe'] is not None else 'n/a'} | "
            f"**{e['verdict']}**{' (weak)' if e['pass_weak'] else ''} |")
    lines += [
        "",
        "## Per-design detail",
        "",
    ]
    for e in result["evaluations"]:
        lines.append(f"### {e['design_id']} — {e['verdict']}")
        lines.append("")
        lines.append(f"{e['reason']}.")
        if e["sg4_detail"]:
            lines.append(f"SG4: {e['sg4_detail']}.")
        if e["audit_reason"]:
            lines.append(f"Audit: {e['audit_reason']}.")
        if e["weak_reason"]:
            lines.append(f"{e['weak_reason']}.")
        lines.append(
            f"Diagnostics @10bps: Sharpe {e['sharpe10']}, max drawdown "
            f"{e['max_drawdown10']}, hit rate {e['hit_rate10']}, turnover "
            f"{e['turnover10']}/day, DSR {e['deflated_sharpe']}, alpha vs SPY "
            f"{e['alpha_annual_vs_spy']}/yr, beta {e['beta_vs_spy']}.")
        lines.append("")
    h3 = result.get("h3_similarity_ic")
    if h3:
        lines += [
            "## SG4 detail (H3)",
            "",
            f"Item 1A similarity IC @20d: {h3['mean_ic']:+.4f} "
            f"(t {h3['ic_tstat']}, {h3['n_days']} usable days, "
            f"mean quintile spread {h3['mean_qspread']:+.5f}). "
            f"Significantly negative: {h3['significant_negative']}.",
            "",
        ]
    lines += [
        "## Candidate",
        "",
    ]
    if result["candidate"]:
        lines.append(
            f"**{result['candidate']}** — the passing design with the best "
            f"fresh-window net10. Candidacy is NOT authorization: paper trading "
            f"still requires the user's explicit decision, with written risk "
            f"limits and a kill switch, under Phase 4 (60-day paper soak).")
    else:
        lines.append(
            "None — no design passed. All designs are demoted to "
            "\"holdout-only passes\"; no paper trading. Research returns to "
            "pattern recognition on the larger combined sample, with a NEW "
            "frozen holdout for any future test. This window is then train "
            "data, never a test set again.")
    lines += [
        "",
        "## DSR basis",
        "",
        f"n_trials continues from {N_TRIALS_BASE} and increases by "
        f"{N_TRIALS_PER_CONFIRMED} per design confirmed on the fresh window: "
        f"{result['n_passed']} passed, so the basis is "
        f"{result['dsr_n_trials_basis']}. Per-design DSR values above are as "
        f"computed by the backtest runner; this reader does not recompute DSR.",
        "",
        "## Honest caveats",
        "",
        "- This tests holdout-overfitting, NOT knowledge-cutoff "
        "contamination: filings were scored by Haiku 5.5 after the window, "
        "with training knowledge that may postdate the filings.",
        "- Universe is not survivorship-free; filing dates (not EDGAR "
        "acceptance timestamps) with a 1-bar lag; after-close filings are "
        "effectively 1 day late.",
        "- Backtest only; no paper trading, no live money, ALLOW_LIVE stays 0.",
        "",
    ]
    return "\n".join(lines)


def verdict_block(result):
    lines = [
        "",
        f"=== Fresh-validation gate — {result['window_start']}..{result['window_end']} "
        f"(extension {result['extension']} of {MAX_EXTENSIONS}) ===",
        f"minimums:  {result['n_trading_days']} trading days "
        f"(>= {MIN_TRADING_DAYS}), {result['n_filings_scored']} filings "
        f"(>= {MIN_FILINGS}) — met",
        f"v4 base:   net10 {result['v4_base']['net10']:+.2%}, "
        f"net20 {result['v4_base']['net20']:+.2%} (fresh window)",
    ]
    for e in result["evaluations"]:
        extra = ""
        if e["pass_weak"]:
            extra = " (weak)"
        if e["audit"]:
            extra = " (AUDIT REQUIRED)"
        lines.append(
            f"{e['design_id']:>12}: {e['verdict']}{extra} — net10 {e['net10']:+.2%}, "
            f"net20 {e['net20']:+.2%}, trades {e['trades']}, "
            f"SG1 {'Y' if e['sg1'] else 'n'} SG2 {'Y' if e['sg2'] else 'n'} "
            f"SG3 {'Y' if e['sg3'] else 'n'}"
            + (f" SG4 {'Y' if e['sg4'] else 'n'}" if e["sg4"] is not None else ""))
    if result["candidate"]:
        lines.append(f"candidate: {result['candidate']} (best passing net10 — "
                     f"candidacy is NOT authorization)")
    else:
        lines.append("candidate: none — no design passed")
    lines.append(f"results:   {result['files']['json']}")
    lines.append(f"           {result['files']['md']}")
    lines.append("")
    return "\n".join(lines)


def shortfall_block(shortfall):
    m = shortfall["minimums"]
    lines = [
        "",
        f"=== Fresh-validation SHORTFALL — {shortfall['window_start']}.."
        f"{shortfall['window_end']} (extension {shortfall['extension']} of "
        f"{MAX_EXTENSIONS}) ===",
        "minimums NOT met — the gate was NOT read, no verdict.",
        f"trading days: {m['trading_days']['have']} / {m['trading_days']['required']}",
        f"filings:      {m['filings']['have']} / {m['filings']['required']} "
        f"(fresh Haiku scores, in-window)",
        shortfall["next_step"],
        f"report:       {shortfall['files']['json']}",
        f"              {shortfall['files']['md']}",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ main

def main(argv=None, *, repo=REPO):
    parser = _Parser(description="Read the exp-fresh-validation-v1 gate "
                                 "from a January-run window directory.")
    parser.add_argument("--window-dir", default=None,
                        help="window directory produced by the January run "
                             "(window.json + v4_base.json + designs/ + "
                             "h3_similarity.json)")
    parser.add_argument("--data-dir", default=None,
                        help="alias for --window-dir")
    parser.add_argument("--out-dir", default=None,
                        help="directory for the dated gate/shortfall report "
                             "(default: experiments/results; never the window dir)")
    args = parser.parse_args(argv)

    window_dir = args.window_dir or args.data_dir
    if args.window_dir and args.data_dir \
            and Path(args.window_dir).resolve() != Path(args.data_dir).resolve():
        parser.error("--window-dir and --data-dir disagree")
    if not window_dir:
        parser.error("--window-dir (or --data-dir) is required")
    out_dir = Path(args.out_dir) if args.out_dir else Path(repo) / "experiments" / "results"
    window_path = Path(window_dir).resolve()
    out_path = out_dir.resolve()
    if out_path == window_path or str(out_path).startswith(str(window_path) + "/"):
        print(f"error: --out-dir ({out_dir}) must not be inside the window dir "
              f"({window_dir}) — the reader never writes into the window it reads",
              file=sys.stderr)
        return EXIT_INVALID
    if not window_path.is_dir():
        print(f"error: window dir does not exist: {window_dir}", file=sys.stderr)
        return EXIT_INVALID

    reader = WindowReader(window_path)
    try:
        window = reader.load()
    except InvalidWindow as e:
        print(f"error: invalid window: {e}", file=sys.stderr)
        return EXIT_INVALID

    minimums = check_minimums(window)
    if not minimums["met"]:
        if window["extension"] >= MAX_EXTENSIONS:
            next_step = (f"Already at extension {MAX_EXTENSIONS} of {MAX_EXTENSIONS} — "
                         "STOP. Report the shortfall and do NOT run the gate.")
        else:
            next_step = (f"Extend the window by {EXTENSION_TRADING_DAYS} trading days "
                         f"(extension {window['extension'] + 1} of {MAX_EXTENSIONS}), "
                         "refresh + score the extension, and re-check.")
        shortfall = {
            "experiment_id": "exp-fresh-validation-v1",
            "reader": "scripts/read_fresh_validation_gate.py",
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "window_start": window["window_start"],
            "window_end": window["window_end"],
            "extension": window["extension"],
            "minimums": minimums,
            "next_step": next_step,
            "gate_read": False,
        }
        json_path, md_path = write_shortfall_report(shortfall, out_path)
        print(shortfall_block(shortfall), flush=True)
        return EXIT_SHORTFALL

    try:
        v4_costs = load_v4_base(reader, window)
        designs = {}
        for d in DESIGNS:
            designs[d["id"]] = load_design(reader, window, d["id"])
        h3_data = reader._open_json("h3_similarity.json")
        if not isinstance(h3_data.get("observations"), list):
            raise InvalidWindow("h3_similarity.json is missing an observations list")
        h3_ic = similarity_ic(h3_data["observations"])
    except InvalidWindow as e:
        print(f"error: invalid window: {e}", file=sys.stderr)
        return EXIT_INVALID

    evaluations = []
    for d in DESIGNS:
        evaluations.append(evaluate_design(d["id"], designs[d["id"]], v4_costs, h3_ic))
    n_passed = sum(1 for e in evaluations if e["verdict"] == "PASS")
    result = {
        "experiment_id": "exp-fresh-validation-v1",
        "reader": "scripts/read_fresh_validation_gate.py",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "window_start": window["window_start"],
        "window_end": window["window_end"],
        "base_end": window["base_end"],
        "extension": window["extension"],
        "n_trading_days": window["n_trading_days"],
        "n_filings_scored": window["n_filings_scored"],
        "minimums": minimums,
        "v4_base": {
            "experiment_id": V4_EXPERIMENT_ID,
            "net0": v4_costs["cost_0bps"]["total_return"],
            "net10": v4_costs["cost_10bps"]["total_return"],
            "net20": v4_costs["cost_20bps"]["total_return"],
            "sharpe10": v4_costs["cost_10bps"].get("sharpe"),
            "trades": v4_costs["cost_10bps"]["trades"],
        },
        "h3_similarity_ic": _sanitize(h3_ic),
        "evaluations": evaluations,
        "n_passed": n_passed,
        "dsr_n_trials_basis": N_TRIALS_BASE + N_TRIALS_PER_CONFIRMED * n_passed,
        "candidate": decide_candidate(evaluations),
        "opened_paths": reader.opened_paths,
    }
    json_path, md_path = write_gate_report(result, out_path)
    print(verdict_block(result), flush=True)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
