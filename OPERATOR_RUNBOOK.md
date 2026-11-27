# Operator Runbook — ATS Phase 4 soak

The soak is 60 trading days of paper trading (dry-run only until the
January gate review decides otherwise; starting the soak is a separate
decision from building this readiness). This runbook is the operator's
reference for the daily run, the kill switch, and every alert the
system can raise. Every procedure cites the code path it rests on.

## 0. What runs, when

| Job | What it does | When |
|---|---|---|
| `ats-daily-run` (cron) | `scripts/run_daily.py` — safety preflight, EDGAR v2 refresh, Haiku-only scoring of new filings, `main.py --dry-run --mode regular`, decision-log check. Dry-run only: preview, zero submissions. | Weekdays ~16:15 ET |
| Heartbeat monitor (cron, separate job) | `scripts/check_heartbeat.py` — reads ONLY `.cache/heartbeat.json` + the wall clock. Reports OK / MISSING / STALLED / FAILED. | After the daily run (see §5) |

The monitor is deliberately a SEPARATE job: a monitor inside the daily
run could not report the daily run missing. Nothing watches the
monitor itself — the backstop is the daily run's own missed-run alert
and the user noticing no daily report.

## 1. State and log locations

All paths are relative to the repo root (`~/workspace/stock-thesis`)
unless overridden by env (see `config.py` `PathConfig`).

| File | What it is | Code |
|---|---|---|
| `.cache/heartbeat.json` | One JSON record: `run_id`, `started_at`, `finished_at`, `status` (`started` / `completed` / `failed`), `run_status`, `detail`. Overwritten every run. | `scripts/run_daily.py` `_write_heartbeat`, `Runner.__init__`, `Runner.finish` |
| `.cache/daily_runs/daily_run_*.log` | Per-run log (kept 90 days, §6). `runs.jsonl` alongside it is one line per run and is never pruned. | `scripts/run_daily.py` `Runner`, `_prune_old_files` |
| `.cache/decision_logs/*.json` | Per-run decision log (kept 90 days, §6). The daily wrapper fails the run if today's log is missing/stale/corrupt. | `strategy_runner.py` `_write_strategy_outputs`, `scripts/run_daily.py` `check_decision_log` |
| `alerts.log` | One-line outcome per run + critical alerts from the daily run and the heartbeat monitor. | `scripts/run_daily.py` `Runner.alert`, `scripts/check_heartbeat.py` `emit_alert` |
| `order_intents.db` | B3 intent store (SQLite): `intents` table (one row per order intent, with `status`, `attempts`, `broker_order_id`) and `alerts` table (persistent alerts for UNKNOWN intents). | `order_manager.py` `OrderManager`, `get_alerts`, `blocked_symbols` |
| `strategy_state.json` | Strategy state: `submitted_today`, `open_entries`, `expected_positions`. | `strategy_runner.py` `StrategyState` |
| `guardian_state.json` | Guardian state: `halted`, `halt_reason`, `orders_today`, `consecutive_errors`, `day_start_equity`. | `guardian.py` `GuardianState` |
| `broker_state.json` | Broker adapter state (paper/live floors, starting capital). | `broker_alpaca.py` `DualBroker` |
| `trade_memory.db`, `execution_journal.db` | Decision history and execution journal. | `memory.py`, `strategy_runner.py` `_write_strategy_outputs` |
| `KILL_SWITCH` | Kill-switch file. Its mere existence halts trading. | `guardian.py` `can_trade` (checked first, every time), `config.py` `PathConfig.kill_switch_path` |

## 2. Kill switch — halt and resume

**Halt (stop all trading immediately):**
```bash
touch KILL_SWITCH
```
`Guardian.can_trade()` (`guardian.py:188`) checks the file FIRST, on
every call, before anything else — a run in progress stops submitting
at the next gate check. The halt is recorded with reason
`kill_switch_engaged` (`guardian.py:69`, `_halt`).

**Verify halted:**
```bash
# the guardian state file shows halted=true and the reason
python3 -c "import json; print(json.load(open('guardian_state.json')))"
# or: the next daily run's decision log shows status "halted"
```
`Guardian.health_report()` (`guardian.py:299`) returns `halted`,
`halt_reason`, `kill_switch` (file present?), `data_age_sec`,
`orders_today`, `consecutive_errors`, `equity`.

