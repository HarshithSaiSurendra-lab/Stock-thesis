#!/usr/bin/env python3
"""B12 — one-command re-run of exp-equibles-pilot-returns.

The November run was three manual steps (fetch prices, run the
returns test, log to the Sheet). This wrapper does them in order,
plus a generated results .md and a printed verdict block:

  1. Fetch fresh split+dividend-adjusted daily bars (Alpaca paper
     keys, data reads only) with END = --as-of (default: today).
  2. Run the returns test (run_pilot_returns.main, imported — the
     return definition, metrics, and evidence bar are NOT changed).
  3. Write NEW dated results files — never the November files.
  4. Append the result to the experiment-log Sheet, unless --no-sheet.
  5. Append the run to experiments/registry.jsonl, unless
     --no-registry (runbook Step 7: Sheet + registry + PROGRESS.md —
     the wrapper does the first two; PROGRESS.md is updated at
     commit time).
  6. Print a short verdict block (n tested/excluded, headline Q&A
     ICs, whether the pre-registered bar cleared, drop-rule outcome).

Scores are NEVER re-scored: the frozen
experiments/exp-equibles-pilot-scoring.json is read, not written.
$0 LLM spend. No orders, no live trading.

Usage:
  .venv/bin/python scripts/run_equibles_rerun.py [--as-of YYYY-MM-DD]
      [--no-sheet] [--prices-dir PATH] [--results-dir PATH]

Defaults: --as-of today, prices in experiments/equibles-pilot-raw/
(dated filenames prices-adjusted-<as-of>-<SYM>.csv — the November
prices-adjusted-<SYM>.csv files are never overwritten), results in
experiments/exp-equibles-pilot-returns-<mon><yyyy>.md/.json
(e.g. dec2026).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import fetch_pilot_prices  # noqa: E402
import log_returns_sheet  # noqa: E402
import run_pilot_returns  # noqa: E402

SYMBOLS = fetch_pilot_prices.SYMBOLS

# Drop-rule thresholds (B12). The registered evidence bar is
# |IC| >= 0.3 with p < 0.10 on the Q&A score (Pearson or Spearman,
# raw or excess) — anything weaker is "no evidence" per the design.
# The drop rule reads the bar plus the size of the largest score IC.
# "~0" means within one standard error of zero under the null
# (SE ~= 1/sqrt(n-3) for a correlation; n = 16 -> 0.28, n = 21 ->
# 0.24 — i.e. statistically indistinguishable from zero):
#   KEEP   — the bar is cleared (suggestive): a wider follow-up is
#            justified, still no trading on the scores.
#   DROP   — the bar is not cleared AND every score IC is within one
#            SE of zero: drop the earnings-call leg.
#   REVIEW — the bar is not cleared but some score IC exceeds one SE
#            without reaching the bar: no evidence, but not ~0 either.
#            A human decides; the default recommendation is DROP,
#            since the pre-registered bar was not cleared.
import math as _math

EVIDENCE_MIN_ABS_IC = 0.30
EVIDENCE_MAX_P = 0.10


def _null_se(n):
    """Standard error of a correlation under the null, ~1/sqrt(n-3)."""
    return 1.0 / _math.sqrt(n - 3) if n > 3 else 1.0


def _score_ics(result):
    """The four Q&A-score ICs the evidence bar is read on."""
    table = result["ic_table"]
    return [
        table["score__raw"]["pearson"],
        table["score__excess"]["pearson"],
        table["score__raw"]["spearman"],
        table["score__excess"]["spearman"],
    ]


def decide_drop(result):
    """Drop-rule outcome for a returns-test result dict.

    Returns (decision, reason, max_abs_ic, bar_cleared) where decision
    is one of "KEEP", "DROP", "REVIEW" (see thresholds above).
    """
    ics = _score_ics(result)
    bar_cleared = any(
        ic["r"] is not None and abs(ic["r"]) >= EVIDENCE_MIN_ABS_IC
        and ic["p"] is not None and ic["p"] < EVIDENCE_MAX_P
        for ic in ics
    )
    n = result["ic_table"]["score__raw"]["n"]
    se = _null_se(n)
    max_abs_ic = max(abs(ic["r"]) for ic in ics if ic["r"] is not None)
    if bar_cleared:
        return ("KEEP",
                f"pre-registered bar cleared (a score IC has |IC| >= {EVIDENCE_MIN_ABS_IC} "
                f"with p < {EVIDENCE_MAX_P}); wider follow-up justified, still no trading on the scores",
                max_abs_ic, True)
    if max_abs_ic < se:
        return ("DROP",
                f"bar not cleared and every score IC is within one standard error of zero "
                f"(SE ~= {se:.2f} at n={n}; max |IC| = {max_abs_ic:.2f}) — the signal is still ~0",
                max_abs_ic, False)
    return ("REVIEW",
            f"bar not cleared, but a score IC reached |{max_abs_ic:.2f}| (one SE is {se:.2f} "
            f"at n={n}) — no evidence, but not ~0 either; human decision required "
            f"(default: DROP, bar not cleared)",
            max_abs_ic, False)


def _ic_str(ic):
    if ic["r"] is None:
        return "n/a"
    return f"{ic['r']:+.2f} (t {ic['t']:+.2f}, p = {ic['p']:.2f})"


def write_results_md(result, path, decision, reason):
    """Generated results summary. The November .md was hand-written
    (per-call notes, the post-hoc AMD-outlier sensitivity check);
    this file records only what the run computed, plus the drop-rule
    outcome. The outlier sensitivity check is NOT part of the
    registered design and is not recomputed here."""
    table = result["ic_table"]
    lines = [
        f"# Equibles Pilot Returns Test — Re-run {result.get('run_label', '')}",
        "",
        f"Generated {result['date']}. Machine-readable: "
        f"`{Path(path).name}`. Design (registered BEFORE the November run): "
        f"`exp-equibles-pilot-returns-design.md`. Wrapper: "
        f"`scripts/run_equibles_rerun.py`. Prices: "
        f"`experiments/equibles-pilot-raw/prices-adjusted-{result['date']}-*.csv` "
        f"(Alpaca daily bars, adjustment = all, split + dividend).",
        "",
        f"## Verdict: {result['verdict'].upper()}",
        "",
        f"Drop-rule outcome: **{decision}** — {reason}",
        "",
        "## What was tested",
        "",
        f"{result['n_included']} of the {result['n_scored']} scored calls "
        f"({result['n_excluded']} excluded — see below). Returns are "
        f"adjusted-close returns from the call date T to T+20 (20th trading "
        f"day after T); excess returns are vs SPY over the same window. T is "
        f"the call date even when the call was after market close, per the "
        f"registered design.",
        "",
        "## Headline ICs",
        "",
        "| Signal | vs raw return | vs excess return |",
        "|---|---|---|",
    ]
    for sig in ("score", "qoq_score_delta", "evasion", "analyst_tone"):
        n = table[f"{sig}__raw"]["n"]
        lines.append(
            f"| {sig} (n = {n}) | Pearson {_ic_str(table[f'{sig}__raw']['pearson'])}; "
            f"Spearman {_ic_str(table[f'{sig}__raw']['spearman'])} | "
            f"Pearson {_ic_str(table[f'{sig}__excess']['pearson'])}; "
            f"Spearman {_ic_str(table[f'{sig}__excess']['spearman'])} |"
        )
    ms = result["median_split"]
    lines += [
        "",
        "## Median split (score)",
        "",
        f"Median score {ms['median']:.2f}. Above-median (n = {ms['n_above']}): "
        f"{ms['above_mean_raw'] * 100:+.2f}% raw, {ms['above_mean_excess'] * 100:+.2f}% "
        f"excess. Below-median (n = {ms['n_below']}): {ms['below_mean_raw'] * 100:+.2f}% "
        f"raw, {ms['below_mean_excess'] * 100:+.2f}% excess. Spread "
        f"**{ms['spread_raw'] * 100:+.2f}pp raw / {ms['spread_excess'] * 100:+.2f}pp excess**. "
        f"{ms['n_dropped_at_median']} calls sit exactly at the median and are dropped "
        f"from the split per the design.",
        "",
        f"## Excluded calls ({result['n_excluded']} of {result['n_scored']})",
        "",
    ]
    if result["excluded"]:
        for e in result["excluded"]:
            lines.append(
                f"- {e['ticker']} FY{e['fiscal_year']} Q{e['fiscal_quarter']} "
                f"({e['call_date']}): {e['reason']}")
    else:
        lines.append("None — every scored call had a complete T+20 window.")
    lines += [
        "",
        "## Per-call returns",
        "",
        "| Ticker | Call date | T+20 | Score | 20d raw | 20d excess |",
        "|---|---|---|---|---|---|",
    ]
    for c in result["per_call"]:
        lines.append(
            f"| {c['ticker']} | {c['call_date']} | {c['t_plus_20']} | {c['score']:+.2f} | "
            f"{c['return_20d'] * 100:+.2f}% | {c['excess_return_20d'] * 100:+.2f}% |")
    lines += [
        "",
        "## Spend",
        "",
        "$0.00. No LLM calls (scores are frozen). Prices from the existing "
        "Alpaca paper account (data reads only). No orders, no live trading.",
        "",
        "## Honest caveats",
        "",
        "- n is tiny; a single call can move any IC by ~0.2.",
        "- 16 ICs on one small sample with no multiple-testing correction; "
        "the pre-registered bar (|IC| >= 0.3, p < 0.10) was read against "
        "that multiplicity.",
        "- Single-model, single-pass scores; Haiku 5.5 may know these calls' "
        "outcomes (knowledge-cutoff contamination).",
        "- This test does not touch the frozen holdout and says nothing about "
        "v2/v3/H3/C1c.",
        "",
    ]
    Path(path).write_text("\n".join(lines) + "\n")


def append_registry(result, decision, reason, bar_cleared, results_json,
                    registry_path):
    """Append one RESULTS line to experiments/registry.jsonl, mirroring
    the November results entry (same fields, same DIAGNOSTIC gate
    wording). Status follows the evidence bar: 'passed' only when the
    bar cleared, 'failed' when it did not — for this diagnostic that
    means the signal failed the bar, not that the run errored."""
    ic = result["ic_table"]["score__raw"]["pearson"]
    ice = result["ic_table"]["score__excess"]["pearson"]
    ms = result["median_split"]
    label = result.get("run_label", "")
    entry = {
        "experiment_id": f"exp-equibles-pilot-returns-{label}",
        "design_doc": "experiments/exp-equibles-pilot-returns-design.md",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "results_file": str(results_json),
        "status": "passed" if bar_cleared else "failed",
        "gate": (
            f"DIAGNOSTIC (no gate pass). Verdict: {result['verdict'].upper()}. "
            f"Score Pearson IC raw {ic['r']:+.3f} (p={ic['p']:.3f}), "
            f"excess {ice['r']:+.3f} (p={ice['p']:.3f}); median-split spread "
            f"{ms['spread_raw'] * 100:+.2f}pp raw / {ms['spread_excess'] * 100:+.2f}pp excess. "
            f"Drop-rule: {decision} — {reason}"
        ),
        "strategy": (
            f"RESULTS for exp-equibles-pilot-returns {label} (re-run via "
            f"scripts/run_equibles_rerun.py). {result['n_included']} of "
            f"{result['n_scored']} scored calls included, "
            f"{result['n_excluded']} excluded (T+20 after last trading day)."
        ),
        "spend": {"run_usd": 0.0},
    }
    with open(registry_path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry["experiment_id"]


def verdict_block(result, decision, reason, results_json, results_md,
                  price_files, sheet_status, registry_status=""):
    """The short verdict block printed at the end of every run."""
    raw_p = result["ic_table"]["score__raw"]["pearson"]
    exc_p = result["ic_table"]["score__excess"]["pearson"]
    raw_s = result["ic_table"]["score__raw"]["spearman"]
    exc_s = result["ic_table"]["score__excess"]["spearman"]
    ms = result["median_split"]
    excluded = ", ".join(
        f"{e['ticker']} {e['call_date']}" for e in result["excluded"]) or "none"
    _, _, _, bar_cleared = decide_drop(result)
    lines = [
        "",
        f"=== Equibles pilot re-run — {result.get('run_label', '')} (as-of {result['date']}) ===",
        f"tested:    n={result['n_included']} of {result['n_scored']} scored calls",
        f"excluded:  n={result['n_excluded']} ({excluded})",
        f"Q&A score IC vs raw:    Pearson {raw_p['r']:+.2f} (p={raw_p['p']:.2f}); "
        f"Spearman {raw_s['r']:+.2f} (p={raw_s['p']:.2f})",
        f"Q&A score IC vs excess: Pearson {exc_p['r']:+.2f} (p={exc_p['p']:.2f}); "
        f"Spearman {exc_s['r']:+.2f} (p={exc_s['p']:.2f})",
        f"median-split spread: {ms['spread_raw'] * 100:+.2f}pp raw / "
        f"{ms['spread_excess'] * 100:+.2f}pp excess",
        f"evidence bar (|IC| >= {EVIDENCE_MIN_ABS_IC}, p < {EVIDENCE_MAX_P}): "
        f"{'CLEARED' if bar_cleared else 'not cleared'}",
        f"DROP-RULE: {decision} — {reason}",
        f"results:   {results_json}",
        f"           {results_md}",
        f"prices:    {len(price_files)} files in {price_files[0].parent if price_files else '?'}",
        f"sheet:     {sheet_status}",
        f"registry:  {registry_status}",
        "",
    ]
    return "\n".join(lines)


def main(argv=None, *, fetch_client=None, sheet=None, repo=REPO, today=None,
         registry_path=None):
    """Run the full re-run. `fetch_client` / `sheet` are injectable
    for tests (fake Alpaca client, fake Sheet); when None, the real
    Alpaca paper client and the real Sheet CLI are used. `today`
    pins the default --as-of for tests. `registry_path` pins the
    registry file for tests; when None, --registry or the repo
    registry is used. Returns 0 on success."""
    parser = argparse.ArgumentParser(
        description="Re-run exp-equibles-pilot-returns as one command.")
    parser.add_argument("--as-of", default=None,
                        help="END date for the price fetch, YYYY-MM-DD (default: today)")
    parser.add_argument("--no-sheet", action="store_true",
                        help="do not append the result to the experiment-log Sheet")
    parser.add_argument("--no-registry", action="store_true",
                        help="do not append the run to experiments/registry.jsonl")
    parser.add_argument("--registry", default=None,
                        help="registry file to append (default: experiments/registry.jsonl)")
    parser.add_argument("--prices-dir", default=None,
                        help="directory for price CSVs (default: experiments/equibles-pilot-raw)")
    parser.add_argument("--results-dir", default=None,
                        help="directory for results md/json (default: experiments)")
    args = parser.parse_args(argv)

    try:
        as_of = datetime.strptime(args.as_of, "%Y-%m-%d").date() if args.as_of \
            else (today if today is not None else date.today())
    except ValueError:
        parser.error("--as-of must be YYYY-MM-DD")
    as_of_iso = as_of.isoformat()
    label = as_of.strftime("%b%Y").lower()  # e.g. dec2026, nov2026
    prices_dir = Path(args.prices_dir) if args.prices_dir else repo / "experiments" / "equibles-pilot-raw"
    results_dir = Path(args.results_dir) if args.results_dir else repo / "experiments"
    results_dir.mkdir(parents=True, exist_ok=True)
    results_json = results_dir / f"exp-equibles-pilot-returns-{label}.json"
    results_md = results_dir / f"exp-equibles-pilot-returns-{label}.md"
    price_template = f"prices-adjusted-{as_of_iso}-{{sym}}.csv"
    try:
        prices_rel = prices_dir.relative_to(repo)
    except ValueError:
        prices_rel = prices_dir
    price_source = (
        f"Alpaca daily bars, adjustment=all (split+dividend), fetched {as_of_iso}; "
        f"files {prices_rel}/prices-adjusted-{as_of_iso}-*.csv"
    )

    # 1. Fetch fresh adjusted bars (dated filenames — the November
    #    prices-adjusted-<SYM>.csv files are never touched).
    print(f"[1/5] fetching adjusted bars, END={as_of_iso} -> {prices_dir}", flush=True)
    price_files = fetch_pilot_prices.main(
        end=as_of_iso, out_dir=prices_dir, filename_template=price_template,
        client=fetch_client)
    missing = [s for s in SYMBOLS
               if not (prices_dir / price_template.format(sym=s)).exists()]
    if missing:
        raise RuntimeError(f"price fetch incomplete, missing: {missing}")

    # 2. Run the returns test (unchanged math, imported).
    print(f"[2/5] running returns test -> {results_json}", flush=True)
    result = run_pilot_returns.main(
        raw_dir=prices_dir, price_template=price_template,
        out_json=results_json, run_date=as_of_iso,
        price_source=price_source, run_label=label)

    # 3. Generated results summary.
    decision, reason, _, bar_cleared = decide_drop(result)
    write_results_md(result, results_md, decision, reason)
    print(f"[3/5] wrote {results_md}", flush=True)

    # 4. Sheet, unless --no-sheet.
    if args.no_sheet:
        sheet_status = "skipped (--no-sheet)"
    else:
        print("[4/5] logging to experiment-log Sheet", flush=True)
        log_returns_sheet.main(
            results_path=results_json, run_date=as_of_iso, sheet=sheet)
        sheet_status = f"logged {result['n_included'] + 1} rows"

    # 5. Registry, unless --no-registry.
    if args.no_registry:
        registry_status = "skipped (--no-registry)"
    else:
        reg_path = Path(registry_path) if registry_path is not None \
            else (Path(args.registry) if args.registry
                  else repo / "experiments" / "registry.jsonl")
        exp_id = append_registry(result, decision, reason, bar_cleared,
                                 results_json, reg_path)
        registry_status = f"logged {exp_id}"
        print(f"[5/5] logged to registry: {exp_id}", flush=True)

    print(verdict_block(result, decision, reason, results_json, results_md,
                        price_files, sheet_status, registry_status), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
