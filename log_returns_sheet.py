#!/usr/bin/env python3
"""Log exp-equibles-pilot-returns rows to the experiment Sheet.

Follows the log_pilot_sheet.py / r2_confirm.py pattern: stringify every
value (the CLI's --json-values rejects numbers/nulls and exits 0 with
empty values), append, then read back and verify the row count grew by
exactly the number of rows appended.
"""
import json
import subprocess
from pathlib import Path

SHEET_ID = "1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4"
SHEET_CLI = "/opt/hatch/bin/hatch_gws_cli"
RESULTS = "/home/hatch/workspace/stock-thesis/experiments/exp-equibles-pilot-returns.json"
TODAY = "2026-11-22"


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def _cli_row_count():
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+read", "--spreadsheet", SHEET_ID,
         "--range", "Sheet1!A1:A1000"],
        capture_output=True, text=True, timeout=120)
    return len(json.loads(proc.stdout).get("values", []))


def _cli_append(rows):
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+append", "--spreadsheet", SHEET_ID,
         "--json-values", json.dumps(rows)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(f"[sheet] append FAILED: {proc.stdout} {proc.stderr}", flush=True)
        raise RuntimeError("Sheet append failed; rows NOT logged")


class _CliSheet:
    """The real Sheet, behind the row_count()/append() interface the
    wrapper and tests use. A fake sheet object with the same two
    methods can be injected instead (tests never touch the Sheet)."""

    def row_count(self):
        return _cli_row_count()

    def append(self, rows):
        _cli_append(rows)


def main(results_path=None, run_date=None, sheet=None) -> int:
    """Append per-call + summary rows for a returns-test result.

    Defaults reproduce the November logging exactly (same results
    file, same date). The December wrapper (B12) passes the dated
    results file and the as-of date. The summary text is built from
    the result itself (n included/excluded, verdict, ICs, spread) —
    the November-specific wording ("pilot 16 calls", "5 calls
    excluded", the AMD-outlier parenthetical) is gone; that
    sensitivity remark belongs in the November .md, not in a
    re-runnable logger.
    """
    results = Path(results_path) if results_path is not None else Path(RESULTS)
    result = json.loads(results.read_text())
    date = run_date if run_date is not None else TODAY
    sheet = sheet if sheet is not None else _CliSheet()
    before = sheet.row_count()
    print(f"[sheet] rows before: {before}", flush=True)
    rows = []
    for r in result["per_call"]:
        rows.append([
            "exp-equibles-pilot-returns", date,
            f"{r['call_date']} -> {r['t_plus_20']}",
            f"{r['ticker']} FY{r['fiscal_year']}Q{r['fiscal_quarter']}",
            "",
            _s(r["return_20d"]),
            _s(r["excess_return_20d"]),
            "",
            "",
            _s(r["score"]),
            _s(r.get("qoq_score_delta")),
            f"Q&A score {r['score']:.2f}; evasion {r['evasion']:.2f}; "
            f"analyst_tone {r['analyst_tone']:.2f}; "
            f"20d raw {r['return_20d'] * 100:+.2f}%, excess {r['excess_return_20d'] * 100:+.2f}%",
        ])
    ic = result["ic_table"]["score__raw"]["pearson"]
    ice = result["ic_table"]["score__excess"]["pearson"]
    rows.append([
        "exp-equibles-pilot-returns", date,
        f"pilot {result['n_included']} calls, T+20",
        "SUMMARY",
        "",
        _s(ic["r"]),
        _s(ice["r"]),
        "",
        "",
        "",
        "",
        f"verdict: {result['verdict']}; score Pearson IC raw {ic['r']:+.3f} p={ic['p']:.3f}, "
        f"excess {ice['r']:+.3f} p={ice['p']:.3f}; median-split spread "
        f"{result['median_split']['spread_raw'] * 100:+.2f}pp raw; "
        f"{result['n_excluded']} calls excluded, windows incomplete",
    ])
    sheet.append(rows)
    after = sheet.row_count()
    print(f"[sheet] rows after: {after}; expected {before + len(rows)}", flush=True)
    if after < before + len(rows):
        raise RuntimeError("Sheet append did not land (row count unchanged); rows NOT logged")
    print(f"[sheet] logged {len(rows)} returns rows", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