**Resume — needs an explicit human decision. Never auto-resume.**
1. Investigate the cause (see §4–§5) and fix it.
2. Remove the file: `rm KILL_SWITCH`.
3. Clear the persisted halt: `Guardian.reset_halt()` (`guardian.py:325`).
   It REFUSES if the kill file still exists ("reset refused: kill
   switch file still present"), and it resets `consecutive_errors`
   to 0. There is no CLI for this — run it from a Python shell, or
   ask Milo.
4. Confirm the next daily run completes with status `ok` and no
   `halted` entry in its decision log.

Note: `manual_halt(note)` (`guardian.py:322`) halts WITHOUT the file
(used by reconcile on drift, §4.3). A manual halt is cleared the same
way (step 3), but there is no kill file to remove.

## 3. Stale data — "benchmark data stale" vs "unavailable"

Freshness is not an age heuristic. A frame is fresh only if its
latest complete bar IS the latest completed NYSE session at decision
time (`universe.py` `_daily_bars`, `market_calendar.py`;
`StaleBarsError`).

| Decision-log reason | Meaning | What to do |
|---|---|---|
| `benchmark data unavailable` | The bars/quote fetch failed (e.g. egress-proxy outage, transient 5xx/429). Bounded retries (3 attempts, 1s/2s backoff, transient errors only — `universe.py` `fetch_with_retry`) were exhausted. Fail-closed: the run is `regime_blocked`, 0 orders. | Nothing, unless it repeats. Check `alerts.log` and the run log for the fetch error; if the proxy/outage persists, wait for the next run. Never re-run the fetch by hand to force a decision. |
| `benchmark data stale` | Bars arrived but their latest bar is an older session than the latest completed one (e.g. the last bar is 2026-06-02 while the latest session is 2026-11-25). The frame was rejected, not used. | Check the data feed / Alpaca bars. Do not trade on it: the decision log's `data_freshness` block (`decision_time`, `expected_latest_session`, `observed_latest_bar`, `missing_sessions`) shows exactly which sessions are missing. If the feed is back, the next run recovers on its own. |

Both are fail-closed: no order is placed on bad data. A stale quote
also fails the Guardian's staleness check (`guardian.py` `can_trade`,
`max_data_staleness_sec` 120s, fed only by quote feed timestamps via
`data_clock.py` `DataClock`).

## 4. Alert triage

Alerts come from three places: `alerts.log` (run outcomes + critical
alerts), the `alerts` table in `order_intents.db` (persistent B3
alerts), and the daily-run report in the ATS chat. Severities, in
order:

### 4.1 CRITICAL — act the same day

**Heartbeat MISSING / STALLED / FAILED** (from `check_heartbeat.py`,
exit 2; see §5). The daily job never started, died mid-run, or
finished failed.

**`TRADING HALTED`** (from `guardian.py` `_halt`): the guardian halted
— kill switch, stale data, daily-loss limit (3% of day-start equity),
order ceilings (25/day, 5/minute), or 3 consecutive errors. Read
`halt_reason` in `guardian_state.json` / `health_report()`, then §2.

**Reconcile drift** (`reconcile.py` `reconcile`, called at the end of
every live `run_daily`): "expected X actual Y" or "unexpected open
position". The broker holds something the strategy does not expect
(or vice versa). Reconcile counts an open order's unfilled quantity
toward the actual holding (a limit order not yet filled is an open
order, not a position — see `reconcile.py` `_actual_for_leg`), so a
discrepancy means a real mismatch: a filled order the strategy never
recorded, a cancelled order, or a manual position. The guardian is
halted automatically (`manual_halt("reconcile detected position
drift")`). Do NOT clear the halt until the mismatch is explained:
compare `strategy_state.json` `expected_positions` with the broker's
positions + open orders, find the extra/missing leg, and only then
`reset_halt()` (§2).

### 4.2 WARNING — check within a day

**UNKNOWN intent + persistent alert** (`order_manager.py`): the broker
accepted an order but the response was dropped and lookups stayed
inconclusive. The intent is UNKNOWN, the symbol is BLOCKED, and an
alert row sits in the `alerts` table until the lookup succeeds.
```sql
-- inspect (read-only)
sqlite3 order_intents.db "SELECT intent_id, symbol, status, attempts, broker_order_id FROM intents WHERE status='UNKNOWN';"
sqlite3 order_intents.db "SELECT intent_id, reason, resolved FROM alerts WHERE resolved=0;"
```
Do NOT submit a replacement order for that symbol by hand — the
manager's reconcile (`order_manager.py` `reconcile`,
`reconcile_all_unknowns`) is lookup-only and will adopt the original
order when it becomes visible (exactly one submission attempt is ever
made; see `ATS_LOOPS.md` Loop 5). A conflicting order for the same
symbol is refused as BLOCKED with zero broker calls. The block clears
itself when the intent resolves; if it does not clear after a day,
check the broker's order by client_order_id directly.

**`regime_blocked`** (decision log status): the market regime gate
failed (SPY below SMA-50/200, or 20d realized vol above the 25% cap).
Normal — no action. It is a decision, not an error.

**Broker outage / retry exhaustion** (§3, "unavailable"): transient.
If it repeats 3+ runs, check Alpaca status and the egress proxy.

### 4.3 INFO — no action

`dry_run` decision logs, `regime_blocked` on a market holiday
(Thanksgiving etc. — the run exits 0 with no orders), the daily
`status=ok` line in `alerts.log`.

