#!/usr/bin/env python3
"""Log exp-fresh-validation-v1 Step 0 to the experiment Sheet.

One summary row (Step 0 has no portfolio returns, so the numeric
backtest columns stay blank and the counts go in the Verdict column).
Follows the log_returns_sheet.py pattern: stringify every value,
append, read back and verify the row count grew by exactly 1.
"""
import json
import subprocess

SHEET_ID = "1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4"
SHEET_CLI = "/opt/hatch/bin/hatch_gws_cli"
TODAY = "2026-11-24"


def row_count():
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+read", "--spreadsheet", SHEET_ID,
         "--range", "Sheet1!A1:A1000"],
        capture_output=True, text=True, timeout=120)
    return len(json.loads(proc.stdout).get("values", []))


def main() -> int:
    before = row_count()
    print(f"[sheet] rows before: {before}", flush=True)
    rows = [[
        "exp-fresh-validation-v1-step0", TODAY,
        "fresh 2026-11-16..2026-12-31 (partial thru 2026-11-24)",
        "Step 0 dataset refresh + Haiku scoring",
        "", "", "", "", "", "", "",
        "6 filings refreshed (CSCO, DIS 11-17; APD, PANW 11-19; HD 11-20; "
        "CRWD 11-23), 6/6 scored Haiku 5.5 llm-sentiment-v2, $0.0214 spend "
        "(Nov ledger $10.6917/$15); validation PASSED (union == v1 on 749 "
        "pre-Nov-13 days, 0 scores before filing date); window end FIXED "
        "2026-12-31; minimums NOT met (7/30 trading days, 6/40 filings) — "
        "no gate read, no backtests run",
    ]]
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+append", "--spreadsheet", SHEET_ID,
         "--json-values", json.dumps(rows)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(f"[sheet] append FAILED: {proc.stdout} {proc.stderr}", flush=True)
        raise RuntimeError("Sheet append failed; rows NOT logged")
    after = row_count()
    print(f"[sheet] rows after: {after} (grew by {after - before}, expected 1)",
          flush=True)
    if after - before != 1:
        raise RuntimeError("Sheet row count did not grow by exactly 1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
