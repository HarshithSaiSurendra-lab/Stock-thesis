from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from broker_alpaca import decide_order_type
from config import TradingConfig
from filters import downside_ok, market_regime, volatility_scale
from indicators import build_feature_frame
from memory import SignalSnapshot, TradeMemory
from notifier import Notifier, format_daily
from reconcile import reconcile
from signal import composite_signal, composite_score, trend_quality_score
from universe import fetch_symbol_frame, select_universe

log = logging.getLogger("strategy")


@dataclass
class StrategyState:
    trading_day: str = ""
    submitted_today: list[str] = field(default_factory=list)
    expected_positions: dict = field(
        default_factory=lambda: {"paper": {}, "live": {}}
    )
    open_entries: dict = field(default_factory=dict)
    last_run_ts: Optional[str] = None

    @classmethod
    def load(cls, path: str) -> "StrategyState":
        p = Path(path)
        if p.exists():
            try:
                payload = json.loads(p.read_text())
                return cls(**payload)
            except Exception:
                pass
        return cls()

    def save(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.__dict__, indent=2))

    def start_day(self, day: str) -> None:
        if self.trading_day != day:
            self.trading_day = day
            self.submitted_today = []


def _latest_row(features: pd.DataFrame) -> Optional[pd.Series]:
    if features.empty:
        return None
    row = features.dropna(how="all").iloc[-1] if not features.dropna(how="all").empty else None
    return row


def _fill_price(quote: Optional[dict], direction: str = "buy") -> float:
    if quote is None:
        return float("nan")
    if direction == "sell":
        return float(quote.get("bid") or quote.get("ask") or 0.0)
    return float(quote.get("ask") or quote.get("bid") or 0.0)


def _quote_with_spread_pct(quote: Optional[dict]) -> Optional[dict]:
    if quote is None:
        return None
    out = dict(quote)
    mid = (float(out.get("bid", 0.0)) + float(out.get("ask", 0.0))) / 2
    if "spread_pct" not in out and mid > 0:
        out["spread_pct"] = float(out.get("spread", 0.0)) / mid
    return out


def _decision_snapshot(symbol: str, direction: str, row: pd.Series, quote: Optional[dict]) -> SignalSnapshot:
    spread_pct = quote.get("spread_pct") if quote else None
    dollar_volume = float(row.get("close", 0.0) * row.get("volume", 0.0)) if "volume" in row else 0.0
    return SignalSnapshot(
        symbol=symbol,
        direction=direction,
        rsi=float(row.get("rsi_14", float("nan"))),
        mfi=float(row.get("mfi_14", float("nan"))),
        kvo_hist=float(row.get("kvo_hist", float("nan"))),
        obv_slope=float(row.get("obv_slope_20", float("nan"))),
        wad_slope=float(row.get("wad_slope_20", float("nan"))),
        momentum=float(row.get("mom_126_21", float("nan"))),
        rvol=float(row.get("rvol_20", float("nan"))),
        spread_pct=float(spread_pct if spread_pct is not None else float("nan")),
        dollar_volume=float(dollar_volume),
    )


def _side_for_signal(direction: str) -> str:
    return "buy" if direction in {"strong_up", "mild_up"} else "sell"


def _build_order(symbol: str, side: str, qty: float, quote: Optional[dict], direction: str, cfg: TradingConfig) -> dict:
    order = {"symbol": symbol, "side": side, "qty": qty}
    if side == "buy":
        order.update(decide_order_type(direction, quote, cfg.universe.max_spread_pct))
    else:
        order["order_type"] = "market"
    return order


def _trail_percent_for_row(row: pd.Series, cfg: TradingConfig) -> float:
    if not cfg.exits.dynamic_trail_enabled:
        return cfg.exits.trail_percent
    rvol = float(row.get("rvol_20", float("nan")))
    if not math.isfinite(rvol) or rvol <= 0:
        return cfg.exits.trail_percent
    daily_vol = rvol / math.sqrt(252)
    trail = daily_vol * cfg.exits.dynamic_trail_vol_multiple * 100.0
    return max(cfg.exits.min_trail_percent, min(cfg.exits.max_trail_percent, trail))


def _entry_price_from_result(result: Optional[dict], fallback: float, quote: Optional[dict], side: str) -> float:
    if result and isinstance(result, dict):
        submitted = result.get("submitted", {})
        if "limit_price" in submitted and submitted["limit_price"] is not None:
            return float(submitted["limit_price"])
    if side == "buy":
        return _fill_price(quote, "buy")
    return fallback


def _close_price_from_quote(quote: Optional[dict], side: str) -> float:
    return _fill_price(quote, side)


