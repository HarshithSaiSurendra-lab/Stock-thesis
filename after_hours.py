from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from config import TradingConfig
from filters import market_regime
from indicators import build_feature_frame
from memory import TradeMemory
from sector_map import sector_benchmark_for, sector_benchmarks_for
from strategy_runner import (
    StrategyState,
    _decision_rank,
    _decision_report_item,
    _fill_price,
    _latest_feature_row_for_symbol,
    _latest_row,
    _nullable_float,
    _position_payload_for_validation,
    _positions_notional,
    _quote_with_spread_pct,
    _relative_strength_63,
    _skip_summary,
    _strategy_equity,
    _strategy_owned_symbols,
    _write_decision_log,
    _decision_snapshot,
)
from trade_signal import composite_score, composite_signal, trend_quality_score
from universe import fetch_symbol_frame

log = logging.getLogger("after_hours")


def _parse_quote_timestamp(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            cleaned = value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(cleaned)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _quote_age_seconds(quote: dict, now: Optional[datetime] = None) -> Optional[float]:
    ts = _parse_quote_timestamp(quote.get("timestamp") or quote.get("t"))
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts.astimezone(timezone.utc)).total_seconds())


def _quote_mid(quote: dict) -> float:
    return (float(quote["bid"]) + float(quote["ask"])) / 2


def _after_hours_limit_price(quote: dict, cfg: TradingConfig) -> float:
    bid = float(quote["bid"])
    ask = float(quote["ask"])
    mid = _quote_mid(quote)
    limit = min(ask, mid * (1 + cfg.after_hours.limit_offset_pct))
    return round(max(bid, limit), 2)


def _after_hours_quote_ok(
    quote: Optional[dict],
    cfg: TradingConfig,
    now: Optional[datetime] = None,
) -> tuple[bool, str, Optional[dict]]:
    quote = _quote_with_spread_pct(quote)
    if quote is None:
        return False, "valid quote unavailable", None
    spread_pct = float(quote.get("spread_pct", 1.0))
    if spread_pct > cfg.after_hours.max_spread_pct:
        return False, f"spread {spread_pct:.2%} above cap {cfg.after_hours.max_spread_pct:.2%}", quote
    age = _quote_age_seconds(quote, now)
    quote["age_seconds"] = age
    if age is not None and age > cfg.after_hours.max_quote_age_seconds:
        return False, (
            f"quote age {age:.0f}s above cap "
            f"{cfg.after_hours.max_quote_age_seconds}s"
        ), quote
    return True, "ok", quote


def _build_after_hours_order(symbol: str, qty: int, quote: dict, cfg: TradingConfig) -> dict:
    return {
        "symbol": symbol,
        "side": "buy",
        "qty": qty,
        "order_type": "limit",
        "limit_price": _after_hours_limit_price(quote, cfg),
        "time_in_force": cfg.after_hours.time_in_force.lower(),
        "extended_hours": True,
    }


def _after_hours_candidate_report(item: dict) -> dict:
    out = _decision_report_item(item)
    out.update(
        {
            "after_hours_move_pct": round(item["after_hours_move_pct"], 4),
            "after_hours_score": round(item["after_hours_score"], 3),
            "quote_age_seconds": (
                None if item["quote"].get("age_seconds") is None else round(item["quote"]["age_seconds"], 1)
            ),
        }
    )
    return out


def _after_hours_score(item: dict, cfg: TradingConfig) -> float:
    quote = item["quote"]
    spread_pct = float(quote.get("spread_pct", cfg.after_hours.max_spread_pct))
    move_pct = float(item["after_hours_move_pct"])
    spread_component = max(0.0, 1.0 - spread_pct / max(cfg.after_hours.max_spread_pct, 0.0001))
    move_component = min(move_pct / max(cfg.after_hours.min_after_hours_move_pct, 0.0001), 4.0) * 0.35
    return float(item["decision_score"]) + spread_component + move_component


