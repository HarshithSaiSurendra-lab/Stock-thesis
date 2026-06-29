from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple


def load_dotenv(path: str = ".env") -> None:
    """
    Minimal .env loader for local secrets. Existing environment variables win.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw_line in p.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    return float(value) if value not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    return int(value) if value not in (None, "") else default


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _csv_tuple(value: str) -> Tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass
class UniverseConfig:
    min_dollar_volume: float = 5_000_000.0
    min_price: float = 5.0
    max_spread_pct: float = 0.005
    universe_source: str = "seed"
    max_candidates: int = 200
    lookback_days: int = 60
    seed_symbols: Tuple[str, ...] = field(
        default_factory=lambda: (
            "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "JPM",
            "UNH", "XOM", "LLY", "AVGO", "COST", "PG", "HD", "MA", "V", "ADBE",
            "CRM", "PEP", "AMD", "NFLX", "KO", "PFE", "ORCL", "INTC", "CSCO",
            "MRK", "BAC", "WMT",
        )
    )


@dataclass
class SizingConfig:
    max_position_pct: float = 0.50
    target_n_positions: int = 5
    strategy_capital: float = 2_500.0
    max_deployed_pct: float = 1.00


@dataclass
class ExitConfig:
    trail_percent: float = 8.0
    exit_on_signal_loss: bool = False
    dynamic_trail_enabled: bool = True
    dynamic_trail_vol_multiple: float = 3.0
    min_trail_percent: float = 6.0
    max_trail_percent: float = 15.0


@dataclass
class SignalConfig:
    strong_score_threshold: int = 3
    mild_score_threshold: int = 1
    overbought_rsi: float = 70.0
    overbought_mfi: float = 80.0
    min_trend_quality: float = 3.0
    min_momentum: float = 0.0
    min_relative_strength_63: float = -999.0


@dataclass
class RegimeConfig:
    enabled: bool = True
    benchmark_symbol: str = "SPY"
    require_above_sma_50: bool = True
    require_above_sma_200: bool = True
    require_sma_20_above_sma_50: bool = True
    require_positive_momentum: bool = True
    max_benchmark_drawdown_63: float = 0.10
    max_benchmark_rvol_20: float = 0.25


@dataclass
class RiskConfig:
    max_entry_rvol: float = 0.55
    target_position_rvol: float = 0.12
    min_vol_scale: float = 0.25
    max_vol_scale: float = 1.00
    max_quote_spread_pct: float = 0.005


@dataclass
class AccountConfig:
    starting_capital: float = 1_000.0
    floor_pct: float = 0.80


@dataclass
class NotifierConfig:
    provider: str = "telegram"
    token: str = ""
    chat_id: str = ""
    enabled: bool = False


@dataclass
class RunConfig:
    timezone: str = "America/New_York"
    decision_time: str = "16:15"
    data_lookback_days: int = 320
    market_data_feed: str = "iex"
    market_data_adjustment: str = "raw"
    bar_cache_enabled: bool = True
    bar_cache_dir: str = "./.cache/bars"
    bar_cache_max_age_hours: float = 18.0


@dataclass
class PathConfig:
    broker_state_path: str = "./broker_state.json"
    strategy_state_path: str = "./strategy_state.json"
    guardian_state_path: str = "./guardian_state.json"
    memory_db_path: str = "./trade_memory.db"
    alert_log_path: str = "./alerts.log"
    kill_switch_path: str = "./KILL_SWITCH"


@dataclass
class TradingConfig:
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    signals: SignalConfig = field(default_factory=SignalConfig)
    regime: RegimeConfig = field(default_factory=RegimeConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper: AccountConfig = field(default_factory=AccountConfig)
    live: AccountConfig = field(default_factory=lambda: AccountConfig(starting_capital=1_000.0, floor_pct=0.80))
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    run: RunConfig = field(default_factory=RunConfig)
    paths: PathConfig = field(default_factory=PathConfig)
    raw_seed_symbols: str = ""

    @classmethod
    def from_env(cls) -> "TradingConfig":
        load_dotenv()
        token = _env_str("NOTIFIER_TOKEN", "")
        chat_id = _env_str("NOTIFIER_CHAT_ID", "")
        notifier = NotifierConfig(
            provider=_env_str("NOTIFIER_PROVIDER", "telegram"),
            token=token,
            chat_id=chat_id,
            enabled=bool(token and chat_id),
        )
        universe = UniverseConfig(
            min_dollar_volume=_env_float("MIN_DOLLAR_VOLUME", 5_000_000.0),
            min_price=_env_float("MIN_PRICE", 5.0),
            max_spread_pct=_env_float("MAX_SPREAD_PCT", 0.005),
            universe_source=_env_str("UNIVERSE_SOURCE", "seed"),
            max_candidates=_env_int("UNIVERSE_MAX_CANDIDATES", 200),
            lookback_days=_env_int("UNIVERSE_LOOKBACK_DAYS", 60),
            seed_symbols=_csv_tuple(
                _env_str(
                    "UNIVERSE_SEED_SYMBOLS",
                    "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,JPM,UNH,XOM,LLY,AVGO,"
                    "COST,PG,HD,MA,V,ADBE,CRM,PEP,AMD,NFLX,KO,PFE,ORCL,INTC,CSCO,"
                    "MRK,BAC,WMT",
                )
            ),
        )
        return cls(
            universe=universe,
            sizing=SizingConfig(
                max_position_pct=_env_float("MAX_POSITION_PCT", 0.50),
                target_n_positions=_env_int("TARGET_N_POSITIONS", 5),
                strategy_capital=_env_float("STRATEGY_CAPITAL", 2_500.0),
                max_deployed_pct=_env_float("MAX_DEPLOYED_PCT", 1.00),
            ),
            exits=ExitConfig(
                trail_percent=_env_float("TRAIL_PERCENT", 8.0),
                exit_on_signal_loss=_env_int("EXIT_ON_SIGNAL_LOSS", 0) == 1,
                dynamic_trail_enabled=_env_int("DYNAMIC_TRAIL_ENABLED", 1) == 1,
                dynamic_trail_vol_multiple=_env_float("DYNAMIC_TRAIL_VOL_MULTIPLE", 3.0),
                min_trail_percent=_env_float("MIN_TRAIL_PERCENT", 6.0),
                max_trail_percent=_env_float("MAX_TRAIL_PERCENT", 15.0),
            ),
            signals=SignalConfig(
                strong_score_threshold=_env_int("STRONG_SCORE_THRESHOLD", 3),
                mild_score_threshold=_env_int("MILD_SCORE_THRESHOLD", 1),
                overbought_rsi=_env_float("OVERBOUGHT_RSI", 70.0),
                overbought_mfi=_env_float("OVERBOUGHT_MFI", 80.0),
                min_trend_quality=_env_float("MIN_TREND_QUALITY", 3.0),
                min_momentum=_env_float("MIN_MOMENTUM", 0.0),
                min_relative_strength_63=_env_float("MIN_RELATIVE_STRENGTH_63", -999.0),
            ),
            regime=RegimeConfig(
                enabled=_env_int("REGIME_FILTER_ENABLED", 1) == 1,
                benchmark_symbol=_env_str("REGIME_BENCHMARK_SYMBOL", "SPY"),
                require_above_sma_50=_env_int("REGIME_REQUIRE_ABOVE_SMA_50", 1) == 1,
                require_above_sma_200=_env_int("REGIME_REQUIRE_ABOVE_SMA_200", 1) == 1,
                require_sma_20_above_sma_50=_env_int("REGIME_REQUIRE_SMA_20_ABOVE_SMA_50", 1) == 1,
                require_positive_momentum=_env_int("REGIME_REQUIRE_POSITIVE_MOMENTUM", 1) == 1,
                max_benchmark_drawdown_63=_env_float("REGIME_MAX_BENCHMARK_DRAWDOWN_63", 0.10),
                max_benchmark_rvol_20=_env_float("REGIME_MAX_BENCHMARK_RVOL_20", 0.25),
            ),
            risk=RiskConfig(
                max_entry_rvol=_env_float("MAX_ENTRY_RVOL", 0.55),
                target_position_rvol=_env_float("TARGET_POSITION_RVOL", 0.12),
                min_vol_scale=_env_float("MIN_VOL_SCALE", 0.25),
                max_vol_scale=_env_float("MAX_VOL_SCALE", 1.00),
                max_quote_spread_pct=_env_float("MAX_QUOTE_SPREAD_PCT", 0.005),
            ),
            paper=AccountConfig(
                starting_capital=_env_float("PAPER_STARTING_CAPITAL", 1_000.0),
                floor_pct=_env_float("PAPER_FLOOR_PCT", 0.80),
            ),
            live=AccountConfig(
                starting_capital=_env_float("LIVE_STARTING_CAPITAL", 1_000.0),
                floor_pct=_env_float("LIVE_FLOOR_PCT", 0.80),
            ),
            notifier=notifier,
            run=RunConfig(
                timezone=_env_str("TRADING_TIMEZONE", "America/New_York"),
                decision_time=_env_str("DECISION_TIME", "16:15"),
                data_lookback_days=_env_int("DATA_LOOKBACK_DAYS", 320),
                market_data_feed=_env_str("MARKET_DATA_FEED", "iex"),
                market_data_adjustment=_env_str("MARKET_DATA_ADJUSTMENT", "raw"),
                bar_cache_enabled=_env_int("BAR_CACHE_ENABLED", 1) == 1,
                bar_cache_dir=_env_str("BAR_CACHE_DIR", "./.cache/bars"),
                bar_cache_max_age_hours=_env_float("BAR_CACHE_MAX_AGE_HOURS", 18.0),
            ),
            paths=PathConfig(
                broker_state_path=_env_str("BROKER_STATE_PATH", "./broker_state.json"),
                strategy_state_path=_env_str("STRATEGY_STATE_PATH", "./strategy_state.json"),
                guardian_state_path=_env_str("GUARDIAN_STATE_PATH", "./guardian_state.json"),
                memory_db_path=_env_str("MEMORY_DB_PATH", "./trade_memory.db"),
                alert_log_path=_env_str("ALERT_LOG_PATH", "./alerts.log"),
                kill_switch_path=_env_str("KILL_SWITCH_PATH", "./KILL_SWITCH"),
            ),
        )

    def ensure_paths(self) -> None:
        for path in (
            self.paths.broker_state_path,
            self.paths.strategy_state_path,
            self.paths.guardian_state_path,
            self.paths.memory_db_path,
            self.paths.alert_log_path,
        ):
            p = Path(path)
            if p.parent:
                p.parent.mkdir(parents=True, exist_ok=True)
