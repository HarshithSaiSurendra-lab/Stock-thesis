"""Experiment registry and untouched-holdout freeze (P0 gate).

This module is research-only. It is NOT imported by the order path
(main.py / strategy_runner.py / guardian.py / broker_alpaca.py).

Why it exists: PROJECT_STATUS.md (2026-08-24) and HANDOFF_SPEC.md §4.4
state that the next research release must add an experiment registry and
an untouched holdout, because research outputs could previously be
inspected repeatedly with no durable log. Every strategy / parameter
experiment must be registered BEFORE its results are inspected, and the
final holdout must be frozen BEFORE any tuning against it.

Rules enforced here:
- An experiment_id can be registered only once.
- A holdout, once frozen, cannot be re-frozen with different content.
  Re-freezing identical content is a no-op and returns the existing record.
- The holdout is defined as a RULE (the final 63 trading days of the
  acquired point-in-time dataset), not as calendar dates, because no
  real point-in-time, adjusted, survivorship-free dataset has been
  acquired yet. The calendar dates must be recorded at acquisition time
  in the holdout record's ``acquired_range`` field — and must not be
  changed after the first backtest touches that period.

Storage is append-only JSONL under ``experiments/`` next to this file,
so a later reader can audit what was registered, when, and in what order.
No API keys, no network, no broker access.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = REPO_ROOT / "experiments"
REGISTRY_FILE = "registry.jsonl"
HOLDOUT_FILE = "holdout.json"

REQUIRED_FIELDS = {
    "experiment_id",
    "strategy",
    "parameters",
    "cost_model",
    "horizon",
    "benchmark",
    "holdout_rule",
}

# Frozen on 2026-11-16, before any technical+LLM backtest. These are the
# P0 defaults from PROJECT_STATUS.md: simple baselines first, walk-forward,
# costs at 1x and stressed at 2x, fold-level reporting.
DEFAULT_HOLDOUT = {
    "definition": "final 63 trading days of the acquired point-in-time, "
    "adjusted OHLCV dataset (with delistings and point-in-time universe "
    "membership)",
    "benchmark": "SPY buy-and-hold and equal-weight universe",
    "cost_model": {"base": "1x modelled spread + slippage", "stress": "2x"},
    "horizon": "daily decisions, 1-bar lag between target and held weights",
    "acquired_range": None,  # fill in with calendar dates when data is acquired
    "frozen_at": None,
    "status": "frozen",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _registry_path(directory: str | os.PathLike | None) -> Path:
    base = Path(directory) if directory is not None else DEFAULT_DIR
    return base / REGISTRY_FILE


def _holdout_path(directory: str | os.PathLike | None) -> Path:
    base = Path(directory) if directory is not None else DEFAULT_DIR
    return base / HOLDOUT_FILE


def register_experiment(experiment: dict, directory: str | os.PathLike | None = None) -> dict:
    """Register one experiment before its results are inspected.

    Raises ValueError on missing required fields or a duplicate
    experiment_id. Returns the stored record (with registered_at added).
    """
    missing = sorted(REQUIRED_FIELDS.difference(experiment.keys()))
    if missing:
        raise ValueError(f"experiment missing required fields: {missing}")
    path = _registry_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = list_experiments(directory)
    if any(e.get("experiment_id") == experiment["experiment_id"] for e in existing):
        raise ValueError(
            f"experiment_id already registered: {experiment['experiment_id']}"
        )
    record = dict(experiment)
    record.setdefault("registered_at", _utc_now())
    record.setdefault("status", "registered")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def list_experiments(directory: str | os.PathLike | None = None) -> list[dict]:
    """Return registered experiments in registration order."""
    path = _registry_path(directory)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def freeze_holdout(holdout: dict | None = None, directory: str | os.PathLike | None = None) -> dict:
    """Freeze the untouched holdout. Refuses a different second freeze."""
    record = dict(DEFAULT_HOLDOUT if holdout is None else holdout)
    record["frozen_at"] = _utc_now()
    path = _holdout_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        comparable_existing = {k: v for k, v in existing.items() if k != "frozen_at"}
        comparable_new = {k: v for k, v in record.items() if k != "frozen_at"}
        if comparable_existing != comparable_new:
            raise ValueError(
                "holdout already frozen with different content; "
                "do not re-freeze after looking at results"
            )
        return existing
    path.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return record


def load_holdout(directory: str | os.PathLike | None = None) -> dict | None:
    """Return the frozen holdout record, or None if not frozen yet."""
    path = _holdout_path(directory)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
