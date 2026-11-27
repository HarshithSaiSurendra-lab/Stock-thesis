#!/usr/bin/env python3
"""check_heartbeat.py — missed-run monitor for the ATS daily job (B13).

Reads ONLY the heartbeat file (.cache/heartbeat.json, written by
scripts/run_daily.py) plus the wall clock. It never reads the daily
job's logs, decision logs, or state — so it can detect a job that
never started, not just one that failed loudly. It is run by a
SEPARATE scheduled job (created later by the parent); a monitor that
lived inside the daily job could not report the daily job missing.

States (for the checked date, default today UTC — the daily run's
date; 16:15 ET is always the same UTC date):
  OK       — a run for the date finished with status "completed", or
             a run started within the stall window and is still going.
  MISSING  — no heartbeat at all, or no run for the date (the file
             holds an older run). The job never started.
  STALLED  — a run for the date started but has no finish marker and
             started more than --stalled-after-minutes ago (default
             60). The job started and died mid-run.
  FAILED   — a run for the date finished with status "failed"
             (run_daily status "failed" or "safety").

Exit codes: 0 = OK, 2 = MISSING / STALLED / FAILED (critical).
Every critical outcome prints a CRITICAL line and appends it to
alerts.log — the same alert path the daily run uses — so the monitor
cron's report carries it. --test-alert emits a clearly-marked TEST
critical alert through that same path (exit 2) to prove delivery
end to end, without touching any real run state.

This monitor has no watcher of its own: if the monitor job itself
never runs, nothing reports it. The backstop is the daily run's own
B9 missed-run alert and the user noticing no daily report.

Usage:
    .venv/bin/python scripts/check_heartbeat.py [--date YYYY-MM-DD]
        [--heartbeat PATH] [--alert-log PATH]
        [--stalled-after-minutes N] [--test-alert]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_HEARTBEAT = REPO / ".cache" / "heartbeat.json"
DEFAULT_ALERT_LOG = REPO / "alerts.log"

EXIT_OK = 0
EXIT_CRITICAL = 2

STALLED_AFTER_MINUTES = 60


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def check_heartbeat(
    heartbeat_path: str | Path,
    now: datetime | None = None,
    date: str | None = None,
    stalled_after_minutes: int = STALLED_AFTER_MINUTES,
) -> dict:
    """Judge the heartbeat file for one date. Pure except for the one
    file read: no logs, no state, no network. `now` and `date` are
    injectable so tests can pin the evaluation time."""
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    day = date or moment.date().isoformat()
    path = Path(heartbeat_path)
    if not path.exists():
        return {
            "state": "MISSING",
            "date": day,
            "reason": f"no heartbeat file at {path}",
            "run_id": None,
        }
    try:
        record = json.loads(path.read_text())
    except Exception as exc:  # noqa: BLE001 - a corrupt heartbeat is MISSING
        return {
            "state": "MISSING",
            "date": day,
            "reason": f"heartbeat file at {path} is not valid JSON: {exc}",
            "run_id": None,
        }
    started_at = _parse_ts(record.get("started_at"))
    if started_at is None or started_at.date().isoformat() != day:
        return {
            "state": "MISSING",
            "date": day,
            "reason": (
                "no run started for "
                f"{day} (latest heartbeat is "
                f"{started_at.date().isoformat() if started_at else 'undated'})"
            ),
            "run_id": record.get("run_id"),
        }
    status = str(record.get("status") or "")
    finished_at = _parse_ts(record.get("finished_at"))
    if status == "failed" or (finished_at is not None and status not in ("completed", "started")):
        return {
            "state": "FAILED",
            "date": day,
            "reason": (
                f"run {record.get('run_id')} finished failed "
                f"(run_status={record.get('run_status')!r}"
                + (f", detail={record.get('detail')!r}" if record.get("detail") else "")
                + ")"
            ),
            "run_id": record.get("run_id"),
        }
    if finished_at is not None:
        return {
            "state": "OK",
            "date": day,
            "reason": f"run {record.get('run_id')} completed at {record.get('finished_at')}",
            "run_id": record.get("run_id"),
        }
    # Started, no finish marker: alive if recent, stalled if old.
    age = moment - started_at
    if age > timedelta(minutes=stalled_after_minutes):
        return {
            "state": "STALLED",
            "date": day,
            "reason": (
                f"run {record.get('run_id')} started "
                f"{age.total_seconds() / 60:.0f} min ago with no finish marker "
                f"(stall window {stalled_after_minutes} min)"
            ),
            "run_id": record.get("run_id"),
        }
    return {
        "state": "OK",
        "date": day,
        "reason": (
            f"run {record.get('run_id')} started "
            f"{age.total_seconds() / 60:.0f} min ago and is still going"
        ),
        "run_id": record.get("run_id"),
    }


def emit_alert(line: str, alert_log_path: str | Path) -> None:
    """Append one alert line to alerts.log (the daily run's alert path)
    and print it. Never raises: alerting failure must not mask the
    check result."""
    stamped = f"[{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}] {line}"
    print(stamped, flush=True)
    try:
        with Path(alert_log_path).open("a", encoding="utf-8") as fh:
            fh.write(stamped + "\n")
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Missed-run monitor for the ATS daily job.")
    parser.add_argument("--date", default=None,
                        help="date to check, YYYY-MM-DD (default: today UTC)")
    parser.add_argument("--heartbeat", default=str(DEFAULT_HEARTBEAT),
                        help="heartbeat file to read")
    parser.add_argument("--alert-log", default=str(DEFAULT_ALERT_LOG),
                        help="alert log to append critical alerts to")
    parser.add_argument("--stalled-after-minutes", type=int, default=STALLED_AFTER_MINUTES,
                        help="start without finish older than this is STALLED")
    parser.add_argument("--test-alert", action="store_true",
                        help="emit a clearly-marked TEST critical alert and exit 2")
    args = parser.parse_args(argv)

    if args.test_alert:
        emit_alert(
            "CRITICAL TEST ALERT (check_heartbeat --test-alert): this is a "
            "test notification through the critical-alert path; no run "
            "failed and no action is needed",
            args.alert_log,
        )
        return EXIT_CRITICAL

    result = check_heartbeat(
        args.heartbeat,
        date=args.date,
        stalled_after_minutes=args.stalled_after_minutes,
    )
    state = result["state"]
    if state == "OK":
        print(f"HEARTBEAT OK date={result['date']}: {result['reason']}")
        return EXIT_OK
    emit_alert(
        f"CRITICAL heartbeat {state} date={result['date']}: {result['reason']}",
        args.alert_log,
    )
    return EXIT_CRITICAL


if __name__ == "__main__":
    raise SystemExit(main())
