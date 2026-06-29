from __future__ import annotations

import argparse
import json
import logging
import time

from broker_alpaca import AccountConfig as BrokerAccountConfig
from broker_alpaca import BrokerConfig, DualBroker
from config import TradingConfig
from guardian import Guardian, GuardianConfig, TelegramAlerter
from memory import TradeMemory
from strategy_runner import _strategy_equity, run_daily


def build_components(cfg: TradingConfig):
    broker = DualBroker(
        BrokerConfig(
            paper=BrokerAccountConfig("paper", cfg.paper.starting_capital, cfg.paper.floor_pct, True),
            live=BrokerAccountConfig("live", cfg.live.starting_capital, cfg.live.floor_pct, True),
            max_position_pct=cfg.sizing.max_position_pct,
            state_path=cfg.paths.broker_state_path,
        )
    )
    memory = TradeMemory(cfg.paths.memory_db_path)

    def guarded_account() -> dict:
        account_equity = broker.paper.equity() or broker.live.equity() or cfg.paper.starting_capital
        return {"equity": _strategy_equity(cfg, account_equity)}

    guardian = Guardian(
        GuardianConfig(
            kill_switch_path=cfg.paths.kill_switch_path,
            state_path=cfg.paths.guardian_state_path,
            alert_log_path=cfg.paths.alert_log_path,
            max_position_pct=cfg.sizing.max_position_pct,
        ),
        get_account=guarded_account,
        last_data_ts=lambda: time.time(),
        alerter=TelegramAlerter(
            cfg.paths.alert_log_path,
            cfg.notifier.token or "invalid",
            cfg.notifier.chat_id or "invalid",
        )
        if cfg.notifier.enabled and cfg.notifier.provider == "telegram"
        else None,
    )
    return broker, guardian, memory


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daily stock thesis loop")
    parser.add_argument("--dry-run", action="store_true", help="build orders without submitting")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = TradingConfig.from_env()
    broker, guardian, memory = build_components(cfg)
    result = run_daily(broker, guardian, memory, cfg, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
