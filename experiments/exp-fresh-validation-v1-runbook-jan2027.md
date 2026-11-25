# exp-fresh-validation-v1 — January 2027 completion runbook

Registered design: `experiments/exp-fresh-validation-v1-design.md`.
Step 0 (partial, through 2026-11-24) was completed 2026-11-24 — see
`experiments/exp-fresh-validation-v1-step0.json` for its counts.
This runbook is the EXACT completion procedure. Do not improvise:
any parameter change invalidates the test and must be registered as
a new experiment instead.

## Preconditions (check before starting)

- User go-ahead for the completion run (same as rounds 1/2).
- Window end is FIXED at **2026-12-31** (written into the Step 0 run
  file before any gate was read). It cannot be moved after seeing
  results.
- Minimums (both must hold, else extend — see below): ≥ 30 trading
  days AND ≥ 40 in-scope filings with fresh Haiku scores (filing date
  inside the window 2026-11-16..2026-12-31).
- ALLOW_LIVE stays 0. No paper trading, no live money.
- December Anthropic budget CONFIRMED 2026-11-25: **$15** (same as
  November; the November ledger does not carry over, and the
  provider's ledger is keyed by calendar month, so December starts
  at $0 against the $15 cap, enforced in code in `score_fresh_v2.py`).
- Use the repo venv (`.venv/bin/python`). Do not import sklearn or
  scipy (standing decision). Do not install anything.

## Step 1 — Complete the dataset refresh (through Dec 31)

The v2 extension covers 2026-11-13..2026-12-31 (window end is FIXED
at 2026-12-31 in `build_edgar_dataset_v2.py` since 2026-11-25) and
is refreshed DAILY by `scripts/run_daily.py`
(`build_edgar_dataset_v2.py --workers 4`, parallel, resumable — it
skips accessions already stored in data/edgar_excerpts_v2/).
As of 2026-11-25 the extension holds 7 filings (6 Step 0 filings +
LOW 10-Q filed 2026-11-25, scored the same day). The January
completion run is a final refresh + validate, not a fresh build:

```bash
cd ~/workspace/stock-thesis
.venv/bin/python build_edgar_dataset_v2.py --workers 4
.venv/bin/python build_edgar_dataset_v2.py --validate
```

Same extraction throughout (same universe, same trims, same
point-in-time discipline, amendments excluded).

## Step 2 — Score remaining filings (Haiku 5.5 only)

```bash
cd ~/workspace/stock-thesis
.venv/bin/python score_fresh_v2.py
.venv/bin/python validate_fresh_v2.py
```

`score_fresh_v2.py` resumes from
`experiments/results/exp-fresh-validation-v1-scores-haiku.json`
(disk cache makes re-scoring $0). Caps in code: run cap $1.00,
monthly cap = the confirmed December budget. Prompt
`llm-sentiment-v2`, same JSON score contract as Phase 2. No Sonnet
adjudication. `validate_fresh_v2.py` must PASS (union == v1 series
on pre-Nov-13 days; no score dated before its filing date).

## Step 3 — Check the minimums (do NOT read the gate yet)

Count, from the Step 0 run file + this run:

- Trading days in 2026-11-16..2026-12-31 (must be ≥ 30).
- Fresh filings with Haiku scores and filing date inside the window
  (must be ≥ 40).

If either minimum is unmet: extend the window by 10 trading days
(max 3 extensions, latest end ≈ 2027-01-29), refresh + score the
extension, and re-check. If still unmet after the third extension,
report the shortfall and STOP — do not run the gate. Record the
(final) window end in the results file BEFORE the gate is read.

## Step 4 — Recompute the v4 base on the fresh window

Same construction as rounds 1/2 (`backtest.py` / `r2_panels.py`
inputs, `exp-technical-v2` panel, long-hold), extended to the fresh
window. The frozen-holdout v4 numbers (−4.33% @10bps, −8.45% @20bps)
are reference only and are NOT the gate baseline here.

## Step 5 — Run v2 / v3 / H3 / H7 with parameters FROZEN

Parameters exactly as registered in the design doc (rounds 1/2
results files). No retuning. Common execution: weekly rebalance
(every 5 days), minimum hold 10 days, 1-bar lag, long-only, $1,000
initial capital. Costs 0/10/20bps (10bps base, 20bps stress).
Prices: same bar panel construction as rounds 1/2, extended to the
fresh window.

## Step 6 — Compute the gates (per design)

PASS iff ALL of SG1–SG3 (plus SG4 for H3):

- **SG1:** fresh-window net return @10bps > fresh-window v4 base @10bps.
- **SG2:** fresh-window net return @20bps > fresh-window v4 base @20bps.
- **SG3:** fresh-window net return @10bps > 0.
- **SG4 (H3 only):** Item 1A similarity IC @20d on the fresh
  window's filings. If significantly negative (IC < 0 with |t| ≥ 2),
  H3 FAILS regardless of SG1–SG3.
- **H7 audit rule (not a gate):** if H7 C1c's fresh-window Sharpe
  exceeds 5, audit the panel, the price join, and the trade list
  BEFORE any paper-trading decision.
- **Per-design minimum:** a design with < 10 trades in the window is
  INCONCLUSIVE, not PASS/FAIL.
- **Deflated Sharpe:** n_trials continues from 129 and increases by
  3 per design confirmed on the fresh window (one per cost level,
  the round-2 convention).
- Diagnostics (reported, not gated): Sharpe, max drawdown, Deflated
  Sharpe, alpha/beta vs SPY, hit rate, trades, turnover.

Write the results to
`experiments/results/exp-fresh-validation-v1.json` (one file, all
four designs + v4 base + gate verdicts).

## Step 7 — Log, commit, push

- Log the results to the experiment log Google Sheet
  (1bJdCrPm2licM-arLLdIck2zAq15gWfa6ZNjrk14h3G4) per repo
  convention (stringify all values; append; read back and verify
  the row count grew by exactly the number of rows appended).
- Append the registry entry to `experiments/registry.jsonl`.
- Update repo PROGRESS.md.
- Commit + push everything on branch agentic-p0 (standing rule:
  every successful version gets committed and pushed).

## Decision the gate informs (paper trading)

- If ≥ 1 design passes SG1–SG3 (and SG4 for H3): the passing
  design with the best fresh-window net10 becomes the paper-trading
  CANDIDATE. Candidacy is NOT authorization: paper trading still
  requires the user's explicit decision, with written risk limits
  and a kill switch, under the plan's Phase 4 (60-day paper soak).
  A pass with DSR < 0.95 or net20 ≤ 0 is flagged "pass, weak".
- If NO design passes: all four are demoted to "holdout-only
  passes" — no paper trading. Research returns to pattern
  recognition on the larger combined sample, with a NEW frozen
  holdout for any future test. The fresh window is then train data,
  never a test set again.
- If H7 C1c passes again: same candidacy rule, plus the audit rule.
  A second pass does not by itself authorize paper trading.

## Limits (read before interpreting any pass)

- This tests holdout-overfitting, NOT knowledge-cutoff
  contamination. Fresh-window filings are scored by Haiku 5.5 after
  the window, with training knowledge that may postdate the filings.
- Universe is not survivorship-free; filing dates (not EDGAR
  acceptance timestamps) with a 1-bar lag; after-close filings are
  effectively 1 day late — same as all prior runs.
- Backtest only; no paper trading, no live money, ALLOW_LIVE stays 0.