def run_after_hours(
    broker,
    guardian,
    memory: TradeMemory,
    cfg: TradingConfig,
    dry_run: bool = False,
) -> dict:
    cfg.ensure_paths()
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc)
    state = StrategyState.load(cfg.paths.strategy_state_path)
    state.start_day(today)
    guardian.start_day()

    if not cfg.after_hours.enabled:
        summary = {
            "date": today,
            "status": "after_hours_disabled",
            "reason": "AFTER_HOURS_ENABLED is off",
            "orders": [],
        }
        _write_decision_log(summary, cfg)
        return summary
    if not dry_run and not cfg.after_hours.allow_real_orders:
        summary = {
            "date": today,
            "status": "after_hours_disabled",
            "reason": "real after-hours submissions require ALLOW_AFTER_HOURS=1",
            "orders": [],
        }
        _write_decision_log(summary, cfg)
        return summary

    safety_ok = guardian.can_trade()
    if not safety_ok and not broker.live.floor_tripped():
        summary = {
            "date": today,
            "status": "halted",
            "reason": guardian.state.halt_reason,
            "orders": [],
        }
        _write_decision_log(summary, cfg)
        return summary

    regime = market_regime(broker, cfg)
    if not regime.ok:
        summary = {
            "date": today,
            "status": "regime_blocked",
            "session": "after_hours",
            "reason": regime.reason,
            "regime": regime.__dict__,
            "orders": [],
        }
        _write_decision_log(summary, cfg)
        return summary

    benchmark_row = None
    benchmark_frame = fetch_symbol_frame(broker, cfg.regime.benchmark_symbol, cfg)
    if benchmark_frame is not None and not benchmark_frame.empty:
        benchmark_features = build_feature_frame(benchmark_frame)
        benchmark_features = benchmark_features.assign(close=benchmark_frame["close"])
        benchmark_row = _latest_row(benchmark_features)

    sector_rows = {}
    if cfg.sector.enabled:
        for sector_symbol in sector_benchmarks_for(cfg.after_hours.symbols):
            row = _latest_feature_row_for_symbol(broker, sector_symbol, cfg)
            if row is not None:
                sector_rows[sector_symbol] = row

    candidates = []
    skipped = []
    orders_preview = []

    for symbol in cfg.after_hours.symbols:
        if symbol in state.submitted_today:
            skipped.append({"symbol": symbol, "stage": "dedupe", "reason": "already submitted today"})
            continue
        frame = fetch_symbol_frame(broker, symbol, cfg)
        if frame is None or frame.empty:
            skipped.append({"symbol": symbol, "stage": "data", "reason": "market data unavailable"})
            continue
        features = build_feature_frame(frame).assign(close=frame["close"], volume=frame["volume"])
        row = _latest_row(features)
        if row is None:
            skipped.append({"symbol": symbol, "stage": "indicators", "reason": "latest indicators unavailable"})
            continue
        quote_ok, quote_reason, quote = _after_hours_quote_ok(
            broker.paper.latest_quote(symbol) or broker.live.latest_quote(symbol),
            cfg,
            now,
        )
        if not quote_ok or quote is None:
            skipped.append({"symbol": symbol, "stage": "risk", "reason": quote_reason})
            log.info("skipping %s: %s", symbol, quote_reason)
            continue

        close = float(row.get("close", float("nan")))
        if not math.isfinite(close) or close <= 0:
            skipped.append({"symbol": symbol, "stage": "indicators", "reason": "latest close unavailable"})
            continue
        ah_move = _quote_mid(quote) / close - 1.0
        if ah_move < cfg.after_hours.min_after_hours_move_pct:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "after_hours",
                    "reason": (
                        f"after-hours move {ah_move:.2%} below "
                        f"{cfg.after_hours.min_after_hours_move_pct:.2%}"
                    ),
                }
            )
            continue
        if ah_move > cfg.after_hours.max_after_hours_move_pct:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "after_hours",
                    "reason": (
                        f"after-hours move {ah_move:.2%} above chase cap "
                        f"{cfg.after_hours.max_after_hours_move_pct:.2%}"
                    ),
                }
            )
            continue

        dollar_volume = float(frame["close"].tail(20).mul(frame["volume"].tail(20)).mean())
        if dollar_volume < cfg.after_hours.min_dollar_volume:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "risk",
                    "reason": (
                        f"20-day dollar volume ${dollar_volume:,.0f} below "
                        f"${cfg.after_hours.min_dollar_volume:,.0f}"
                    ),
                }
            )
            continue

        direction_series = composite_signal(features).dropna()
        score_series = composite_score(features).dropna()
        quality_series = trend_quality_score(features).dropna()
        if direction_series.empty or score_series.empty or quality_series.empty:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "signal unavailable"})
            continue
        direction = direction_series.iloc[-1]
        score = float(score_series.iloc[-1])
        quality = float(quality_series.iloc[-1])
        if direction not in {"strong_up", "mild_up"}:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "daily signal not long"})
            continue
        if quality < cfg.after_hours.min_trend_quality:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "trend quality below after-hours minimum"})
            continue
        momentum = float(row.get("mom_126_21", float("nan")))
        if pd.isna(momentum) or momentum < cfg.after_hours.min_momentum:
            skipped.append({"symbol": symbol, "stage": "signal", "reason": "momentum below after-hours minimum"})
            continue

        relative_strength = _relative_strength_63(row, benchmark_row)
        if pd.isna(relative_strength) or relative_strength < cfg.after_hours.min_relative_strength_63:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "signal",
                    "reason": (
                        f"63-day relative strength "
                        f"{relative_strength * 100 if not pd.isna(relative_strength) else float('nan'):.2f}% "
                        f"below {cfg.after_hours.min_relative_strength_63 * 100:.2f}%"
                    ),
                }
            )
            continue

        sector_benchmark = sector_benchmark_for(symbol) if cfg.sector.enabled else None
        sector_relative_strength = _relative_strength_63(row, sector_rows.get(sector_benchmark))
        if sector_benchmark and cfg.sector.min_relative_strength_63 > -9:
            if pd.isna(sector_relative_strength) or sector_relative_strength < cfg.sector.min_relative_strength_63:
                skipped.append(
                    {
                        "symbol": symbol,
                        "stage": "signal",
                        "reason": (
                            f"63-day sector relative strength "
                            f"{sector_relative_strength * 100 if not pd.isna(sector_relative_strength) else float('nan'):.2f}% "
                            f"vs {sector_benchmark} below {cfg.sector.min_relative_strength_63 * 100:.2f}%"
                        ),
                    }
                )
                continue

        rank = _decision_rank(row, quote, score, quality, relative_strength, sector_relative_strength, cfg)
        if rank["decision_score"] < cfg.after_hours.min_decision_score:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "signal",
                    "reason": (
                        f"decision score {rank['decision_score']:.2f} below "
                        f"after-hours minimum {cfg.after_hours.min_decision_score:.2f}"
                    ),
                }
            )
            continue

        item = {
            "symbol": symbol,
            "direction": direction,
            "score": score,
            "trend_quality": quality,
            "decision_score": rank["decision_score"],
            "rank": rank,
            "relative_strength_63": relative_strength,
            "sector_benchmark": sector_benchmark,
            "sector_relative_strength_63": sector_relative_strength,
            "row": row,
            "quote": quote,
            "last_price": _quote_mid(quote),
            "snapshot": _decision_snapshot(symbol, direction, row, quote),
            "after_hours_move_pct": ah_move,
        }
        item["after_hours_score"] = _after_hours_score(item, cfg)
        candidates.append(item)

    candidates.sort(
        key=lambda item: (item["after_hours_score"], item["decision_score"], item["trend_quality"]),
        reverse=True,
    )
    selected = candidates[: cfg.after_hours.target_n_positions]

    paper_equity = broker.paper.equity()
    live_equity = broker.live.equity()
    account_equity = paper_equity or live_equity or cfg.paper.starting_capital
    base_equity = _strategy_equity(cfg, account_equity)
    after_hours_equity = base_equity * cfg.after_hours.position_scale
    paper_positions = broker.paper.positions()
    live_positions = broker.live.positions()
    strategy_symbols = _strategy_owned_symbols(state)
    existing_strategy_notional = _positions_notional(paper_positions, strategy_symbols)
    max_deployed = after_hours_equity * cfg.sizing.max_deployed_pct
    remaining_budget = max(0.0, max_deployed - existing_strategy_notional)
    blocked_symbols = set(state.open_entries) | set(paper_positions) | set(live_positions)
    eligible_new_count = len([item for item in selected if item["symbol"] not in blocked_symbols])
    target_notional = remaining_budget / max(eligible_new_count, 1)

    for item in selected:
        symbol = item["symbol"]
        quote = item["quote"]
        if symbol in state.open_entries:
            skipped.append({"symbol": symbol, "stage": "sizing", "reason": "already open"})
            continue
        if symbol in paper_positions or symbol in live_positions:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "sizing",
                    "reason": "untracked existing broker position; not mixing strategy and manual lots",
                }
            )
            continue
        price_for_sizing = float(item["last_price"])
        capped_notional = min(target_notional, after_hours_equity * cfg.sizing.max_position_pct)
        qty = max(0, math.floor(capped_notional / max(price_for_sizing, 0.01)))
        intended_notional = qty * price_for_sizing
        if qty <= 0:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "sizing",
                    "reason": "calculated whole-share quantity is zero",
                    "price": round(price_for_sizing, 2),
                    "target_notional": round(capped_notional, 2),
                }
            )
            continue

        order = _build_after_hours_order(symbol, qty, quote, cfg)
        paper_ok, paper_reason = guardian.validate_order(
            order,
            last_price=price_for_sizing,
            equity=base_equity,
            positions=_position_payload_for_validation(paper_positions, strategy_symbols | {symbol}),
        )
        live_ok = True
        live_reason = "live disabled"
        if cfg.after_hours.submit_live:
            live_ok, live_reason = guardian.validate_order(
                order,
                last_price=price_for_sizing,
                equity=base_equity,
                positions=_position_payload_for_validation(live_positions, strategy_symbols | {symbol}),
            )
        if not paper_ok or not live_ok:
            skipped.append(
                {
                    "symbol": symbol,
                    "stage": "guardian",
                    "reason": f"paper={paper_reason}; live={live_reason}",
                    "intended_notional": round(intended_notional, 2),
                }
            )
            continue

        if dry_run:
            orders_preview.append(
                {
                    "symbol": symbol,
                    "direction": item["direction"],
                    "session": "after_hours",
                    "after_hours_score": item["after_hours_score"],
                    "decision_score": item["decision_score"],
                    "after_hours_move_pct": item["after_hours_move_pct"],
                    "target_notional": round(capped_notional, 2),
                    "intended_notional": round(intended_notional, 2),
                    "quote_age_seconds": quote.get("age_seconds"),
                    "stale_order_seconds": cfg.after_hours.stale_order_seconds,
                    "order": order,
                    "dry_run": True,
                }
            )
            continue

        paper_result = guardian.submit(order, broker.paper.submit)
        live_result = None
        if cfg.after_hours.submit_live and not broker.live.floor_tripped():
            live_result = guardian.submit(order, broker.live.submit)

        if paper_result is None:
            continue
        paper_fill = _fill_price(quote, "buy")
        live_fill = paper_fill if live_result else None
        decision_id = memory.log_decision(
            item["snapshot"],
            order_type=order["order_type"],
            intended_price=price_for_sizing,
            qty=qty,
            paper_fill=paper_fill,
            live_fill=live_fill,
        )
        state.submitted_today.append(symbol)
        state.open_entries[symbol] = {
            "symbol": symbol,
            "qty": qty,
            "side": "buy",
            "direction": item["direction"],
            "session": "after_hours",
            "decision_id": decision_id,
            "entry_price": live_fill if live_fill else paper_fill,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "stale_order_seconds": cfg.after_hours.stale_order_seconds,
        }
        state.expected_positions["paper"][symbol] = {"qty": qty, "decision_id": decision_id}
        if live_result is not None:
            state.expected_positions["live"][symbol] = {"qty": qty, "decision_id": decision_id}

        orders_preview.append(
            {
                "symbol": symbol,
                "direction": item["direction"],
                "session": "after_hours",
                "after_hours_score": item["after_hours_score"],
                "decision_score": item["decision_score"],
                "after_hours_move_pct": item["after_hours_move_pct"],
                "target_notional": round(capped_notional, 2),
                "intended_notional": round(intended_notional, 2),
                "quote_age_seconds": quote.get("age_seconds"),
                "stale_order_seconds": cfg.after_hours.stale_order_seconds,
                "order": order,
                "decision_id": decision_id,
                "dry_run": False,
            }
        )

    if not dry_run:
        state.last_run_ts = datetime.now(timezone.utc).isoformat()
        state.save(cfg.paths.strategy_state_path)
        broker.save_state()

    summary = {
        "date": today,
        "status": "after_hours_dry_run" if dry_run else "after_hours_ok",
        "session": "after_hours",
        "regime": regime.__dict__,
        "after_hours_config": {
            "allow_real_orders": cfg.after_hours.allow_real_orders,
            "submit_live": cfg.after_hours.submit_live,
            "symbols": cfg.after_hours.symbols,
            "target_positions": cfg.after_hours.target_n_positions,
            "position_scale": cfg.after_hours.position_scale,
            "max_spread_pct": cfg.after_hours.max_spread_pct,
            "min_move_pct": cfg.after_hours.min_after_hours_move_pct,
            "max_move_pct": cfg.after_hours.max_after_hours_move_pct,
            "min_decision_score": cfg.after_hours.min_decision_score,
            "stale_order_seconds": cfg.after_hours.stale_order_seconds,
        },
        "budget": {
            "strategy_capital": cfg.sizing.strategy_capital,
            "account_equity": account_equity,
            "regular_sizing_equity": base_equity,
            "after_hours_sizing_equity": round(after_hours_equity, 2),
            "max_deployed": round(max_deployed, 2),
            "existing_notional": round(existing_strategy_notional, 2),
            "remaining_budget": round(remaining_budget, 2),
        },
        "candidates": [_after_hours_candidate_report(item) for item in candidates],
        "decision_report": [_after_hours_candidate_report(item) for item in candidates],
        "selected": [item["symbol"] for item in selected],
        "orders": orders_preview,
        "skip_summary": _skip_summary(skipped),
        "skipped": skipped,
    }
    _write_decision_log(summary, cfg)
    return summary
