# exp-ml-ranker-v1 — REGISTERED design (not run)

Status: REGISTERED 2026-11-25 — approved by the user as written
("Register the ML ranker design", 2026-11-25 19:46 EST). No code
written, nothing installed, nothing evaluated. It runs in the January
fresh-validation run, after the v2/v3/H3/H7 gate reads — never on the
frozen 63-day holdout (another look at that window would raise the
false-positive risk further).

## The question it answers

User, 2026-11-25: "what's your thoughts on us building a machine learning
algo" for ATS, and "if there is a issue with the alpaca key let me know".

## Short answer

Yes — build a machine-learning *ranker*, not a neural net, and not now.
The ranker is the P4 step in the plan ("ML / LLM comparison only after
simple baselines pass"), and the simple baselines have not yet passed a
fresh-data gate. The January gate reads v2 / v3 / H3 / H7 on the fresh
Nov 16–Dec 31 window first. This design is registered now so the ML step
cannot be tuned after seeing the January result.

## What already exists — do not rebuild it

H5, round 2 (`r2_panels.py`, native numpy only): a cross-family ensemble
of closed-form Ridge (lam=10) + a native binned GBDT (depth-3, 100 rounds,
lr 0.05, min_leaf 200). Result on TRAIN: +54.98% net10, walk-forward 3/4,
turnover 0.166/day — DROPPED, below v2's +57.77%. The disagreement veto
did not bind (no selected name was in the top disagreement quintile).
Fit: 84,854 panel rows / 67,431 walk-forward predictions at a 20-day
horizon.

That is the honest baseline for any new ML algo: it must beat v2/v3 on
train AND on the fresh window, not just exist. A neural net / transformer
is the wrong tool here:

- Effective sample is filings, not panel rows. Train window is 1,446
  filings (2023-01-05..2026-08-17, dataset v1: 2,108 records, 133 tickers).
  The 84,854 panel rows are the same ~1,446 scores carried forward day
  after day — highly correlated, not 84,854 independent examples.
- The ML learning plan (Nov 14, 2026–Mar 5, 2027) is foundations-first:
  neural nets come after ML fundamentals, and no PyTorch/transformers go
  on the resume or LinkedIn until a project proves them. The proving
  project is rebuilding a piece of Stockify in PyTorch — not ATS.
- Standing decision (Nov 25): sklearn 1.9.1 + scipy 1.18.1 were REMOVED
  from the repo venv; do NOT import sklearn or scipy in any repo code.
  Reuse the native numpy Ridge/GBDT already in `r2_panels.py`. No new
  dependency, no install.

## Proposed design (frozen when approved)

- Model: rank-average ensemble of the existing native Ridge + native
  GBDT (same hyperparameters as H5 — no retuning), plus the deterministic
  v3 rules as a comparison arm, not as a feature to be re-fit.
- Features — proven ones only, all point-in-time (filing date <= day):
  1. Haiku 5.5 score LEVEL (predicts at 20–126d; IC @20d +0.203 on the
     adjudication subset).
  2. Textual delta, C6-rank (+48.97% train net10, never evaluated on
     holdout — its one shot is this fresh window).
  3. Realized vol (strongest technical predictor in train diagnostics).
  4. H7 C1c components: z(rvol_20) − z(ret_63) − z(drawdown_63) −
     z(mom_126_21) − z(rsi_14), cross-sectional z-scores per day.
  5. Item 1A similarity — with the DIAGNOSED sign, not the veto's
     premise: changers predict HIGHER returns (IC @20d −0.0177, t −4.78).
     Enter it as a feature and let the model learn the sign; do not
     hard-code the H3 veto.
  6. NOT included: GPT score (IC @20d −0.024, adds no signal),
     earnings-call score (no evidence, n=16, p=0.43), volume-shock /
     intraday / overnight features (H6 IC gate not cleared, |IC| < 0.03).
- Target: 20-day forward excess return vs SPY, rank-transformed
  cross-sectionally per day (same horizon as H5 / the LLM legs).
- Training: train window only (1,446 filings). Walk-forward, 4 folds,
  same folds as rounds 1/2 where comparable. No fitting on the fresh
  window, no fitting on the frozen holdout.
- Evaluation: ONLY the fresh window (Nov 16–Dec 31, 2026), in the same
  January run as v2/v3/H3/H7, same engine (weekly rebalance, 10-day
  minimum hold, 1-bar lag, long-only, $1,000 capital), same costs
  (0/10/20bps), same gates SG1–SG3, Deflated Sharpe with n_trials
  incremented past 129, minimum 10 trades. If it fails any gate, it is
  dropped like H5 — no rescue tuning.
- Execution: if (and only if) it passes the January gate AND the user
  then approves paper trading, it enters the production runner the same
  way the winning design does — after exact-once order lifecycle (P1)
  is built. It never enters the order path before that.

## Alpaca key — checked 2026-11-25 ~18:10 EST

The key is NOT the bottleneck for building the ML algo. Training is
offline, on the EDGAR dataset + cached bars; the key is only used for
(a) fresh market-data pulls and (b) placing paper orders — neither of
which training needs.

Check done in this run, with the stored paper key from the git-ignored
`.env` (key present, secret present, `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET`;
no key material printed or stored anywhere else):
- Market-data API: AUTHENTICATED SUCCESSFULLY. `data.alpaca.markets`
  returned SPY bars for 2026-11-25 (close 716.7). The key works.
- Account API (`paper-api.alpaca.markets/v2/account`): returned a
  policy denial in this background run — "this source is not available
  to background generation … do not retry". That is a background-run
  policy block, NOT an Alpaca auth failure, and it was not retried.
  Equity / buying-power re-check is left to the main chat.
- Earlier live-call timeouts (Nov 25) were unconfirmed, not evidence
  of revocation. No rotation needed: paper-only, never placed an order,
  lives in a git-ignored file. Revisit if exposed or before any live step.

## Order of work (agreed 2026-11-25)

1. Small fixes first — daily run + filing refresh + missed-run alerts
   (user approved 2026-11-25 23:02 UTC; builder already working).
2. January gate reads v2/v3/H3/H7 on the fresh window.
3. Only then: build + evaluate exp-ml-ranker-v1 on the same fresh window.
4. Options (calls/puts): later (P4), after the equity paper soak passes.

## Registration record

Approved by the user 2026-11-25 19:46 EST ("Register the ML ranker
design"). Registered as `experiments/exp-ml-ranker-v1-design.md` and
added to the January runbook (`exp-fresh-validation-v1-runbook-jan2027.md`,
Steps 5–6) as the fifth design in the January run. No training run
happens now; the build + evaluation happen in the January run, under
the same gates.
