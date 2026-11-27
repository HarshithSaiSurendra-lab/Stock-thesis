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
- **B13 (Nov 26, 2026) — B3 wired into the live loop (paper leg only,
  submit mode off by default):** `run_daily` in `strategy_runner.py`
  now creates an `OrderManager` (after the halted / market-closed /
  regime-blocked early returns, so a blocked run never touches the
  intent store) and runs startup reconcile (lookup-only) before any
  new submission; entries and exits go through the manager inside
  the guardian's gate and breaker; the live leg is untouched and
  disabled (`ALLOW_LIVE=0`). The daily cron still runs `--dry-run`
  only — the default invocation is provably unchanged (e2e Run 0:
  preview order only, no intent store, no broker state). The live
  intent namespace is `live-trend` (the trend engine is not a
  registered LLM design). Heartbeat: `run_daily.py` writes
  `.cache/heartbeat.json` (started marker in `Runner.__init__`,
  completed/failed marker in `finish()` for every outcome); the
  separate monitor cron runs `scripts/check_heartbeat.py` after the
  daily run and reports OK / MISSING / STALLED / FAILED to this chat.
  Runbook: `OPERATOR_RUNBOOK.md` (kill switch, stale vs unavailable,
  alert triage, heartbeat response, soak exit gate). Log rotation:
  run logs + decision logs older than 90 days are pruned on finish;
  `runs.jsonl` and `heartbeat.json` are never pruned. Reconcile fix
  found by the build (B13): `reconcile()` now counts an open order's
  unfilled quantity toward the actual holding
  (`broker_alpaca.py` `open_orders()` + `reconcile.py`
  `_actual_for_leg`) — without it, the first live run with an
  unfilled limit order would trip the drift halt, because the
  strategy records the expected position at submit time while
  `positions()` only reflects fills. An open order the strategy does
  NOT expect is still drift. **Closed Nov 26, 2026 (B15):** sizing
  now reserves budget for open buy orders — `existing_strategy_notional`
  includes remaining qty × reservation price (limit → filled average →
  current last price → entry price; no price is ever invented; an order
  with no usable price reserves nothing and logs a warning). Two buys
  that each fit alone but exceed the cap together now size the second
  down; a partial fill counts exactly once; a confirmed cancellation
  releases only the unfilled portion; an open sell reserves nothing.
  **Open before the soak (B15 observations, not in B15):** (1) the
  startup reconcile marks an expired/cancelled broker order CANCELLED
  in the intent store but nothing feeds that back into the strategy
  state — `expected_positions` still holds the full qty, so the next
  run's end-of-run reconcile trips the drift halt on the routine case
  of an unfilled DAY limit order expiring at close; (2) the exit
  branch sells the full entry qty without cancelling a working buy
  limit for the symbol — if that buy fills after the exit, the
  position re-opens. **Closed Nov 26, 2026 (B16):** both are fixed —
  (1) the startup reconcile now mirrors cancelled/expired orders back
  into the strategy state (`reconcile_open_intents` syncs confirmed-
  accepted intents at startup; the mirror reduces the expected
  position to the filled qty, or removes it when nothing filled;
  matched by intent_id only; dry-run never mutates state); (2) the
  exit cancels the working buy FIRST and sizes the sell to the filled
  qty, failing closed (exit skipped, entry left open) if the cancel
  cannot be confirmed — a late fill on a cancelled buy is a no-op in
  the fake broker. B16 also fixed a latent bug the investigation
  found: the SDK's `str(OrderStatus.EXPIRED)` is
  `"OrderStatus.EXPIRED"`, so the old `.lower()` mapping fell through
  to SUBMITTED — expired now maps to CANCELLED. **Corrected Nov 27,
  2026 (B16 reopen):** done_for_day is NOT terminal and does NOT map
  to CANCELLED — it is its own non-terminal state DONE_FOR_DAY (the
  Alpaca docs: it "will not receive further updates until the next
  trading day"; it can still fill or be cancelled, and its unfilled
  qty still counts against buying power). A DONE_FOR_DAY intent keeps
  its expected position and its budget reservation (sized from the
  intent store, since the broker's OPEN query excludes it); no
  replacement may be submitted for its symbol until the startup
  lookup adopts it as FILLED or CANCELLED. **Also closed Nov 27,
  2026:** the exit cancels the attached trailing stop FIRST (both
  legs, confirmed by a fresh open-orders re-fetch, fail-closed), then
  re-derives the sell size from the leg's current position; if the
  exit then fails to submit after the stop was cancelled, a CRITICAL
  `exit_unprotected` alert fires and the entry stays open. Known
  limitations (not in B16): an UNKNOWN exit intent blocks its symbol
  indefinitely with no admin/resolution path (test 3c — needs a human
  procedure before the soak); a stop firing with no signal-loss exit
  leaves a stale entry (the stop is not in the intent store, so
  nothing mirrors its fill — the end-of-run reconcile halts on
  drift); the residual race where the stop elects between the cancel
  confirmation and the exit submit (only an OCO/bracket eliminates
  it); a never-filled buy's tax lot would close at 0 proceeds only if
  the live leg is ever enabled (lot_id is None in the paper-only
  path). **Closed Nov 27, 2026 (B17):** (1) the operator resolution
  path (`scripts/resolve_intent.py` + `OrderManager.resolve_intent`)
  — lookup first, then a human decision with a reason AND a
  structured evidence reference; a timeout/404 alone, or an empty
  orders list alone, is refused and the block stays; the resolution
  is an appended record, attempt history preserved; it never
  submits. (2) the startup mirror of stop fills (by order ID —
  never inferred from absence in open orders; ambiguous -> exit
  blocked across restart; mirror only when the stop's fills account
  for the shortfall). (3) stop-fill P&L attribution (NULL, not 0,
  when the fill price cannot be fetched). (4) post-submit oversell
  detection — halt + CRITICAL alert, DETECTION not prevention
  (OCO/bracket stays deferred). (5) live-leg entry + exit through
  the OrderManager when the live leg is enabled; no intents while
  disabled. (6) the real leg's lookup returns limit_price +
  filled_avg_price. Plus the three-outcome traces ((a) stop fills
  during cancel, (b) partial fill, (c) cancel unknown across
  restart, ~1.90s bounded/uncertain unprotected window), the
  durable unfilled-exit record (2 unprotected shares survive a
  restart; a partial fill reduces the count by exactly what sold),
  and the oversized-stop interval on the SAME no-cap fake broker
  (before: short reproduced, position -1.0; after: stop sized to
  the FILLED qty at attach and resized as fills arrive,
  cancel-old-then-attach-new, sellable never exceeding held; the
  double-protection window — sellable 3 vs held 2 — does not occur
  under cancel-first ordering). The stop is now sized to the
  FILLED qty everywhere: an unfilled limit buy gets NO stop until
  the startup size sync attaches one on the first fill. Remaining
  (not in B17): the oversell race can only be detected, not
  prevented (OCO deferred); the same-day re-entry intent-id
  collision for ENTRIES (exits got `exit_attempts`; entries did
  not — blocked in production by `submitted_today`); live-leg stop
  sizing and the live-leg oversell check are paper-only gaps;
  B17.6/B17.5 are stub/fake-leg verified only, never against live
  Alpaca. Full traces, numbers, and the protection tradeoff are in
  PROGRESS.md (B17 section, Nov 27, 2026). Overall soak readiness
  stays OPEN for the user's call after the B17 report.
  **Closed Nov 27, 2026 (B18):** (1) the same-day re-entry
  intent-id collision — `StrategyState.entry_attempts` (persisted
  in the state file), bumped whenever an entry record leaves
  `open_entries` because the trade ended (exit-loop close,
  cancelled-intent mirror removal, stop-fill mirror removal —
  reductions do not bump, re-created entries after a
  CANCELLED/REJECTED exit do not bump, dry-run never bumps); the
  entry loop includes `entry_attempt` in the inputs only when > 0,
  so first-attempt ids are unchanged and a re-entry after a close
  gets a FRESH intent id (e2e: order_count 3, status ok, no drift
  halt, expected 2). (2) live-leg stop sizing — the startup size
  sync generalized per leg (per-leg stop fields, per-leg unknown
  flag, exit blocked while EITHER is set); live stop attached at
  the LIVE filled qty and resized cancel-old-then-attach-new as
  live fills arrive (trace sellable [1.0, 0.0, 2.0] vs held [2.0,
  2.0, 2.0]). (3) live-leg oversell check — `_check_exit_oversell`
  per leg against that leg's account-tagged stop fills and that
  leg's held qty; e2e exactly 1 detection, account "live", total
  committed 4.0 vs held 2.0, paper silent, live fill -> live held
  -2.0. (4) the `pending_exit_filled` snapshot blemish — filled_qty
  updated from the intent before the snapshot, exit-pending now
  0.0. (5) decisions close at FILL, not at submission —
  `_close_pending_exit_decision` in the FILLED branch, exit
  `filled_avg_price` by order id + the stop attribution persisted
  in the pending record; unfetchable price -> realized_pl NULL
  (not 0) + CRITICAL alert; immediate close kept only where the
  price is final (exit_qty 0, no manager, no exit submitted);
  cancelled/rejected exits keep the decision open (no double-close).
  The submit run's summary now shows trades_closed 0 for that
  trade; the fill run shows 1. Remaining (not in B18): the
  oversell race can only be detected, not prevented (OCO
  deferred); the ENTIRE mirror layer is paper-only — a live
  CANCELLED intent reaches the mirror but never matches (the
  mirror compares the paper intent_id only), the stop-fill mirror
  reads broker.paper only, and the post-mirror stop sync cancels
  paper stops only, so a live stop can be orphaned and live
  expected positions never mirrored (fail-closed drift halt if it
  ever happened; live is disabled, so simulation-only today);
  the decision is closed by the FIRST leg's exit to fill (paper
  first in dict order), so the other leg's fill is not separately
  attributed; the guardian's 5/min rate counts disabled-live-leg
  no-op submits (rapid manual re-runs can trip it — production
  runs are a day apart); a decision stays open until its exit
  fills (the B18.5 tail); B17.6/B17.5/B18.2/B18.3 are stub/fake-leg
  verified only, never against live Alpaca. Full traces and
  numbers are in PROGRESS.md (B18 section, Nov 27, 2026). Overall
  soak readiness stays OPEN for the user's call after the B18
  report.
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
  PROGRESS.md, commit + push. **Gate reader built Nov 26, 2026 (B14):**
  `scripts/read_fresh_validation_gate.py` — one command, stdlib-only,
  synthetic fixtures only, 27 tests; minimums-first (exit 2 shortfall,
  no gate read), fresh-v4 verification, SG1–SG3 + SG4, H7 AUDIT as a
  fourth verdict (not PASS, not FAIL, excluded from candidacy until an
  audit clears it). NOT run against the real window — gate reads in
  early January. **Calendar lock added Nov 26, 2026 (B14 amendment,
  32 tests):** the reader refuses to read the gate before the
  window's declared end (manifest `window_end`, extension-aware;
  exit 2, reason `window_not_closed`, no gate report, no candidate,
  no scores computed); today == window end is allowed; there is
  deliberately no `--today` CLI flag, so the lock cannot be
  overridden from the command line.
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
- **B12 built Nov 26, 2026:** `scripts/run_equibles_rerun.py` — one
  command (fetch fresh adjusted bars, run the returns test, write
  dated results, append to the Sheet, append to the registry, print
  the verdict). Frozen scores, unchanged math: the wrapper on the
  frozen November prices reproduces November exactly (n=16, IC
  +0.21, p=0.43, same 5 exclusions, whole-dict-equal on ic_table /
  median_split / per_call). Drop rule is three-way: KEEP if the
  pre-registered bar clears (|IC| >= 0.3, p < 0.10); DROP if the bar
  is not cleared and every score IC is within one standard error of
  zero; REVIEW in between (human decides, default DROP). The real
  December n=21 read runs ~Dec 3, once the 5 excluded windows
  complete (~Dec 2) — the wrapper is built and verified now, the
  live run is a separate scheduled step.
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
  resubmits. **Nov 26, 2026 (B16):** reconcile() never re-checks
  SUBMITTED intents, so the startup also runs
  `reconcile_open_intents` — every SUBMITTED / PARTIALLY_FILLED /
  DONE_FOR_DAY intent with a broker_order_id is looked up and its
  current broker status adopted (expired maps to CANCELLED, fixing
  the latent `str(OrderStatus.EXPIRED)` fall-through to SUBMITTED;
  done_for_day maps to its own non-terminal DONE_FOR_DAY state, not
  CANCELLED — it is still working, still reserved, and blocks a
  replacement for its symbol until the lookup adopts it as FILLED or
  CANCELLED, per the Nov 27, 2026 reopen); lookup-only, an
  inconclusive lookup leaves the intent untouched.
  The live loop then mirrors CANCELLED intents into the strategy
  state (Loop 1) and cancels the working entry order before an
  exit, sizing the sell to the filled qty. **Nov 27, 2026 (B17.1):**
  an UNKNOWN intent now has a human resolution path —
  `scripts/resolve_intent.py` (lookup first, then `--confirm` +
  `--reason` + structured evidence `{source, scope, result}`; a
  timeout/404 alone, or an empty orders list alone, is refused;
  the resolution is appended, never overwriting attempts or the
  broker_order_id; it never submits). **Nov 27, 2026 (B17.5):** the
  account is part of the idempotency key — the paper and live legs
  are separate broker accounts, so the same logical order on both
  is two broker orders with two client_order_ids, and each leg's
  startup reconcile is account-filtered. Adding "account" changes
  EVERY intent id vs pre-B17 (verified by hash); that is safe only
  because no production intent store exists yet — do not deploy
  mid-soak with open intents. **Nov 27, 2026 (B18.1):** the entry
  intent id now carries a per-symbol attempt component
  (`entry_attempts` in the state file, bumped when a trade ends);
  a same-day re-entry after a close gets a fresh id instead of
  re-deriving the old FILLED entry intent. **Nov 27, 2026 (B18
  live-mirror gap, NOT fixed):** the mirror layer is paper-only —
  `_mirror_cancelled_intents_into_state` matches the paper
  `intent_id` only (a live CANCELLED intent reaches it via the
  unfiltered `cancelled_intents()` query but never matches), the
  stop-fill mirror reads `broker.paper` only, and the post-mirror
  stop sync cancels paper stops only. A live stop can therefore be
  orphaned and live expected positions never mirrored; the
  end-of-run reconcile would halt on live drift (fail-closed).
  Live is disabled in production, so this is simulation-only
  today — a future build's item, not a soak blocker in the
  paper-only path.
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
  is now wired to B3 (B13, Nov 26, 2026 — paper leg, submit mode off
  by default; the daily cron still runs `--dry-run` only).

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
