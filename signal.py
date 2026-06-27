from __future__ import annotations

import numpy as np
import pandas as pd


REQUIRED_COLUMNS = {
    "close",
    "sma_20",
    "sma_50",
    "mom_126_21",
    "obv_slope_20",
    "kvo_hist",
    "rsi_14",
    "mfi_14",
    "rvol_20",
}


def _ensure_required(features: pd.DataFrame) -> None:
    missing = sorted(REQUIRED_COLUMNS.difference(features.columns))
    if missing:
        raise ValueError(f"feature frame missing required columns: {missing}")


def composite_score(features: pd.DataFrame) -> pd.Series:
    """
    Composite score for the daily long-only signal.
    Higher is better. The score is intentionally simple and auditable.
    """
    _ensure_required(features)
    score = pd.Series(0, index=features.index, dtype=float)

    trend = (features["mom_126_21"] > 0) & (features["close"] > features["sma_50"])
    confirm = (features["obv_slope_20"] > 0) & (features["kvo_hist"] > 0)
    cool = (features["rsi_14"] < 70) & (features["mfi_14"] < 80)
    strong_volume = (features["obv_slope_20"] > 0) | (features["kvo_hist"] > 0)
    clean_trend = trend_quality_score(features) >= 3.0

    score += trend.astype(int) * 2
    score += confirm.astype(int)
    score += cool.astype(int)
    score += strong_volume.astype(int) * 0.5
    score += clean_trend.astype(int)

    score = score.where(features.notna().all(axis=1), np.nan)
    return score


def composite_signal(features: pd.DataFrame) -> pd.Series:
    """
    Returns per-row labels in {'strong_up', 'mild_up', 'no_trade'}.
    """
    _ensure_required(features)
    out = pd.Series("no_trade", index=features.index, dtype="object")

    trend = (features["mom_126_21"] > 0) & (features["close"] > features["sma_50"])
    confirm = (features["obv_slope_20"] > 0) & (features["kvo_hist"] > 0)
    one_confirm = (features["obv_slope_20"] > 0) | (features["kvo_hist"] > 0)
    not_overbought = (features["rsi_14"] < 70) & (features["mfi_14"] < 80)
    clean_trend = trend_quality_score(features) >= 3.0

    strong = trend & confirm & not_overbought & clean_trend
    mild = trend & one_confirm & not_overbought & ~strong

    out.loc[mild] = "mild_up"
    out.loc[strong] = "strong_up"
    out = out.where(features.notna().all(axis=1), "no_trade")
    return out


def trend_quality_score(features: pd.DataFrame) -> pd.Series:
    """
    Scores whether a trend is smooth enough to be worth paying attention to.
    This is separate from direction: a stock can be going up but still be too
    choppy or extended for a clean entry.
    """
    _ensure_required(features)
    score = pd.Series(0.0, index=features.index)
    score += (features["close"] > features["sma_50"]).astype(float)
    score += (features["sma_20"] > features["sma_50"]).astype(float)
    score += (features["mom_126_21"] > 0).astype(float)
    score += (features["obv_slope_20"] > 0).astype(float) * 0.5
    score += (features["kvo_hist"] > 0).astype(float) * 0.5
    score += (features["rvol_20"] <= 0.60).astype(float)
    return score.where(features.notna().all(axis=1), np.nan)
