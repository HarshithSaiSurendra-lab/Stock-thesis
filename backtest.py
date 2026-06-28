"""
backtest.py — A bias-aware cross-sectional backtest engine.

This engine is built around the three lies that kill retail backtests:

  1. LOOK-AHEAD BIAS. Signals are computed on data up to close of day t, but
     positions are entered at the NEXT bar's open (t+1). You can never trade on
     information you wouldn't have had yet. This is enforced by an explicit
     one-bar shift of the target weights — see `_lag_weights`.

  2. SURVIVORSHIP BIAS. The engine accepts a panel where symbols can enter and
     leave (NaN before listing / after delisting). It NEVER assumes a symbol
     that exists today existed in the past. (You still need a survivorship-free
     data source to feed it — the engine can't invent delisted tickers — but it
     won't break or silently forward-fill across gaps.)

  3. COST-FREE FANTASY. Every position change pays a per-dollar transaction cost
     (commission + slippage). A strategy that only works at zero cost is not a
     strategy.

Scope: cross-sectional long (or long/short) equity, daily rebalance, weights
derived from a signal. This is deliberately simple and auditable rather than
feature-complete. Read every line; trust nothing you haven't read.
"""

from dataclasses import dataclass, field
import numpy as np
import pandas as pd


@dataclass
class BacktestConfig:
    cost_bps: float = 5.0          # round-trip cost in basis points of traded notional
    long_only: bool = True         # if False, allows short positions
    max_positions: int = 10        # cap on simultaneous names
    gross_leverage: float = 1.0    # total gross exposure (1.0 = fully invested)
    vol_target: float | None = None  # if set, scale exposure to this annual vol
    rebalance_every: int = 1       # trading days between rebalances (1 = daily)
    throttle_drawdown_pct: float | None = None  # e.g. 0.05 means scale down after -5%
    throttle_scale: float = 0.50
    halt_drawdown_pct: float | None = None      # e.g. 0.10 means no exposure after -10%
    halt_scale: float = 0.0


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    daily_returns: pd.Series
    weights: pd.DataFrame
    turnover: pd.Series
    stats: dict = field(default_factory=dict)


def _lag_weights(target_weights: pd.DataFrame) -> pd.DataFrame:
    """
    THE look-ahead guard. Signals known at close of day t can only be acted on
    at day t+1. We shift the entire target-weight matrix forward by one bar so
    that returns earned on day t+1 are multiplied by weights decided at t.
    """
    return target_weights.shift(1)


