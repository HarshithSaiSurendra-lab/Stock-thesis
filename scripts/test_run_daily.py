"""Tests for scripts/run_daily.py (B1/B4/B9 daily wrapper).

Covers the failure paths the wrapper must get right: the decision-log
check (missed/stale/corrupt logs fail) and step exit-code propagation.
Also covers the v2 parallel builder's merge dedup (worker shards are
merged into the main dir with no duplicate accessions).

Run: .venv/bin/python -m pytest scripts/test_run_daily.py -v
These tests never touch the network, the broker, or real data dirs.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_daily  # noqa: E402
import build_edgar_dataset as v1  # noqa: E402
import build_edgar_dataset_v2 as v2  # noqa: E402


# ---------------------------------------------------------------- check_decision_log

def _write_log(log_dir: Path, name: str, status: str = "dry_run", mtime: datetime | None = None) -> Path:
    path = log_dir / name
    path.write_text(json.dumps({"date": "2026-11-25", "status": status,
                                "selected": ["AAPL"], "orders": [],
                                "skip_summary": {"risk: valid quote unavailable": 2}}))
    if mtime is not None:
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))
    return path


def test_check_missing_dir_fails(tmp_path: Path) -> None:
    ok, detail = run_daily.check_decision_log(tmp_path / "nope", "2026-11-25",
                                              datetime.now(timezone.utc))
    assert not ok and "missing" in detail


def test_check_empty_dir_fails(tmp_path: Path) -> None:
    ok, _ = run_daily.check_decision_log(tmp_path, "2026-11-25", datetime.now(timezone.utc))
    assert not ok


def test_check_yesterday_only_fails(tmp_path: Path) -> None:
    _write_log(tmp_path, "2026-11-24_dry_run_20261124T210000Z.json")
    ok, detail = run_daily.check_decision_log(tmp_path, "2026-11-25", datetime.now(timezone.utc))
    assert not ok and "no decision log" in detail


def test_check_stale_today_log_fails(tmp_path: Path) -> None:
    since = datetime.now(timezone.utc)
    _write_log(tmp_path, "2026-11-25_dry_run_20261125T200000Z.json",
               mtime=since - timedelta(hours=1))
    ok, detail = run_daily.check_decision_log(tmp_path, "2026-11-25", since)
    assert not ok and "stale" in detail


def test_check_fresh_today_log_passes(tmp_path: Path) -> None:
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    _write_log(tmp_path, "2026-11-25_dry_run_20261125T213000Z.json",
               mtime=datetime.now(timezone.utc))
    ok, detail = run_daily.check_decision_log(tmp_path, "2026-11-25", since)
    assert ok
    assert "status=dry_run" in detail and "quote_unavailable_skips=2" in detail


def test_check_corrupt_log_fails(tmp_path: Path) -> None:
    (tmp_path / "2026-11-25_dry_run_20261125T213000Z.json").write_text("{not json")
    ok, detail = run_daily.check_decision_log(tmp_path, "2026-11-25",
                                              datetime.now(timezone.utc) - timedelta(minutes=5))
    assert not ok and "not valid JSON" in detail


# ---------------------------------------------------------------- run_step exit codes

def test_run_step_propagates_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_daily, "RUN_LOG_DIR", tmp_path / "runs")
    monkeypatch.setattr(run_daily, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(run_daily, "DECISION_LOG_DIR", tmp_path / "decision_logs")
    monkeypatch.setattr(run_daily, "ALERTS_LOG", tmp_path / "alerts.log")
    r = run_daily.Runner()
    rc, _ = r.run_step("failing", ["false"])
    assert rc != 0
    rc, _ = r.run_step("passing", ["true"])
    assert rc == 0
    r._log_fh.close()


def test_run_step_timeout_kills_hung_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_daily, "RUN_LOG_DIR", tmp_path / "runs")
    monkeypatch.setattr(run_daily, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(run_daily, "DECISION_LOG_DIR", tmp_path / "decision_logs")
    monkeypatch.setattr(run_daily, "ALERTS_LOG", tmp_path / "alerts.log")
    r = run_daily.Runner()
    rc, _ = r.run_step("hung", ["sleep", "30"], timeout=1)
    assert rc == -2
    assert r.steps[-1]["error"] == "timeout after 1s"
    r._log_fh.close()


def test_finish_writes_ledger_and_alert(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_daily, "RUN_LOG_DIR", tmp_path / "runs")
    monkeypatch.setattr(run_daily, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(run_daily, "DECISION_LOG_DIR", tmp_path / "decision_logs")
    monkeypatch.setattr(run_daily, "ALERTS_LOG", tmp_path / "alerts.log")
    r = run_daily.Runner()
    assert r.finish("ok", "test") == run_daily.EXIT_OK
    ledger = (tmp_path / "runs" / "runs.jsonl").read_text()
    assert json.loads(ledger)["status"] == "ok"
    assert "status=ok" in (tmp_path / "alerts.log").read_text()


# ---------------------------------------------------------------- heartbeat (B13)

def _runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> run_daily.Runner:
    monkeypatch.setattr(run_daily, "RUN_LOG_DIR", tmp_path / "runs")
    monkeypatch.setattr(run_daily, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(run_daily, "DECISION_LOG_DIR", tmp_path / "decision_logs")
    monkeypatch.setattr(run_daily, "ALERTS_LOG", tmp_path / "alerts.log")
    return run_daily.Runner()


def test_heartbeat_start_marker_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _runner(tmp_path, monkeypatch)
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["status"] == "started" and hb["finished_at"] is None
    assert hb["run_id"] == r.run_id and hb["started_at"]
    r._log_fh.close()


def test_heartbeat_finish_marker_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _runner(tmp_path, monkeypatch)
    assert r.finish("ok", "test") == run_daily.EXIT_OK
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["status"] == "completed" and hb["run_status"] == "ok"
    assert hb["finished_at"] and hb["run_id"] == r.run_id


def test_heartbeat_finish_marker_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _runner(tmp_path, monkeypatch)
    assert r.finish("failed", "step 4 broke") == run_daily.EXIT_STEP_FAILED
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["status"] == "failed" and hb["run_status"] == "failed"
    assert hb["detail"] == "step 4 broke"


def test_heartbeat_finish_marker_safety(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _runner(tmp_path, monkeypatch)
    assert r.finish("safety", "ALLOW_LIVE=1") == run_daily.EXIT_SAFETY
    hb = json.loads((tmp_path / "heartbeat.json").read_text())
    assert hb["status"] == "failed" and hb["run_status"] == "safety"


# ---------------------------------------------------------------- log rotation (B13)

def _touch(path: Path, days_old: float) -> None:
    path.write_text("{}")
    moment = (datetime.now(timezone.utc) - timedelta(days=days_old)).timestamp()
    os.utime(path, (moment, moment))


def test_prune_old_files_keeps_recent(tmp_path: Path) -> None:
    _touch(tmp_path / "daily_run_old.log", 91)
    _touch(tmp_path / "daily_run_edge.log", 89)
    _touch(tmp_path / "daily_run_new.log", 1)
    (tmp_path / "runs.jsonl").write_text("{}\n")
    pruned = run_daily._prune_old_files(tmp_path, "daily_run_*.log", 90)
    assert pruned == 1
    assert not (tmp_path / "daily_run_old.log").exists()
    assert (tmp_path / "daily_run_edge.log").exists()
    assert (tmp_path / "daily_run_new.log").exists()
    assert (tmp_path / "runs.jsonl").exists(), "runs.jsonl is never pruned"


def test_prune_old_files_missing_dir_is_zero(tmp_path: Path) -> None:
    assert run_daily._prune_old_files(tmp_path / "nope", "*.log", 90) == 0


def test_finish_prunes_old_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    r = _runner(tmp_path, monkeypatch)
    runs = tmp_path / "runs"
    logs = tmp_path / "decision_logs"
    logs.mkdir(parents=True, exist_ok=True)
    _touch(runs / "daily_run_20260101T000000Z.log", 100)
    _touch(logs / "2026-08-01_regime_blocked_x.json", 100)
    _touch(logs / "2026-11-26_regime_blocked_x.json", 0)
    assert r.finish("ok", "test") == run_daily.EXIT_OK
    assert not (runs / "daily_run_20260101T000000Z.log").exists()
    assert not (logs / "2026-08-01_regime_blocked_x.json").exists()
    assert (logs / "2026-11-26_regime_blocked_x.json").exists()
    assert (runs / "runs.jsonl").exists()


# ---------------------------------------------------------------- _read_allow_live

def test_read_allow_live(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALLOW_LIVE=0\nALPACA_PAPER_KEY=xxx\n")
    assert run_daily._read_allow_live(tmp_path) == "0"
    env.write_text("ALLOW_LIVE=1\n")
    assert run_daily._read_allow_live(tmp_path) == "1"
    assert run_daily._read_allow_live(tmp_path / "missing") is None


# ---------------------------------------------------------------- v2 merge dedup

def _shard_record(accession: str, ticker: str = "AAPL") -> dict:
    return {"ticker": ticker, "accession": accession, "filing_date": "2026-11-25",
            "form": "10-Q", "excerpt_text": "x", "mda_text": "x", "risk_text": "x"}


def test_merge_worker_dirs_dedups(tmp_path: Path) -> None:
    main_dir = tmp_path / "main"
    main_dir.mkdir()
    w0, w1 = tmp_path / "w0", tmp_path / "w1"
    for d in (w0, w1):
        d.mkdir()
    (w0 / "excerpts_2026_Q4.jsonl").write_text(
        json.dumps(_shard_record("acc-1")) + "\n" + json.dumps(_shard_record("acc-2")) + "\n")
    (w0 / "worker_result.json").write_text(json.dumps(
        {"tickers": ["AAPL"], "n_new": 2, "failures": []}))
    (w1 / "excerpts_2026_Q4.jsonl").write_text(
        json.dumps(_shard_record("acc-2")) + "\n" + json.dumps(_shard_record("acc-3")) + "\n")
    (w1 / "worker_result.json").write_text(json.dumps(
        {"tickers": ["MSFT"], "n_new": 2, "failures": [{"ticker": "MSFT", "stage": "x"}]}))
    # The merge target is passed explicitly — the test never touches the
    # real v2 dir (the old version monkeypatched v1.DATA_DIR, which the
    # 2026-11-25 refactor removed).
    n_new, failures = v2._merge_worker_dirs([w0, w1], data_dir=main_dir)
    assert n_new == 3  # acc-2 counted once
    assert len(failures) == 1
    stored = v1.load_all_records(data_dir=main_dir)
    assert sorted(r["accession"] for r in stored) == ["acc-1", "acc-2", "acc-3"]
