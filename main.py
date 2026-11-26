from __future__ import annotations

import argparse
import json
import logging

from broker_alpaca import AccountConfig as BrokerAccountConfig
from broker_alpaca import BrokerConfig, DualBroker
from config import TradingConfig
from data_clock import DataClock
from guardian import Guardian, GuardianConfig, TelegramAlerter
from memory import TradeMemory
from session_router import run_routed
from strategy_runner import _strategy_equity


def build_components(cfg: TradingConfig):
    broker = DualBroker(
        BrokerConfig(
            paper=BrokerAccountConfig("paper", cfg.paper.starting_capital, cfg.paper.floor_pct, cfg.paper.enabled),
            live=BrokerAccountConfig("live", cfg.live.starting_capital, cfg.live.floor_pct, cfg.live.enabled),
            max_position_pct=cfg.sizing.max_position_pct,
            state_path=cfg.paths.broker_state_path,
        )
    )
    memory = TradeMemory(cfg.paths.memory_db_path)

    # One clock for the whole run, fed ONLY by quote timestamps from the
    # data feed (broker.latest_quote + the universe screen note them).
    # The Guardian's staleness check judges this clock — the feed, not
    # the wall clock. Daily bars are judged separately, against the
    # exchange calendar (market_calendar.py). Until the first quote is
    # observed the clock is empty and the Guardian skips its staleness
    # check; the per-decision checks judge the data once it arrives.
    clock = DataClock()
    broker.data_clock = clock
    broker.paper.data_clock = clock
    broker.live.data_clock = clock

    def guarded_account() -> dict:
        account_equity = broker.paper.equity() or broker.live.equity() or cfg.sizing.strategy_capital
        return {"equity": _strategy_equity(cfg, account_equity)}

    guardian = Guardian(
        GuardianConfig(
            kill_switch_path=cfg.paths.kill_switch_path,
            state_path=cfg.paths.guardian_state_path,
            alert_log_path=cfg.paths.alert_log_path,
            max_position_pct=cfg.sizing.max_position_pct,
        ),
        get_account=guarded_account,
        last_data_ts=clock.last_ts,
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
    parser = argparse.ArgumentParser(description="Run the stock thesis loop")
    parser.add_argument("--dry-run", action="store_true", help="build orders without submitting")
    parser.add_argument(
        "--mode",
        choices=("auto", "regular", "after-hours"),
        default="auto",
        help="route by session or force a specific engine",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    cfg = TradingConfig.from_env()
    broker, guardian, memory = build_components(cfg)
    result = run_routed(broker, guardian, memory, cfg, dry_run=args.dry_run, mode=args.mode)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
