from __future__ import annotations

import json
import os
from pathlib import Path

from config import TradingConfig


def main() -> int:
    cfg = TradingConfig.from_env()
    cfg.ensure_paths()
    report = {
        "paper_keys_present": bool(os.getenv("ALPACA_PAPER_KEY") and os.getenv("ALPACA_PAPER_SECRET")),
        "live_keys_present": bool(os.getenv("ALPACA_LIVE_KEY") and os.getenv("ALPACA_LIVE_SECRET")),
        "live_enabled": cfg.live.enabled,
        "after_hours_enabled": cfg.after_hours.enabled,
        "after_hours_real_orders_enabled": cfg.after_hours.allow_real_orders,
        "after_hours_live_enabled": cfg.after_hours.submit_live,
        "strategy_capital": cfg.sizing.strategy_capital,
        "target_positions": cfg.sizing.target_n_positions,
        "max_deployed_pct": cfg.sizing.max_deployed_pct,
        "max_position_pct": cfg.sizing.max_position_pct,
        "execution_journal_db_path": cfg.paths.execution_journal_db_path,
        "execution_journal_parent_exists": Path(cfg.paths.execution_journal_db_path).parent.exists(),
        "kill_switch_present": Path(cfg.paths.kill_switch_path).exists(),
    }
    print(json.dumps(report, indent=2))
    if not report["paper_keys_present"]:
        print("ERROR: missing Alpaca paper key/secret in .env")
        return 1
    if report["live_enabled"]:
        print("ERROR: ALLOW_LIVE=1. Keep ALLOW_LIVE=0 for today's paper deployment.")
        return 1
    if report["kill_switch_present"]:
        print("ERROR: KILL_SWITCH is present; trading is intentionally blocked.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
