#!/usr/bin/env python3
"""Log exp-equibles-pilot-scoring rows to the experiment Sheet.

Follows the r2_confirm.py pattern: stringify every value (the CLI's
--json-values rejects numbers/nulls and exits 0 with empty values),
append, then read back and verify the row count grew by exactly the
number of rows appended.
"""

import json
import subprocess
import sys

SHEET_ID = "1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4"
SHEET_CLI = "/opt/hatch/bin/hatch_gws_cli"
RESULTS = "/home/hatch/workspace/stock-thesis/experiments/exp-equibles-pilot-scoring.json"
TODAY = "2026-11-22"


def _s(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def row_count():
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+read", "--spreadsheet", SHEET_ID,
         "--range", "Sheet1!A1:A1000"],
        capture_output=True, text=True, timeout=120)
    return len(json.loads(proc.stdout).get("values", []))


def main() -> int:
    result = json.loads(open(RESULTS).read())
    before = row_count()
    print(f"[sheet] rows before: {before}", flush=True)
    rows = []
    for r in result["per_call"]:
        rows.append([
            "exp-equibles-pilot-scoring", TODAY, "pilot",
            f"{r['ticker']} FY{r['fiscal_year']}Q{r['fiscal_quarter']} {r['call_date']}",
            "",
            _s(r["score"]),
            _s(r["confidence"]),
            _s(r["evasion"]),
            _s(r["n_qa_turns"]),
            _s(r["analyst_tone"]),
            _s(r.get("qoq_score_delta")),
            f"Haiku Q&A score; evasion={r['evasion']:.2f}; "
            f"analyst_tone={r['analyst_tone']:.2f}; "
            f"{r['rationale'][:100]}",
        ])
    proc = subprocess.run(
        [SHEET_CLI, "sheets", "+append", "--spreadsheet", SHEET_ID,
         "--json-values", json.dumps(rows)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(f"[sheet] append FAILED: {proc.stdout} {proc.stderr}", flush=True)
        raise RuntimeError("Sheet append failed; rows NOT logged")
    after = row_count()
    print(f"[sheet] rows after: {after}; expected {before + len(rows)}",
          flush=True)
    if after < before + len(rows):
        raise RuntimeError("Sheet append did not land (row count unchanged); "
                           "rows NOT logged")
    print(f"[sheet] logged {len(rows)} pilot rows", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