def run_daily(
    broker,
    guardian,
    memory: TradeMemory,
    cfg: TradingConfig,
    dry_run: bool = False,
) -> dict:
    cfg.ensure_paths()
    today = datetime.now(timezone.utc).date().isoformat()
    state = StrategyState.load(cfg.paths.strategy_state_path)
    state.start_day(today)

    guardian.start_day()
    safety_ok = guardian.can_trade()
    if not safety_ok and not broker.live.floor_tripped():
        summary = {
            "date": today,
            "status": "halted",
            "reason": guardian.state.halt_reason,
            "orders": [],
        }
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        return summary

    regime = market_regime(broker, cfg)
    if not regime.ok:
        summary = {
            "date": today,
            "status": "regime_blocked",
            "reason": regime.reason,
            "regime": regime.__dict__,
            "orders": [],
        }
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        return summary

    benchmark_row = None
    benchmark_frame = fetch_symbol_frame(broker, cfg.regime.benchmark_symbol, cfg)
    if benchmark_frame is not None and not benchmark_frame.empty:
        benchmark_features = build_feature_frame(benchmark_frame)
        benchmark_features = benchmark_features.assign(close=benchmark_frame["close"])
        benchmark_row = _latest_row(benchmark_features)

    universe = select_universe(broker, cfg)
    candidates = []
    orders_preview = []

    for symbol in universe:
        if symbol in state.submitted_today:
            continue
        frame = fetch_symbol_frame(broker, symbol, cfg)
        if frame is None or frame.empty:
            continue
        features = build_feature_frame(frame)
        features = features.assign(close=frame["close"], volume=frame["volume"])
        row = _latest_row(features)
        if row is None or pd.isna(row.get("close", float("nan"))):
            continue

        direction = composite_signal(features).dropna().iloc[-1]
        score = composite_score(features).dropna().iloc[-1]
        quote = _quote_with_spread_pct(broker.paper.latest_quote(symbol) or broker.live.latest_quote(symbol))
        snap = _decision_snapshot(symbol, direction, row, quote)
        quality = trend_quality_score(features).dropna().iloc[-1]

        if pd.isna(snap.rsi) or pd.isna(snap.mfi) or pd.isna(snap.momentum):
            continue
        if float(row.get("mom_126_21", 0.0)) < cfg.signals.min_momentum:
            continue
        relative_strength = float("nan")
        if benchmark_row is not None and cfg.signals.min_relative_strength_63 > -9:
            relative_strength = float(row.get("ret_63", float("nan"))) - float(
                benchmark_row.get("ret_63", float("nan"))
            )
            if pd.isna(relative_strength) or relative_strength < cfg.signals.min_relative_strength_63:
                log.info(
                    "skipping %s: 63-day relative strength %.2f%% below %.2f%%",
                    symbol,
                    relative_strength * 100 if not pd.isna(relative_strength) else float("nan"),
                    cfg.signals.min_relative_strength_63 * 100,
                )
                continue
        if float(quality) < cfg.signals.min_trend_quality:
            continue
        ok_downside, downside_reason = downside_ok(row, quote, cfg)
        if not ok_downside:
            log.info("skipping %s: %s", symbol, downside_reason)
            continue
        warning = memory.flag_if_repeating_loss(snap)
        if warning:
            log.info(warning)
            continue

        if direction == "no_trade":
            continue

        last_price = float(quote["ask"] if quote else row["close"])
        candidates.append(
            {
                "symbol": symbol,
                "direction": direction,
                "score": float(score),
                "trend_quality": float(quality),
                "relative_strength_63": relative_strength,
                "row": row,
                "quote": quote,
                "last_price": last_price,
                "snapshot": snap,
            }
        )

    candidates.sort(key=lambda item: (item["score"], item["trend_quality"]), reverse=True)
    selected = candidates[: cfg.sizing.target_n_positions]

    equity = broker.paper.equity() or broker.live.equity() or cfg.paper.starting_capital
    target_notional = equity / max(len(selected), 1)

    for item in selected:
        symbol = item["symbol"]
        quote = item["quote"]
        direction = item["direction"]
        row = item["row"]
        snap = item["snapshot"]
        if symbol in state.open_entries:
            continue

        price_for_sizing = item["last_price"] or float(row["close"])
        capped_notional = min(target_notional, equity * cfg.sizing.max_position_pct)
        capped_notional *= volatility_scale(float(row.get("rvol_20", float("nan"))), cfg)
        qty = max(0, math.floor(capped_notional / max(price_for_sizing, 0.01)))
        if qty <= 0:
            continue

        side = _side_for_signal(direction)
        order = _build_order(symbol, side, qty, quote, direction, cfg)
        paper_ok, paper_reason = guardian.validate_order(
            order,
            last_price=price_for_sizing,
            equity=broker.paper.equity() or equity,
            positions=broker.paper.positions(),
        )
        live_ok, live_reason = guardian.validate_order(
            order,
            last_price=price_for_sizing,
            equity=broker.live.equity() or equity,
            positions=broker.live.positions(),
        )
        if not paper_ok or not live_ok:
            log.info("order rejected for %s: paper=%s live=%s", symbol, paper_reason, live_reason)
            continue

        paper_fill = _entry_price_from_result(None, price_for_sizing, quote, side)
        live_fill = _entry_price_from_result(None, price_for_sizing, quote, side)

        if dry_run:
            orders_preview.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "trend_quality": item["trend_quality"],
                    "volatility_scale": volatility_scale(float(row.get("rvol_20", float("nan"))), cfg),
                    "order": order,
                    "dry_run": True,
                }
            )
            continue

        paper_result = None
        live_result = None
        paper_result = guardian.submit(order, broker.paper.submit)
        if not broker.live.floor_tripped():
            live_result = guardian.submit(order, broker.live.submit)

        if paper_result is not None:
            paper_fill = _entry_price_from_result(paper_result, price_for_sizing, quote, side)
        if live_result is not None:
            live_fill = _entry_price_from_result(live_result, price_for_sizing, quote, side)

        decision_id = memory.log_decision(
            snap,
            order_type=order["order_type"],
            intended_price=price_for_sizing,
            qty=qty,
            paper_fill=paper_fill,
            live_fill=live_fill,
        )
        if side == "buy" and live_result is not None:
            lot_id = memory.open_tax_lot(symbol, qty, live_fill * qty)
        else:
            lot_id = None

        state.submitted_today.append(symbol)
        state.open_entries[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "direction": direction,
            "decision_id": decision_id,
            "lot_id": lot_id,
            "entry_price": live_fill if live_fill else paper_fill,
            "opened_at": datetime.now(timezone.utc).isoformat(),
        }
        state.expected_positions["paper"][symbol] = {"qty": qty, "decision_id": decision_id}
        if live_result is not None and not broker.live.floor_tripped():
            state.expected_positions["live"][symbol] = {"qty": qty, "decision_id": decision_id}

        trail_percent = _trail_percent_for_row(row, cfg)
        if side == "buy" and paper_result is not None and not dry_run:
            broker.paper.attach_trailing_stop(symbol, qty, trail_percent)
        if side == "buy" and live_result is not None and not dry_run:
            broker.live.attach_trailing_stop(symbol, qty, trail_percent)

        orders_preview.append(
            {
                "symbol": symbol,
                "direction": direction,
                "trend_quality": item["trend_quality"],
                "volatility_scale": volatility_scale(float(row.get("rvol_20", float("nan"))), cfg),
                "trail_percent": trail_percent,
                "order": order,
                "decision_id": decision_id,
                "dry_run": dry_run,
            }
        )

    for symbol, record in list(state.open_entries.items()):
        if not cfg.exits.exit_on_signal_loss:
            continue
        current_signal = next((item for item in candidates if item["symbol"] == symbol), None)
        if current_signal is not None and current_signal["direction"] != "no_trade":
            continue
        quote = _quote_with_spread_pct(broker.paper.latest_quote(symbol) or broker.live.latest_quote(symbol))
        exit_side = "sell" if record.get("side") == "buy" else "buy"
        qty = record.get("qty", 0)
        if qty <= 0:
            continue
        exit_order = _build_order(symbol, exit_side, qty, quote, "mild_up", cfg)
        if dry_run:
            orders_preview.append(
                {
                    "symbol": symbol,
                    "direction": "exit",
                    "order": exit_order,
                    "dry_run": True,
                }
            )
            continue
        if record["side"] == "buy":
            guardian.submit(exit_order, broker.paper.submit)
            if not broker.live.floor_tripped():
                guardian.submit(exit_order, broker.live.submit)
        exit_price = _close_price_from_quote(quote, exit_side)
        holding_days = (
            datetime.now(timezone.utc) - datetime.fromisoformat(record["opened_at"])
        ).total_seconds() / 86400.0
        realized_pl = (exit_price - float(record["entry_price"])) * qty
        memory.close_decision(record["decision_id"], exit_price, realized_pl, holding_days)
        if record.get("lot_id"):
            memory.close_tax_lot(record["lot_id"], exit_price * qty)
        state.open_entries.pop(symbol, None)
        state.expected_positions["paper"].pop(symbol, None)
        state.expected_positions["live"].pop(symbol, None)

    if dry_run:
        return {
            "date": today,
            "status": "dry_run",
            "regime": regime.__dict__,
            "selected": [item["symbol"] for item in selected],
            "orders": orders_preview,
        }

    state.last_run_ts = datetime.now(timezone.utc).isoformat()
    state.save(cfg.paths.strategy_state_path)
    broker.save_state()

    reconcile_result = reconcile(broker, memory, cfg, guardian=guardian)
    summary = memory.daily_summary()
    status = broker.status()
    title, body = format_daily(summary, status)
    Notifier(cfg).send(title, body)

    return {
        "date": today,
        "status": "ok" if reconcile_result["ok"] else "needs_attention",
        "regime": regime.__dict__,
        "selected": [item["symbol"] for item in selected],
        "orders": orders_preview,
        "summary": summary,
        "broker": status,
        "reconcile": reconcile_result,
    }
