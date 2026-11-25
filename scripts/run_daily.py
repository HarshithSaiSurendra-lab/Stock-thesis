#!/usr/bin/env python3
"""Daily ATS run: refresh + score filings, then the decision loop.

B1 + B4 + B9 (approved 2026-11-25). Runs, in order:

  0. Safety preflight — deploy_check.py (ALLOW_LIVE must be 0, paper
     keys present, no kill switch) plus an explicit ALLOW_LIVE=0 check
     read from .env. Any failure here exits 2 and nothing else runs.
  1. EDGAR v2 refresh — build_edgar_dataset_v2.py --workers 4
     (parallel, resumable; fetches filings through 2026-12-31, i.e.
     effectively today), then --validate.
  2. Score ONLY new filings — score_fresh_v2.py (Haiku 5.5 only;
     resumes from the scores file, never re-scores; run cap $1.00,
     monthly cap $15.00 enforced in the provider; December budget
     confirmed $15). Then validate_fresh_v2.py as a WARNING-only
     check (a single unscored filing must not kill the daily
     decision; it is logged loudly instead).
  3. Decision loop — main.py --dry-run --mode regular. --mode
     regular is deliberate: at 16:15 ET the market is closed, so
     --mode auto would route to the after-hours engine (disabled,
     ALLOW_AFTER_HOURS=0) and no regular decision would run. Dry-run
     never submits an order (orders_preview only, dry_run=True).
  4. Decision-log check — a decision log for today's UTC date must
     exist in .cache/decision_logs/ with mtime after this run
     started; its status + skip summary are reported. A "valid quote
     unavailable" skip count > 0 is surfaced as a warning (quote
     flakiness was seen on the Nov 17 dry run).

Exit codes: 0 = OK; 1 = a step failed; 2 = safety preflight failed.
Every step has a timeout (deploy_check 60s, refresh 30 min, validate
5 min, scoring 10 min, decision 60 min): a step that hangs is killed
and counts as a failure instead of blocking the run forever. The
decision timeout is 60 min (not 30) because the first live run took
~40 min in that step — the alpaca-py SDK retries a flaky paper
trading API for a long time before giving up — and killing a slow
but legitimate run would be worse than waiting.
Every run writes a timestamped log to .cache/daily_runs/, appends a
one-line outcome to ./alerts.log, and appends a JSON record to
.cache/daily_runs/runs.jsonl. Telegram is NOT configured (no
NOTIFIER_TOKEN / NOTIFIER_CHAT_ID in .env), so the cron's non-zero
exit + these logs are the alert path — do not invent credentials.

The parent (cron) runs this once a day; this script never schedules
itself. No orders are ever placed by this script. ALLOW_LIVE stays 0.

Usage:
    .venv/bin/python scripts/run_daily.py
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV_PY = REPO / ".venv" / "bin" / "python"
RUN_LOG_DIR = REPO / ".cache" / "daily_runs"
ALERTS_LOG = REPO / "alerts.log"

EXIT_OK = 0
EXIT_STEP_FAILED = 1
EXIT_SAFETY = 2

# Statuses a decision log may legitimately carry. Anything else is
# still a written log (the check passes) but is reported verbatim.
KNOWN_STATUSES = {"dry_run", "halted", "regime_blocked", "market_closed",
                  "breadth_blocked", "closed", "ok", "needs_attention"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Runner:
    def __init__(self) -> None:
        RUN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.log_path = RUN_LOG_DIR / f"daily_run_{stamp}.log"
        self._log_fh = self.log_path.open("a", encoding="utf-8")
        self.started_at = datetime.now(timezone.utc)
        self.today_utc = self.started_at.date().isoformat()
        self.steps: list[dict] = []

    def log(self, line: str) -> None:
        msg = f"[{_utc_now()}] {line}"
        print(msg, flush=True)
        self._log_fh.write(msg + "\n")
        self._log_fh.flush()

    def alert(self, line: str) -> None:
        """One-line outcome/warning in the repo's alert log (git-ignored)."""
        with ALERTS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{_utc_now()} daily_run {line}\n")

    def run_step(self, name: str, argv: list[str], timeout: int | None = None) -> tuple[int, str]:
        """Run one step, streaming output to stdout + the run log.

        Returns (returncode, last-200-lines tail). Never raises. A
        step that exceeds `timeout` seconds is killed and counts as a
        failure (rc -2) — a hung step must fail loudly instead of
        blocking the daily run forever (seen 2026-11-25: main.py hung
        30+ min on the Alpaca paper trading API with no timeout).
        """
        self.log(f"STEP {name}: {' '.join(argv)}" + (f" (timeout {timeout}s)" if timeout else ""))
        try:
            proc = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self.log(f"STEP {name}: TIMEOUT after {timeout}s (killed); "
                     f"see the run log — the step hung instead of failing")
            self.steps.append({"step": name, "rc": -2, "error": f"timeout after {timeout}s"})
            return -2, str(exc)
        except Exception as exc:  # noqa: BLE001 - a failed spawn is a step failure
            self.log(f"STEP {name}: FAILED to spawn: {exc}")
            self.steps.append({"step": name, "rc": -1, "error": str(exc)})
            return -1, str(exc)
        out = (proc.stdout or "") + (proc.stderr or "")
        for line in out.splitlines():
            self.log(f"  | {line}")
        self.steps.append({"step": name, "rc": proc.returncode})
        if proc.returncode != 0:
            self.log(f"STEP {name}: FAILED (exit {proc.returncode})")
        else:
            self.log(f"STEP {name}: ok")
        return proc.returncode, out

    def finish(self, status: str, detail: str = "") -> int:
        record = {
            "ts": _utc_now(),
            "date": self.today_utc,
            "status": status,
            "detail": detail,
            "steps": self.steps,
            "log": str(self.log_path),
        }
        with (RUN_LOG_DIR / "runs.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        line = f"status={status} date={self.today_utc} log={self.log_path.name}"
        if detail:
            line += f" detail={detail}"
        self.alert(line)
        self.log(f"DAILY_RUN {status.upper()} {detail} (log: {self.log_path})")
        self._log_fh.close()
        return EXIT_OK if status == "ok" else (EXIT_SAFETY if status == "safety" else EXIT_STEP_FAILED)


def _read_allow_live(repo: Path) -> str | None:
    """ALLOW_LIVE value from .env (flag only — no keys are read)."""
    env_path = repo / ".env"
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("ALLOW_LIVE="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


def _count_score_errors(scores_path: Path) -> int:
    if not scores_path.exists():
        return 0
    scores = json.loads(scores_path.read_text())
    return sum(1 for v in scores.values() if isinstance(v, dict) and "error" in v)


def check_decision_log(log_dir: Path, today_utc: str, since: datetime) -> tuple[bool, str]:
    """True iff a decision log for `today_utc` was written after `since`.

    Returns (ok, human-readable detail incl. status + skip summary).
    Pure function — unit-tested in scripts/test_run_daily.py.
    """
    if not log_dir.exists():
        return False, f"decision log dir missing: {log_dir}"
    candidates = sorted(log_dir.glob(f"{today_utc}_*.json"))
    fresh = [p for p in candidates
             if datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= since]
    if not fresh:
        n_today = len(candidates)
        return False, (f"no decision log for {today_utc} written after run start "
                       f"({n_today} log(s) for today, all stale or none)")
    newest = fresh[-1]
    try:
        summary = json.loads(newest.read_text())
    except Exception as exc:  # noqa: BLE001 - a corrupt log is a failure
        return False, f"newest decision log {newest.name} is not valid JSON: {exc}"
    status = str(summary.get("status", "unknown"))
    skip_summary = summary.get("skip_summary", {}) or {}
    quote_skips = 0
    for reason, count in skip_summary.items():
        if "valid quote unavailable" in str(reason):
            quote_skips += int(count)
    detail = (f"{newest.name} status={status} "
              f"selected={len(summary.get('selected', []) or [])} "
              f"orders_preview={len(summary.get('orders', []) or [])} "
              f"quote_unavailable_skips={quote_skips}")
    if status not in KNOWN_STATUSES:
        detail += " (UNEXPECTED STATUS)"
    return True, detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily ATS run: refresh + score + dry-run decision.")
    parser.parse_args(argv)
    r = Runner()
    r.log(f"DAILY_RUN start date={r.today_utc} repo={REPO}")

    # ---- Step 0: safety preflight (nothing else runs if this fails).
    allow_live = _read_allow_live(REPO)
    if allow_live != "0":
        r.log(f"SAFETY FAIL: ALLOW_LIVE={allow_live!r} in .env (must be '0'); refusing to run")
        return r.finish("safety", f"ALLOW_LIVE={allow_live!r}")
    rc, _ = r.run_step("deploy_check", [str(VENV_PY), "deploy_check.py"], timeout=60)
    if rc != 0:
        r.log("SAFETY FAIL: deploy_check.py failed (live enabled, missing paper keys, or kill switch present)")
        return r.finish("safety", "deploy_check failed")
    r.log("SAFETY OK: ALLOW_LIVE=0, paper keys present, no kill switch")

    # ---- Step 1: EDGAR v2 refresh (parallel) + validate.
    rc, _ = r.run_step("edgar_refresh",
                       [str(VENV_PY), "build_edgar_dataset_v2.py", "--workers", "4"],
                       timeout=1800)
    if rc != 0:
        return r.finish("failed", "edgar_refresh failed")
    rc, _ = r.run_step("edgar_validate",
                       [str(VENV_PY), "build_edgar_dataset_v2.py", "--validate"],
                       timeout=300)
    if rc != 0:
        return r.finish("failed", "edgar_validate failed")

    # ---- Step 2: score only new filings (Haiku only, caps in code).
    scores_path = REPO / "experiments" / "results" / "exp-fresh-validation-v1-scores-haiku.json"
    errors_before = _count_score_errors(scores_path)
    rc, _ = r.run_step("score_fresh", [str(VENV_PY), "score_fresh_v2.py"], timeout=600)
    if rc != 0:
        return r.finish("failed", "score_fresh failed")
    errors_after = _count_score_errors(scores_path)
    if errors_after > errors_before:
        r.log(f"WARNING: {errors_after - errors_before} new scoring error(s) "
              f"({errors_before} -> {errors_after}); recorded in the scores file, run continues")
        r.alert(f"WARNING scoring errors +{errors_after - errors_before} date={r.today_utc}")
    # Validation is warning-only: one unscored filing must not kill
    # the daily decision (the design tolerates individual failures).
    rc, _ = r.run_step("validate_fresh", [str(VENV_PY), "validate_fresh_v2.py"], timeout=600)
    if rc != 0:
        r.log("WARNING: validate_fresh_v2.py FAILED (see output above); "
              "decision loop still runs — investigate before the January gate read")
        r.alert(f"WARNING validate_fresh failed date={r.today_utc}")

    # ---- Step 3: decision loop, paper DRY-RUN only.
    # --mode regular is deliberate (see module docstring): at 16:15 ET
    # the market is closed, so --mode auto would route to the disabled
    # after-hours engine and no regular decision would run.
    rc, _ = r.run_step("decision_dry_run",
                       [str(VENV_PY), "main.py", "--dry-run", "--mode", "regular"],
                       timeout=3600)
    if rc != 0:
        return r.finish("failed", "decision_dry_run failed (main.py non-zero exit)")

    # ---- Step 4: decision-log check (B9 missed-run alert).
    log_dir = REPO / ".cache" / "decision_logs"
    ok, detail = check_decision_log(log_dir, r.today_utc, r.started_at)
    if not ok:
        r.log(f"DECISION LOG CHECK FAILED: {detail}")
        return r.finish("failed", f"no decision log: {detail}")
    r.log(f"DECISION LOG OK: {detail}")
    if "quote_unavailable_skips=0" not in detail:
        r.alert(f"WARNING quote_unavailable_skips date={r.today_utc} {detail}")

    return r.finish("ok", detail)


if __name__ == "__main__":
    raise SystemExit(main())