def signal_to_weights(
    signal: pd.DataFrame, cfg: BacktestConfig
) -> pd.DataFrame:
    """
    Convert a cross-sectional signal panel (index=dates, cols=symbols) into
    target portfolio weights. Higher signal = more weight. Names that are NaN
    for a given date (not yet listed / delisted / insufficient history) are
    simply not eligible that day — this is the survivorship guard in action.
    """
    w = pd.DataFrame(0.0, index=signal.index, columns=signal.columns)

    for date, row in signal.iterrows():
        valid = row.dropna()
        if valid.empty:
            continue

        if cfg.long_only:
            # rank-select the top names by signal
            top = valid.nlargest(min(cfg.max_positions, len(valid)))
            if (top > 0).any():
                top = top[top > 0]  # only take genuinely positive signals
            if top.empty:
                continue
            weights = pd.Series(1.0 / len(top), index=top.index)
        else:
            # long top half, short bottom half
            n = min(cfg.max_positions, len(valid))
            longs = valid.nlargest(n // 2)
            shorts = valid.nsmallest(n // 2)
            weights = pd.concat([
                pd.Series(0.5 / max(len(longs), 1), index=longs.index),
                pd.Series(-0.5 / max(len(shorts), 1), index=shorts.index),
            ])

        weights *= cfg.gross_leverage
        w.loc[date, weights.index] = weights.values

    # apply rebalance frequency: hold weights between rebalance dates
    if cfg.rebalance_every > 1:
        mask = np.zeros(len(w), dtype=bool)
        mask[::cfg.rebalance_every] = True
        w = w.where(pd.Series(mask, index=w.index), np.nan).ffill().fillna(0.0)

    return w


def run_backtest(
    prices: pd.DataFrame,
    signal: pd.DataFrame,
    cfg: BacktestConfig = BacktestConfig(),
) -> BacktestResult:
    """
    prices : DataFrame of close prices (index=dates, cols=symbols). Survivorship-
             free: symbols may be NaN outside their listing window.
    signal : DataFrame of the cross-sectional signal, SAME shape/index as prices.
             Computed using only data up to and including each date.
    """
    if not prices.index.equals(signal.index):
        raise ValueError("prices and signal must share the same index")

    # forward returns earned from t to t+1, per symbol
    fwd_ret = prices.pct_change(fill_method=None).shift(-1)  # return realized on the NEXT bar

    target_w = signal_to_weights(signal, cfg)
    held_w = _lag_weights(target_w)          # <-- look-ahead guard

    if cfg.throttle_drawdown_pct is not None or cfg.halt_drawdown_pct is not None:
        net_ret, held_w, weight_change = _run_path_dependent_backtest(held_w, fwd_ret, cfg)
    else:
        # portfolio return on day t = sum_i held_w[t,i] * (return from t to t+1)
        # align: held_w decided using info up to t-1 close, applied to t->t+1 move
        gross_ret = (held_w * fwd_ret).sum(axis=1)

        # transaction costs: pay on the change in weights at each rebalance
        weight_change = held_w.diff().abs().sum(axis=1).fillna(0.0)
        cost = weight_change * (cfg.cost_bps / 1e4)
        net_ret = (gross_ret - cost).dropna()

    # optional volatility targeting (scales the whole curve, applied ex-post for clarity)
    if cfg.vol_target is not None:
        realized = net_ret.rolling(20).std() * np.sqrt(252)
        scale = (cfg.vol_target / realized).clip(upper=3.0).shift(1).fillna(1.0)
        net_ret = net_ret * scale

    equity = (1 + net_ret).cumprod()
    stats = compute_stats(net_ret, weight_change)

    return BacktestResult(
        equity_curve=equity,
        daily_returns=net_ret,
        weights=held_w,
        turnover=weight_change,
        stats=stats,
    )


def _drawdown_scale(equity: float, peak: float, cfg: BacktestConfig) -> float:
    if peak <= 0:
        return 1.0
    drawdown = equity / peak - 1.0
    if cfg.halt_drawdown_pct is not None and drawdown <= -cfg.halt_drawdown_pct:
        return cfg.halt_scale
    if cfg.throttle_drawdown_pct is not None and drawdown <= -cfg.throttle_drawdown_pct:
        return cfg.throttle_scale
    return 1.0


def _run_path_dependent_backtest(
    held_w: pd.DataFrame, fwd_ret: pd.DataFrame, cfg: BacktestConfig
) -> tuple[pd.Series, pd.DataFrame, pd.Series]:
    """
    Path-dependent risk control using only prior portfolio equity.
    This can cut exposure after drawdown without looking at future returns.
    """
    equity = 1.0
    peak = 1.0
    prev_w = pd.Series(0.0, index=held_w.columns)
    scaled_rows = []
    returns = []
    turnovers = []

    for dt in held_w.index:
        scale = _drawdown_scale(equity, peak, cfg)
        desired_w = held_w.loc[dt].fillna(0.0) * scale
        turnover = (desired_w - prev_w).abs().sum()
        gross_ret = (desired_w * fwd_ret.loc[dt].fillna(0.0)).sum()
        cost = turnover * (cfg.cost_bps / 1e4)
        net = gross_ret - cost

        returns.append(net)
        turnovers.append(turnover)
        scaled_rows.append(desired_w)

        equity *= 1 + net
        peak = max(peak, equity)
        prev_w = desired_w

    scaled_w = pd.DataFrame(scaled_rows, index=held_w.index, columns=held_w.columns)
    return (
        pd.Series(returns, index=held_w.index).dropna(),
        scaled_w,
        pd.Series(turnovers, index=held_w.index).fillna(0.0),
    )


def compute_stats(returns: pd.Series, turnover: pd.Series) -> dict:
    """Standard performance diagnostics. Annualization assumes 252 trading days."""
    if returns.empty or returns.std() == 0:
        return {"error": "no return variation"}
    total_multiple = (1 + returns).prod()
    ann_ret = total_multiple ** (252 / len(returns)) - 1 if total_multiple > 0 else np.nan
    ann_vol = returns.std() * np.sqrt(252)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else np.nan
    downside = returns[returns < 0].std() * np.sqrt(252)
    sortino = ann_ret / downside if downside > 0 else np.nan
    equity = (1 + returns).cumprod()
    drawdown = (equity / equity.cummax() - 1).min()
    hit_rate = (returns > 0).mean()
    avg_turnover = turnover.mean()
    return {
        "annual_return": round(ann_ret, 4),
        "annual_vol": round(ann_vol, 4),
        "sharpe": round(sharpe, 3),
        "sortino": round(sortino, 3),
        "max_drawdown": round(drawdown, 4),
        "hit_rate": round(hit_rate, 4),
        "avg_daily_turnover": round(avg_turnover, 4),
        "n_days": len(returns),
    }


def walk_forward_splits(index: pd.DatetimeIndex, n_splits: int = 4):
    """
    Yield (train_idx, test_idx) tuples for walk-forward / out-of-sample testing.
    Expanding window: each test period uses only data strictly before it for any
    fitting you do. This is how you avoid fooling yourself with one lucky split.
    """
    fold = len(index) // (n_splits + 1)
    for i in range(1, n_splits + 1):
        train_end = fold * i
        test_end = fold * (i + 1)
        yield index[:train_end], index[train_end:test_end]


if __name__ == "__main__":
    # End-to-end smoke test on a synthetic survivorship-aware panel.
    rng = np.random.default_rng(7)
    dates = pd.bdate_range("2020-01-01", periods=750)
    symbols = [f"SYM{i}" for i in range(20)]

    # build correlated-ish random-walk prices; introduce a real momentum effect
    price_panel = {}
    for s in symbols:
        drift = rng.normal(0.0004, 0.0002)
        rets = rng.normal(drift, 0.018, len(dates))
        # inject mild autocorrelation so momentum has something to find
        rets = pd.Series(rets).ewm(span=5).mean().values
        price_panel[s] = 100 * np.exp(np.cumsum(rets))
    prices = pd.DataFrame(price_panel, index=dates)

    # simulate survivorship: two names "list late", one "delists early"
    prices.loc[:dates[100], "SYM18"] = np.nan
    prices.loc[:dates[200], "SYM19"] = np.nan
    prices.loc[dates[600]:, "SYM0"] = np.nan

    # signal = 6-1 momentum, computed per symbol with no look-ahead
    signal = prices.shift(21) / prices.shift(126) - 1.0

    for cost in (0.0, 5.0, 15.0):
        cfg = BacktestConfig(cost_bps=cost, long_only=True, max_positions=5)
        res = run_backtest(prices, signal, cfg)
        print(f"\n=== cost = {cost} bps ===")
        for k, v in res.stats.items():
            print(f"  {k:>20}: {v}")
