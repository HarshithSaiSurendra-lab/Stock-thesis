from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from config import TradingConfig

log = logging.getLogger("reconcile")


def _load_state(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _positions_for_leg(leg) -> dict:
    try:
        return leg.positions() or {}
    except Exception:
        return {}


def reconcile(broker, memory, cfg: TradingConfig, guardian=None) -> dict:
    state = _load_state(cfg.paths.strategy_state_path)
    expected = state.get("expected_positions", {})
    actual = {
        "paper": _positions_for_leg(broker.paper),
        "live": _positions_for_leg(broker.live),
    }
    discrepancies: list[str] = []

    for leg_name in ("paper", "live"):
        exp = expected.get(leg_name, {})
        act = actual.get(leg_name, {})
        for symbol, payload in exp.items():
            exp_qty = float(payload.get("qty", 0.0))
            act_qty = float(act.get(symbol, {}).get("qty", 0.0))
            if round(exp_qty, 6) != round(act_qty, 6):
                discrepancies.append(f"{leg_name}:{symbol} expected {exp_qty} actual {act_qty}")
        for symbol in act:
            if symbol not in exp:
                discrepancies.append(f"{leg_name}:{symbol} unexpected open position {act[symbol].get('qty')}")

    ok = not discrepancies
    if discrepancies:
        log.warning("reconcile discrepancies: %s", discrepancies)
        if guardian is not None:
            guardian.manual_halt("reconcile detected position drift")
    return {"ok": ok, "discrepancies": discrepancies, "actual": actual, "expected": expected}

