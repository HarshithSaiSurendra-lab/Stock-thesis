"""Tests for scripts/check_heartbeat.py (B13 missed-run monitor).

The checker reads ONLY the heartbeat file + the wall clock (both
injectable). These tests pin the four states — OK / MISSING / STALLED /
FAILED — plus the --test-alert path, with fake heartbeats. No logs, no
state, no network.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_heartbeat  # noqa: E402

NOW = datetime(2026, 11, 26, 21, 30, tzinfo=timezone.utc)  # 16:30 ET


def _write_hb(path: Path, record: dict) -> Path:
    path.write_text(json.dumps(record))
    return path


def _started(run_id="20261126T211500Z", started="2026-11-26T21:15:00Z") -> dict:
    return {"run_id": run_id, "started_at": started, "finished_at": None,
            "status": "started", "run_status": None, "detail": ""}


def test_ok_completed_run(tmp_path):
    _write_hb(tmp_path / "heartbeat.json", {
        **_started(), "finished_at": "2026-11-26T21:20:00Z",
        "status": "completed", "run_status": "ok", "detail": ""})
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "OK"
    assert "completed" in result["reason"]


def test_ok_run_still_going_within_stall_window(tmp_path):
    _write_hb(tmp_path / "heartbeat.json", _started())
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "OK", result
    assert "still going" in result["reason"]


def test_missing_no_file(tmp_path):
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "MISSING"
    assert "no heartbeat file" in result["reason"]


def test_missing_older_run_only(tmp_path):
    _write_hb(tmp_path / "heartbeat.json", {
        **_started(run_id="20261125T211500Z", started="2026-11-25T21:15:00Z"),
        "finished_at": "2026-11-25T21:20:00Z", "status": "completed", "run_status": "ok"})
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "MISSING"
    assert "no run started for 2026-11-26" in result["reason"]


def test_missing_corrupt_file(tmp_path):
    (tmp_path / "heartbeat.json").write_text("{not json")
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "MISSING"
    assert "not valid JSON" in result["reason"]


def test_stalled_start_without_finish_over_60_min(tmp_path):
    _write_hb(tmp_path / "heartbeat.json",
              _started(started="2026-11-26T20:00:00Z"))
    late = NOW + timedelta(minutes=45)  # 135 min after the 20:00 start
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=late)
    assert result["state"] == "STALLED", result
    assert "135 min ago" in result["reason"]


def test_stalled_boundary_is_strictly_over(tmp_path):
    _write_hb(tmp_path / "heartbeat.json",
              _started(started="2026-11-26T20:30:00Z"))
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "OK", result  # exactly 60 min: not yet stalled


def test_failed_finish_status(tmp_path):
    _write_hb(tmp_path / "heartbeat.json", {
        **_started(), "finished_at": "2026-11-26T21:18:00Z",
        "status": "failed", "run_status": "failed", "detail": "step 4 broke"})
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "FAILED", result
    assert "step 4 broke" in result["reason"]


def test_failed_safety_status(tmp_path):
    _write_hb(tmp_path / "heartbeat.json", {
        **_started(), "finished_at": "2026-11-26T21:18:00Z",
        "status": "failed", "run_status": "safety", "detail": "ALLOW_LIVE=1"})
    result = check_heartbeat.check_heartbeat(tmp_path / "heartbeat.json", now=NOW)
    assert result["state"] == "FAILED", result


def test_date_override_checks_another_day(tmp_path):
    _write_hb(tmp_path / "heartbeat.json", {
        **_started(run_id="20261125T211500Z", started="2026-11-25T21:15:00Z"),
        "finished_at": "2026-11-25T21:20:00Z", "status": "completed", "run_status": "ok"})
    result = check_heartbeat.check_heartbeat(
        tmp_path / "heartbeat.json", now=NOW, date="2026-11-25")
    assert result["state"] == "OK", result


def test_main_ok_exits_zero(tmp_path, capsys):
    _write_hb(tmp_path / "heartbeat.json", {
        **_started(), "finished_at": "2026-11-26T21:20:00Z",
        "status": "completed", "run_status": "ok"})
    rc = check_heartbeat.main(["--heartbeat", str(tmp_path / "heartbeat.json"),
                               "--date", "2026-11-26"])
    assert rc == 0
    assert "HEARTBEAT OK" in capsys.readouterr().out


def test_main_missing_exits_critical_and_alerts(tmp_path, capsys):
    rc = check_heartbeat.main(["--heartbeat", str(tmp_path / "heartbeat.json"),
                               "--alert-log", str(tmp_path / "alerts.log"),
                               "--date", "2026-11-26"])
    assert rc == 2
    out = capsys.readouterr().out
    assert "CRITICAL heartbeat MISSING" in out
    assert "CRITICAL heartbeat MISSING" in (tmp_path / "alerts.log").read_text()


def test_main_test_alert(tmp_path, capsys):
    rc = check_heartbeat.main(["--test-alert",
                               "--alert-log", str(tmp_path / "alerts.log")])
    assert rc == 2
    out = capsys.readouterr().out
    assert "CRITICAL TEST ALERT" in out
    assert "no run failed" in out
    assert "CRITICAL TEST ALERT" in (tmp_path / "alerts.log").read_text()
