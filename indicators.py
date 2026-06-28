"""
indicators.py — Volume-flow + momentum indicator library.

Implements the six-indicator system: KVO, OBV, Weighted A/D, MFI, RSI, and
moving averages. Every function is vectorized, takes an OHLCV DataFrame, and
returns a Series aligned to the input index.

CRITICAL DESIGN RULE: every indicator at row t uses ONLY data from rows <= t.
No function here peeks at the future. This is the first line of defense against
look-ahead bias. When you later shift signals for execution, that is a SECOND
layer on top of this one.

Expected input: a DataFrame with columns ['open','high','low','close','volume'],
indexed by a DatetimeIndex, for a SINGLE symbol.
"""

import numpy as np
import pandas as pd


def _require_cols(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")


def sma(close: pd.Series, window: int) -> pd.Series:
    """Simple moving average."""
    return close.rolling(window, min_periods=window).mean()


def ema(close: pd.Series, span: int) -> pd.Series:
    """Exponential moving average."""
    return close.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """
    Relative Strength Index (Wilder's smoothing).
    Returns values in [0, 100]. >70 conventionally overbought, <30 oversold.
    """
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder's smoothing == EMA with alpha = 1/window
    avg_gain = gain.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    avg_loss = loss.ewm(alpha=1 / window, adjust=False, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # When avg_loss == 0 -> RSI 100; when avg_gain == 0 -> RSI 0
    out = out.where(avg_loss != 0, 100.0)
    out = out.where(avg_gain != 0, out)
    out = out.where(~((avg_gain == 0) & (avg_loss == 0)), 50.0)
    return out


def obv(df: pd.DataFrame) -> pd.Series:
    """
    On-Balance Volume. Cumulative volume, signed by daily price direction.
    A running total: +volume on up days, -volume on down days.
    """
    _require_cols(df, ["close", "volume"])
    direction = np.sign(df["close"].diff()).fillna(0.0)
    return (direction * df["volume"]).cumsum()


def mfi(df: pd.DataFrame, window: int = 14) -> pd.Series:
    """
    Money Flow Index — a volume-weighted RSI. Range [0, 100].
    Uses typical price (H+L+C)/3 times volume as 'raw money flow'.
    """
    _require_cols(df, ["high", "low", "close", "volume"])
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    raw_flow = tp * df["volume"]
    tp_change = tp.diff()
    pos_flow = raw_flow.where(tp_change > 0, 0.0)
    neg_flow = raw_flow.where(tp_change < 0, 0.0)
    pos_sum = pos_flow.rolling(window, min_periods=window).sum()
    neg_sum = neg_flow.rolling(window, min_periods=window).sum()
    money_ratio = pos_sum / neg_sum.replace(0.0, np.nan)
    out = 100 - (100 / (1 + money_ratio))
    out = out.where(neg_sum != 0, 100.0)
    return out


def williams_ad(df: pd.DataFrame) -> pd.Series:
    """
    Williams Accumulation/Distribution (the 'Weighted A/D' in the system).
    Cumulative line driven by where close sits relative to the prior close
    and the current true range. Rising = accumulation, falling = distribution.
    """
    _require_cols(df, ["high", "low", "close"])
    prev_close = df["close"].shift(1)
    true_high = pd.concat([df["high"], prev_close], axis=1).max(axis=1)
    true_low = pd.concat([df["low"], prev_close], axis=1).min(axis=1)

    ad = pd.Series(0.0, index=df.index)
    up = df["close"] > prev_close
    down = df["close"] < prev_close
    ad = ad.where(~up, df["close"] - true_low)
    ad = ad.where(~down, df["close"] - true_high)
    ad = ad.where(up | down, 0.0)  # unchanged close -> 0 contribution
    return ad.cumsum()


def kvo(df: pd.DataFrame, fast: int = 34, slow: int = 55, signal: int = 13):
    """
    Klinger Volume Oscillator. Returns (kvo_line, signal_line).
    Volume force is signed by the direction of the typical price, scaled by
    a trend factor; the oscillator is fast_EMA - slow_EMA of that volume force.
    """
    _require_cols(df, ["high", "low", "close", "volume"])
    hlc = (df["high"] + df["low"] + df["close"]) / 3.0
    trend = np.sign(hlc.diff()).fillna(0.0)
    # cumulative measurement of trend persistence
    dm = df["high"] - df["low"]
    cm = dm.copy()
    # volume force
    vf = df["volume"] * trend * dm.abs() * 2.0
    kvo_line = (
        vf.ewm(span=fast, adjust=False, min_periods=fast).mean()
        - vf.ewm(span=slow, adjust=False, min_periods=slow).mean()
    )
    signal_line = kvo_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return kvo_line, signal_line


def realized_vol(close: pd.Series, window: int = 20) -> pd.Series:
    """Annualized realized volatility from daily log returns."""
    log_ret = np.log(close / close.shift(1))
    return log_ret.rolling(window, min_periods=window).std() * np.sqrt(252)


def rolling_drawdown(close: pd.Series, window: int = 63) -> pd.Series:
    """Drawdown from the rolling high over `window` bars."""
    high_water = close.rolling(window, min_periods=window).max()
    return close / high_water - 1.0


def momentum(close: pd.Series, lookback: int = 126, skip: int = 21) -> pd.Series:
    """
    Cross-sectional momentum signal: return over `lookback` days, but skipping
    the most recent `skip` days (the classic 12-1 / 6-1 construction that omits
    the short-term reversal month). Jegadeesh & Titman (1993).
    """
    return close.shift(skip) / close.shift(lookback) - 1.0


def build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute the full indicator set for one symbol and return a feature DataFrame
    aligned to the input index. Each row uses only past/current data.
    """
    _require_cols(df, ["open", "high", "low", "close", "volume"])
    kvo_line, kvo_sig = kvo(df)
    feats = pd.DataFrame(index=df.index)
    feats["ret_1d"] = df["close"].pct_change()
    feats["sma_20"] = sma(df["close"], 20)
    feats["sma_50"] = sma(df["close"], 50)
    feats["sma_200"] = sma(df["close"], 200)
    feats["ema_12"] = ema(df["close"], 12)
    feats["rsi_14"] = rsi(df["close"], 14)
    feats["obv"] = obv(df)
    feats["mfi_14"] = mfi(df, 14)
    feats["wad"] = williams_ad(df)
    feats["kvo"] = kvo_line
    feats["kvo_signal"] = kvo_sig
    feats["kvo_hist"] = kvo_line - kvo_sig
    feats["rvol_20"] = realized_vol(df["close"], 20)
    feats["drawdown_63"] = rolling_drawdown(df["close"], 63)
    feats["mom_126_21"] = momentum(df["close"], 126, 21)
    # Normalized OBV/WAD slopes (raw cumulative levels aren't comparable across symbols)
    feats["obv_slope_20"] = feats["obv"].diff(20) / df["volume"].rolling(20).mean()
    feats["wad_slope_20"] = feats["wad"].diff(20) / df["close"]
    return feats


if __name__ == "__main__":
    # Smoke test on synthetic data so the module is runnable standalone.
    rng = np.random.default_rng(42)
    n = 400
    idx = pd.bdate_range("2023-01-01", periods=n)
    price = 100 * np.exp(np.cumsum(rng.normal(0.0003, 0.015, n)))
    high = price * (1 + rng.uniform(0, 0.01, n))
    low = price * (1 - rng.uniform(0, 0.01, n))
    vol = rng.integers(1_000_000, 5_000_000, n).astype(float)
    df = pd.DataFrame(
        {"open": price, "high": high, "low": low, "close": price, "volume": vol},
        index=idx,
    )
    feats = build_feature_frame(df)
    print("Feature frame shape:", feats.shape)
    print("\nLast 3 rows of key features:")
    print(feats[["rsi_14", "mfi_14", "kvo_hist", "mom_126_21"]].tail(3).round(3))
    print("\nNaN counts (expected: warmup periods only):")
    print(feats.isna().sum())
