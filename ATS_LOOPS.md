# ATS Loops — revised plan (Cherny / Cat Wu loop discipline)

Source: Boris Cherny (Head of Claude Code) + Cat Wu (Head of Product),
"Claude Code's Creators Explain Agent Loops & How They Code"
(https://www.youtube.com/watch?v=Hth_tLaC2j8), plus Anthropic's loop
guidance (June guide; July session on four loop patterns). Saved as
entry 5 in `~/workspace/goals/ai-ml-learning-plan/files/ai-agent-building-videos.md`.

Pattern: **Agents → self-improving Loops → Graphs** connecting the loops.
The reel's headline numbers are viral packaging, not verified quotes;
the pattern itself is the lesson.

## The rule that governs every loop below

The spec, the verification rules, and the stop conditions are the most
important things you write. A loop without a written stop condition is
not a loop, it is a runaway process.

And: **every repeated mistake becomes memory.** Write the lesson into
`CLAUDE.md`, a skill, `PROGRESS.md`, or the experiment log — the same
place the next run will actually read — instead of fixing it once in
chat and forgetting it. That is the same discipline as the experiment
registry: a result is not real until it is written down where it can
be checked.

## Loop types (Anthropic taxonomy)

| Type | How it closes | ATS example |
|---|---|---|
| Turn-based | A human closes the loop | Astra feedback loop; January gate read |
| Goal-based | A stop condition / evaluator checks the goal | Fresh validation (minimums + SG1–SG3); B2 simulation (all designs, winner None) |
| Time-based | Re-run on an interval, or scheduled in the cloud | Daily run, weekdays 16:15 ET |
| Proactive | An event starts the work, nobody watches | B3 reconcile on UNKNOWN / startup; missed-run alert |

## Loop 1 — Daily run (time-based, with a turn-based review)

- **Spec:** `scripts/run_daily.py`, weekdays ~16:15 ET, dry-run only.
  Order: (0) safety preflight (`ALLOW_LIVE=0`, exit 2 on failure);
  (1) EDGAR v2 refresh `--workers 4` + validate; (2) Haiku-only
  scoring of new filings + `validate_fresh_v2.py` (warning-only);
  (3) `main.py --dry-run --mode regular`; (4) decision-log check.
  Every step has a timeout; every run writes a timestamped log to
  `.cache/daily_runs/`, one line to `alerts.log`, one JSON record to
  `.cache/daily_runs/runs.jsonl`. Telegram is NOT configured; the
  alert path is the cron report + `alerts.log`.
- **Verification (the run is OK only if all hold):** exit 0; a
  decision log for today's date exists with mtime after the run
  started; `data_freshness` shows expected vs observed session
  (`expected_latest_session`, `observed_latest_bar`,
  `missing_sessions`, `last_quote_ts`, `quote_age_seconds`); 0 orders.
- **B5 (Nov 26, 2026):** the benchmark fetch now retries transient
  failures (3 attempts, 1s/2s backoff — total retry delay 3s, far
  inside the 60-minute decision timeout) and still fails closed as
  "benchmark data unavailable" when all attempts fail; the universe
  screen runs per-symbol work in parallel (4 daemon threads, 60s
  per-symbol timeout) with identical selection and ordering, and a
  failing/hung symbol is skipped, never fatal. `StaleBarsError` is
  never retried — the freshness check above is unchanged.
- **Stop conditions:** any step rc != 0; safety preflight fails;
  regime_blocked / halted / kill switch present; `StaleBarsError`
  ("benchmark data stale") — the run stops, it does not trade on old
  data. A blocked run is a *correct* outcome, not a failure to retry
  by hand.
- **Graph:** feeds Loop 3 — each daily refresh + Haiku score adds to
  the fresh-validation window (Nov 16–Dec 31).

## Loop 2 — B2 design simulation (goal-based)

- **Spec:** `design_runner.py`, fake broker only, simulation only.
  Pure decision function `select_symbols(design, inputs)` over the
  SAME dated inputs for all five designs (v2, v3, h3-lazy-veto,
  h7-c1c, exp-ml-ranker-v1). Each design gets its own FakeBroker
  sandbox + intent store; `design_id` + `inputs_hash` recorded on
  every decision; orders routed through the tested B3 path.
- **Verification:** 6 B2 tests + 10 B3 tests pass; same inputs to all
  designs; v2 vs v3 same symbol/qty/day have different
  `client_order_id`s; re-running a design reuses the same broker
  order; `broker_order_count` equals the number of decisions.
- **Stop conditions:** any design produces a duplicate broker order;
  any attempt to pick a winner; any use of real Alpaca, real market
  data, or the frozen holdout in a simulation run. **Winner is always
  `None` until January** — the window closes Dec 31 and the sample
  minimums (≥30 trading days, ≥40 filings) are met.
- **Graph:** feeds the January gate — B2 proves the order path works
  before the gate picks a candidate; it does not pick the candidate.

## Loop 3 — Fresh validation (goal-based)

- **Spec:** `experiments/exp-fresh-validation-v1-runbook-jan2027.md`.
  Window Nov 16–Dec 31, 2026, FIXED. Minimums ≥30 trading days AND
  ≥40 newly scored filings, per-design ≥10 trades. Run v2/v3/H3/H7 +
  exp-ml-ranker-v1 with parameters FROZEN, vs a freshly recomputed
  v4 base. Gates SG1–SG3 (+SG4 for H3; H7 Sharpe >5 triggers a data
  audit, not a gate).
- **Verification:** Step 1 refresh + validate; Step 2 score remaining
  filings (Haiku only, December budget $15); Step 3 check minimums
  BEFORE reading any gate; Step 4 recompute v4 base; Steps 5–6 run
  frozen designs + compute gates; Step 7 log to Sheet, registry,
  PROGRESS.md, commit + push.
- **Stop conditions:** minimums unmet → extend by 10 trading days
  (max 3 extensions, latest end ≈ Jan 29, 2027); still unmet after
  the third → report the shortfall and STOP, do not run the gate.
  No design passes → all five demoted to "holdout-only passes", no
  paper trading; the fresh window becomes train data, never a test
  set again. A pass is candidacy only — paper trading still needs
  the user's explicit decision, written risk limits, and a kill
  switch (Phase 4, 60-day soak).

## Loop 4 — Earnings-call re-run, December (time-based, goal-based)

- **Spec:** Equibles pilot, re-run in December for the full n=21
  calls (was n=16, Nov 22). Script the re-run as one command (B12);
  do not hand-score.
- **Verification:** Q&A scores vs post-call returns, same IC test as
  November (IC +0.21, p=0.43, NO EVIDENCE on n=16).
- **Stop conditions:** if December IC is still ~0, DROP the leg.
  Better Equibles coverage (AAPL/AEP transcripts, guidance
  extraction, pagination — see the Nov 26 draft to Daniel O.) helps
  the test; it does not change the drop rule.

## Loop 5 — B3 reconcile (proactive / event-based)

- **Spec:** `order_manager.py` + `fake_broker.py`. One logical order
  produces at most one broker order AND exactly one submission
  attempt, ever. Deterministic `client_order_id` (SHA-256 of
  design/day/symbol/side/qty/type/limit/inputs hash); SQLite intent
  store (intents + fills + alerts), atomic claim, 120s lease;
  persist + claim BEFORE any broker call; `attempts` is incremented
  and persisted BEFORE place_order_fn is called, so a crash mid-call
  still leaves attempts >= 1 on disk.
- **Verification:** the acceptance tests in
  `test_b3_exact_once.py` — (1) accept + drop, restart, delayed
  lookup recovers the original, exactly 1 broker order; (1b) exactly
  ONE submission attempt across timeout, repeated 404s, lease
  expiry, and restart — with both endings covered: (a) a later
  lookup finds the original order and adopts it, still 1 attempt /
  1 order, alerts resolved, symbol unblocked; (b) the lookup never
  finds it, the intent stays UNKNOWN with a persistent alert and the
  symbol blocked, and a conflicting order for that symbol is refused
  as BLOCKED without any place call; (2) two concurrent workers,
  same intent + broker order; (3) partial fill + restart +
  duplicate fill replay, filled/remaining recorded exactly once;
  (4) startup reconcile before any new order; (5) deterministic
  replay re-derives the same ID. Plus: a 409/duplicate conflict is
  reconciled FIRST (lookup with grace polls) — adopted if found,
  UNKNOWN + alert + blocked if inconclusive, never REJECTED on an
  inconclusive lookup; only a conclusive non-duplicate rejection
  (400/validation) is terminal REJECTED. An expired claim taken over
  after the one attempt was made reconciles only — it never
  resubmits.
- **Stop conditions:** reconcile() is LOOKUP-ONLY — it has no
  place_order_fn parameter at all, so there is no resubmit path in
  the module. A 404 "not found" is inconclusive, not proof the
  broker never accepted the first attempt. A restart, a lease
  expiry, or a 404 never authorises a second submission attempt.
  If the lookup stays inconclusive, the intent stays UNKNOWN, an
  alert is recorded in the `alerts` table, and submit() refuses any
  OTHER intent for that (account, symbol) as BLOCKED until the
  intent resolves. The earlier resubmit-same-ID behaviour was
  removed Nov 26, 2026: the broker deduping a second attempt to one
  order did not make the second attempt acceptable.
- **Graph:** feeds the Phase 4 soak — B3 is the reason the soak can
  start the day the January gate reads, instead of needing a
  build-then-test cycle first. The live loop in `strategy_runner.py`
  is deliberately NOT rewired to B3 yet.

## Loop 6 — Astra collaboration (turn-based)

- **Spec:** user relays manually between Milo and ChatGPT
  "Astra 6.2 light" (GPT-6.2 Astra, Light effort — chat only, no API
  link, ATS API stays `gpt-6.1-sol`). Milo writes a self-contained
  paste-ready prompt; Astra's feedback comes back here.
- **Verification:** validate everything Astra says against repo
  evidence (tests, data, frozen holdout, registered designs) before
  accepting it; flag plainly when it looks off.
- **Stop conditions:** Astra's ideas are hypotheses to register and
  test — never a reason to retune against the frozen holdout, skip a
  gate, or paper-trade without the user's decision. Never paste
  credentials, API keys, or `.env` contents to Astra.

## Mistakes already turned into memory (the habit, applied)

| Mistake | Lesson written down where |
|---|---|
| Alpaca `limit` truncates from the START of the window — "successful" fetches returned bars ending Jun 2, 2026 | `universe.py` (request wide, take tail in code) + PROGRESS.md Nov 25 |
| 7-day bar-age heuristic could not tell a holiday weekend from a missing feed | Exchange-calendar freshness (`market_calendar.py`), PROGRESS.md Nov 25 |
| Guardian staleness check was a `lambda: time.time()` no-op | `DataClock` fed only by quote feed timestamps, PROGRESS.md Nov 25 |
| EDGAR v2 mutated v1 globals at import time | Builders take explicit windows/dirs, regression test, PROGRESS.md Nov 26 |
| "Dependency-free" was invented as a rule; it never was one | Library rule corrected in ranker design + runbook + PROGRESS.md Nov 25 |
| Sheet append silently wrote nothing (numbers rejected, exit 0) | Stringify all values + verify row count after appending, PROGRESS.md Nov 22 |
| Sonnet budget breached by $0.56 (chars/4 token estimate) | Token estimate fixed to ~2.9 chars/token, PROGRESS.md Nov 20–21 |
| Workday autofill puts Stockify/NexusAI under Work Experience | Check and remove on every NVIDIA submission (main-chat memory) |

A new repeated mistake belongs in this table, in `PROGRESS.md`,
and in the code that can prevent it — in that order.