## 5. Missed-run / heartbeat alert response

`scripts/check_heartbeat.py` reads ONLY `.cache/heartbeat.json` and
the wall clock. `scripts/run_daily.py` writes a `started` marker in
`Runner.__init__` and a `completed`/`failed` marker in `Runner.finish`
for EVERY outcome (including market-closed and early-failure paths).

| State | Meaning | Exact checks |
|---|---|---|
| **OK** | A run for the date finished `completed`, or one started within the last 60 min and is still going. | None. |
| **MISSING** | No heartbeat file, or the latest heartbeat is for an older date. The job never started. | 1. `ls -la .cache/heartbeat.json` — is the file there, and when was it last written? 2. `tail -5 .cache/daily_runs/runs.jsonl` — when did a run last finish? 3. Check the cron job exists and is enabled (`ats-daily-run`, weekdays ~16:15 ET). 4. Check the VM/scheduler is up. If the cron is missing or disabled, re-create it (parent task — do not hand-edit the cron definition files). |
| **STALLED** | A run started >60 min ago with no finish marker. The job started and died mid-run. | 1. `tail -50 .cache/daily_runs/daily_run_*.log` (newest file) — which step was last? Steps are `run_step` entries with exit codes; a timeout is `error: "timeout after Ns"`. 2. `tail -20 alerts.log`. 3. Check for a hung step (EDGAR refresh or decision step — the decision step timeout is 60 min). If the process is gone and the step cannot be retried safely, leave it: the next day's run starts fresh, and the missed run is recorded as a gap, not re-run. |
| **FAILED** | A run finished with `status: failed` (`run_status` `failed` or `safety`). | 1. Read `detail` in the heartbeat record (`jq . .cache/heartbeat.json`). 2. `tail -50` the run log named in `runs.jsonl`. 3. If `run_status` is `safety`, the preflight tripped (e.g. `ALLOW_LIVE=1` in `.env`, or a live key present) — fix `.env`, do not bypass the preflight. |

**Test the alert path** (proves delivery end to end, no real failure):
```bash
.venv/bin/python scripts/check_heartbeat.py --test-alert
# prints a CRITICAL TEST ALERT line, appends it to alerts.log, exits 2
```
Run it after any change to the monitor or the alert path.

## 6. Log rotation

On every `Runner.finish()` (`scripts/run_daily.py`), files older
than 90 days are pruned: `.cache/daily_runs/daily_run_*.log` and
`.cache/decision_logs/*.json` (`_prune_old_files`). Kept forever:
`.cache/daily_runs/runs.jsonl` (one line per run) and
`.cache/heartbeat.json` (one record, overwritten). Pruning is
best-effort and never fails a run. Do not hand-delete logs inside the
90-day window — the January gate reader and the soak audit need them.

## 7. Unprotected positions

"Unprotected" = a position the strategy holds with no open entry in
`strategy_state.json`, or an open entry with no live order. Two guards
already exist — know them before intervening:

- The entry loop refuses to add to a symbol it does not track
  (`strategy_runner.py` `run_daily`: "untracked existing broker
  position; not mixing strategy and manual lots"). If you see that
  skip, the broker holds a position the strategy does not own — find
  out why (manual trade? a filled order whose entry was never
  recorded?) before touching it.
- The exit loop refuses to exit into an unresolved intent
  (`strategy_runner.py` `run_daily`, order-manager blocked check):
  if a symbol's intent is UNKNOWN, the exit is skipped and the entry
  stays open until the intent resolves.

To close a position manually, do it at the broker (paper account),
then reconcile: the next run's `reconcile()` will flag the missing
expected position and halt — clear the halt only after confirming the
manual close in the broker's order history.

## 8. Soak exit gate

The soak ends after 60 trading days. It passes only if ALL of these
hold across the whole soak (checked from the logs, not from memory):

1. **No duplicate submissions** — every intent in `order_intents.db`
   has `attempts = 1` and at most one broker order (check: the
   `intents` table's `broker_order_id` values are unique, and no two
   intents share a `broker_order_id`).
2. **No unresolved mismatches** — zero rows in the `alerts` table with
   `resolved = 0` at the end, and zero reconcile discrepancies in
   `.cache/daily_runs/runs.jsonl` / the decision logs.
3. **No missed critical alerts** — every heartbeat MISSING / STALLED /
   FAILED was answered the same day (§5), and the `--test-alert`
   path was exercised at least once.
4. **No unexplained drift** — every `regime_blocked`, `needs_attention`,
   or halted run has a recorded cause in its decision log / run log.

A pass is candidacy only: paper trading beyond the soak, and any live
step, still need the user's explicit decision, written risk limits,
and a kill switch (§2). The January fresh-validation gate is a
separate read (see `experiments/exp-fresh-validation-v1-runbook-jan2027.md`);
do not read it early and do not confuse its verdict with the soak's.
