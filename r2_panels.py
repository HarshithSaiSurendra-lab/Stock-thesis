#!/usr/bin/env python3
"""Round-2 shared panel builders (TRAIN + HOLDOUT windows, $0, native only).

BACKTEST / RESEARCH ONLY. No paper trading, no live money, ALLOW_LIVE stays 0.
No API calls anywhere in this module ($0 spend). Every builder is
point-in-time: a panel value on day t uses only data dated <= t
(filing dates, past bars, scores of filings already filed). Nothing
from the 7 shared research repos is cloned, installed, imported, or
run — all ideas are implemented natively in stock-thesis code only.

Builders (all take an explicit day index so the same code builds train
panels for calibration and full-window panels for confirmation):
  - v2_selection / v3_selection: the round-1 passing designs.
  - banded_selection: buy/hold hysteresis on a base selection
    (De Groot et al. 2012; Blitz et al. 2023). Enter on the base
    selection; exit only when the Haiku score falls below a
    cross-sectional buffer percentile of positive scores. Held set
    updates only on the rebalance grid (index % grid == 0).
  - cohort_returns: overlapping 1/H daily cohorts (Chen & Wang 2026).
    Each cohort rebalances every H days from a different offset; the
    blended portfolio is the equal-capital mean of cohort daily
    returns. Caps daily one-way turnover structurally at 100%/H.
  - lazy_prices: YoY Jaccard/cosine on Item 1A Risk Factors text
    (Cohen, Malloy & Nguyen 2020, "Lazy Prices"), same form type
    (10-K vs prior 10-K, 10-Q vs prior 10-Q). Missing Risk Factors
    text in either filing is treated as no change (similarity 1.0):
    no evidence of an edit, no veto. Changer = bottom-quintile
    similarity cross-sectionally each day; the veto excludes changers
    from v2/v3 longs (the long leg is the only usable leg for an
    unleveraged book — the literature's spread is short-leg driven).
  - mixture_panel: decoupled prediction mixture (Guo & Hauptmann
    2025). Rank-average of Haiku rank + technical rank, with separate
    thresholds: mixture >= 0.9 AND each rank >= 0.5. No additive tilt.
  - Ridge (closed-form, numpy) + GBDT (native binned gradient
    boosting, numpy) for the cross-family ensemble (H5). sklearn is
    NOT used: a pip install of scikit-learn in this venv was flagged
    by an execution-risk check, so it is never imported by repo code.
  - ensemble_panels: walk-forward Ridge + GBDT + Haiku rank average,
    with and without a top-quintile cross-family disagreement veto
    (Bali, Kelly, Moerke & Rahman 2026).
  - h6_features: high-volume shock (Gervais, Kaniel & Mingelgrin
    2001: volume in the top decile of its own 49-day distribution)
    plus intraday/overnight 20-day return decomposition (Lou, Polk &
    Skouras 2019).
  - c6_rank_panel / c1c_panel: the round-1 runner-ups, rebuilt exactly
    as in verify_sprint.py / calibrate_sprint.py.
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from backtest import BacktestConfig, run_backtest
from config import TradingConfig
from research_runner import build_signal_variants, slice_research_window, _feature_panels
from run_real_backtest import _load_cached_bars
import run_llm_backtest as p2
import run_freshness_backtest as rf

REPO = Path(__file__).parent
RESEARCH_START = "2024-01-01"
RESEARCH_END = "2026-11-16"
HOLDOUT_DAYS_N = 63
TRAIN_END_DATE = "2026-08-17"

WORD_RE = re.compile(r"[a-z]{3,}")


# ----------------------------------------------------------------------
# Shared data loading (full window; callers slice train / holdout)
# ----------------------------------------------------------------------
def load_r2_data():
    """Load bars, variants, features, filings, Haiku scores, score panel.

    Returns a dict with prices, variants, features, filings, score
    (Haiku score carried forward), composite (v1_composite), days
    (full window index), train_days, holdout_days, full_prices.
    The current-filing series is the union construction from v2/v3
    (train in-scope + holdout in-scope filings); it equals the stale
    series on train days (verified by the v2/v3 drivers).
    """
    data = _load_cached_bars()
    if data is None:
        raise RuntimeError("no cached bars; .cache/backtest_data is empty")
    cfg = TradingConfig.from_env()
    prices, variants = build_signal_variants(data, cfg)
    prices, variants = slice_research_window(prices, variants, RESEARCH_START, RESEARCH_END)
    window_days = prices.index
    holdout_start = window_days[-HOLDOUT_DAYS_N]
    train_days = window_days[window_days < holdout_start]
    holdout_days = window_days[window_days >= holdout_start]

    filings = p2.load_edgar_filings()
    haiku_train = p2._load_scores(
        REPO / "experiments" / "results" / "exp-technical-plus-llm-v1-scores-haiku.json")
    haiku_fresh = p2._load_scores(
        REPO / "experiments" / "results" / "exp-technical-plus-llm-freshness-v1-scores-haiku.json")
    merged_haiku = {**haiku_train, **haiku_fresh}

    in_scope_train = p2.in_scope_filings(filings, variants, train_days)
    in_scope_hw = rf.in_scope_holdout_filings(filings, variants, holdout_days)
    filings_union = pd.concat([in_scope_train, in_scope_hw], ignore_index=True)
    current_union = rf.current_filing_series_union(filings_union, window_days)
    current_stale = p2.current_filing_series(in_scope_train, window_days)
    if not (current_union.loc[train_days].fillna("")
            == current_stale.loc[train_days].fillna("")).all().all():
        raise RuntimeError("union current series differs from stale on TRAIN days; aborting")

    score = pd.DataFrame(0.0, index=window_days,
                         columns=sorted(current_union.columns))
    for sym in current_union.columns:
        for day in window_days:
            acc = current_union.at[day, sym]
            if isinstance(acc, str):
                h = merged_haiku.get(acc)
                if h is not None and "error" not in h:
                    score.at[day, sym] = float(h["score"])

    _, features = _feature_panels(data)
    full_prices = pd.DataFrame(
        {s: f["close"] for s, f in data.bars.items()}).sort_index().loc[window_days]
    return {
        "data": data,
        "prices": prices,
        "variants": variants,
        "features": features,
        "filings": filings,
        "score": score,
        "composite": variants["v1_composite"].loc[window_days],
        "days": window_days,
        "train_days": train_days,
        "holdout_days": holdout_days,
        "full_prices": full_prices,
        "merged_haiku": merged_haiku,
    }


def zscore_xsec(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional z-score per day (mean 0, sd 1 across symbols)."""
    m = panel.mean(axis=1)
    s = panel.std(axis=1).replace(0.0, np.nan)
    return panel.sub(m, axis=0).div(s, axis=0)


def pct_rank_xsec(panel: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional percentile rank per day (0..1; NaN stays NaN)."""
    return panel.rank(axis=1, pct=True)


# ----------------------------------------------------------------------
# Round-1 selections (reproduction anchors)
# ----------------------------------------------------------------------
def v2_selection(score: pd.DataFrame) -> pd.DataFrame:
    """1.0 for every name with score >= the day's 5th-highest positive score."""
    selection = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    for day in score.index:
        row = score.loc[day]
        pos = row[row > 0].sort_values(ascending=False)
        if len(pos) == 0:
            continue
        cutoff = pos.iloc[min(5, len(pos)) - 1]
        selection.loc[day, row >= cutoff] = 1.0
    return selection


def v3_selection(score: pd.DataFrame, composite: pd.DataFrame) -> pd.DataFrame:
    """v2 selection AND v1_composite > 0 (deterministic technical veto)."""
    sel = v2_selection(score)
    return sel.where(composite > 0, 0.0)


# ----------------------------------------------------------------------
# H2a: buy/hold banding (turnover hysteresis)
# ----------------------------------------------------------------------
def banded_selection(base_selection: pd.DataFrame, score: pd.DataFrame,
                     buffer_pct: float, grid: int = 5) -> pd.DataFrame:
    """Buy/hold banding on a base 0/1 selection panel.

    Enter when the base selection picks a name; retain it until its
    Haiku score falls below the ``buffer_pct`` percentile of that day's
    positive scores (buffer 50 = median, 30 = 30th percentile). The
    held set updates only on the rebalance grid (row position % grid
    == 0) so it matches the engine's rebalance grid for the window
    passed in. Returns a 0/1 panel over the same index/columns.
    """
    out = pd.DataFrame(0.0, index=score.index, columns=score.columns)
    held: set[str] = set()
    for i, day in enumerate(score.index):
        if i % grid != 0:
            if held:
                out.loc[day, list(held)] = 1.0
            continue
        row = score.loc[day]
        pos = row[row > 0]
        threshold = float(pos.quantile(buffer_pct / 100.0)) if len(pos) else None
        # exits first: held names below the buffer threshold leave
        if threshold is not None:
            held = {s for s in held if row.get(s, 0.0) >= threshold}
        else:
            held = set()
        # entries: base-selected names join
        new_names = base_selection.columns[base_selection.loc[day] > 0]
        held |= set(new_names)
        if held:
            out.loc[day, list(held)] = 1.0
    return out


# ----------------------------------------------------------------------
# H2b: overlapping 1/H cohorts
# ----------------------------------------------------------------------
def cohort_daily_returns(prices: pd.DataFrame, selection: pd.DataFrame, horizon: int,
                         cost: int, min_hold_days: int = 10,
                         max_positions: int = 60, trades_from=None):
    """One cost level of overlapping 1/H cohorts: returns the blended
    daily net-return series (mean of cohort daily returns), the mean
    cohort turnover, and the total trade count. Cohort ``o`` rebalances
    every ``horizon`` days starting at row ``o``: its panel is the
    selection sampled on its grid days and forward-filled, run with
    rebalance_every=1 (the panel only changes on cohort days).
    ``trades_from`` (a Timestamp) restricts the trade count to entries
    on or after that day (used for holdout trade counts)."""
    n = len(prices)
    cohort_rets = []
    turnovers = []
    trades = 0
    for o in range(horizon):
        grid_idx = [i for i in range(n) if i % horizon == o]
        keep = pd.Series(False, index=selection.index)
        keep.iloc[grid_idx] = True
        cohort_panel = selection.copy().where(keep, np.nan).ffill().fillna(0.0)
        res = run_backtest(
            prices, cohort_panel,
            BacktestConfig(cost_bps=float(cost), long_only=True,
                           max_positions=max_positions, rebalance_every=1,
                           min_hold_days=min_hold_days))
        cohort_rets.append(res.daily_returns)
        turnovers.append(float(res.stats.get("avg_daily_turnover") or 0.0))
        w = res.weights.fillna(0.0)
        entries = (w > 0) & (w.shift(1).fillna(0.0) <= 0)
        if trades_from is not None:
            entries = entries.loc[entries.index >= trades_from]
        trades += int(entries.sum().sum())
    blended = pd.concat(cohort_rets, axis=1).mean(axis=1)
    return blended, float(np.mean(turnovers)) if turnovers else None, int(trades)


def _series_stats(returns: pd.Series):
    """Total return, Sharpe, WF folds for a daily-return series."""
    total = float((1 + returns).prod() - 1.0) if len(returns) else 0.0
    sharpe = None
    if len(returns) > 30 and returns.std() > 0:
        sharpe = float(returns.mean() / returns.std() * np.sqrt(252))
    fold_sharpes = []
    fold_n = len(returns) // 5
    for k in range(1, 5):
        seg = returns.iloc[fold_n * k: fold_n * (k + 1)]
        if len(seg) >= 30 and seg.std() > 0:
            fold_sharpes.append(float(seg.mean() / seg.std() * np.sqrt(252)))
    return {
        "total_return": total,
        "sharpe": sharpe,
        "wf_median_sharpe": float(np.median(fold_sharpes)) if fold_sharpes else None,
        "wf_positive_folds": int(sum(s > 0 for s in fold_sharpes)),
        "wf_folds": len(fold_sharpes),
    }


def cohort_returns(prices: pd.DataFrame, selection: pd.DataFrame, horizon: int,
                   costs: tuple = (0, 10, 20), min_hold_days: int = 10,
                   max_positions: int = 60):
    """Equal-capital overlapping cohorts on a 0/1 selection panel.

    The blended portfolio is the mean of the cohort daily net-return
    series. Returns a dict keyed by cost with total_return, sharpe,
    wf_median_sharpe, wf_positive_folds, avg_daily_turnover (mean
    across cohorts), trades (sum)."""
    out = {}
    for cost in costs:
        blended, turnover, trades = cohort_daily_returns(
            prices, selection, horizon, cost, min_hold_days, max_positions)
        stats = _series_stats(blended)
        stats["avg_daily_turnover"] = turnover
        stats["trades"] = trades
        out[f"cost_{cost}bps"] = stats
    return out


# ----------------------------------------------------------------------
# H3: Lazy Prices — YoY Item 1A Risk Factors similarity veto
# ----------------------------------------------------------------------
def _token_set(text) -> set:
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return set()
    return set(WORD_RE.findall(str(text).lower()))


def lazy_prices_similarity(filings: pd.DataFrame) -> pd.DataFrame:
    """YoY Jaccard + cosine similarity of Item 1A Risk Factors text.

    For each filing, compare its ``risk_text`` token set with the most
    recent prior filing of the SAME form type (10-K vs 10-K, 10-Q vs
    10-Q) for the same ticker. Missing/empty risk text in either
    filing is treated as no change (similarity 1.0): no evidence of an
    edit, no veto. Returns one row per filing with a prior same-form
    filing (plus rows with similarity 1.0 where comparison is
    impossible), columns: ticker, filing_date, form, jaccard, cosine.
    """
    rows = []
    f = filings.sort_values(["ticker", "form", "filing_date"])
    for (ticker, form), group in f.groupby(["ticker", "form"]):
        prev_toks = None
        for _, row in group.iterrows():
            toks = _token_set(row.get("risk_text"))
            if prev_toks is None or not toks or not prev_toks:
                sim_j = 1.0
                sim_c = 1.0
            else:
                inter = len(toks & prev_toks)
                union = len(toks | prev_toks)
                sim_j = inter / union if union else 1.0
                sim_c = inter / np.sqrt(len(toks) * len(prev_toks)) if toks and prev_toks else 1.0
            rows.append({
                "ticker": ticker,
                "filing_date": row["filing_date"],
                "form": form,
                "jaccard": float(sim_j),
                "cosine": float(sim_c),
            })
            prev_toks = toks
    return pd.DataFrame(rows)


def lazy_changer_panel(sim: pd.DataFrame, days: pd.DatetimeIndex,
                       columns: list) -> tuple:
    """Carried-forward similarity panel + daily bottom-quintile changer flag.

    For each symbol/day, the similarity of its latest filing (filing
    date <= day). Changer = similarity below the cross-sectional 20th
    percentile among symbols with a computed similarity (< 1.0 means
    a real comparison exists; 1.0 = no-change default, never a
    changer). Returns (similarity_panel, changer_panel) of 0/1 flags.
    """
    sim_panel = pd.DataFrame(np.nan, index=days, columns=columns)
    for sym, group in sim.groupby("ticker"):
        if sym not in columns:
            continue
        dates = group["filing_date"].to_numpy()
        vals = group["jaccard"].to_numpy()
        pos = np.searchsorted(dates, days.to_numpy(), side="right") - 1
        sim_panel[sym] = np.where(pos >= 0, vals[np.maximum(pos, 0)], np.nan)
    changer = pd.DataFrame(0.0, index=days, columns=columns)
    for day in days:
        row = sim_panel.loc[day]
        real = row[(row < 1.0)]
        if len(real) < 5:
            continue
        threshold = float(real.quantile(0.20))
        changer.loc[day, real[real < threshold].index] = 1.0
    return sim_panel, changer


def lazy_veto_selection(base_selection: pd.DataFrame,
                       changer: pd.DataFrame) -> pd.DataFrame:
    """Exclude bottom-quintile Item 1A changers from a base selection."""
    return base_selection.where(changer.reindex_like(base_selection) == 0, 0.0)


# ----------------------------------------------------------------------
# H4: decoupled prediction mixture (rank-average, separate thresholds)
# ----------------------------------------------------------------------
def mixture_panel(score: pd.DataFrame, composite: pd.DataFrame) -> pd.DataFrame:
    """Rank-average of Haiku rank + technical rank, separate thresholds.

    haiku_rank = percentile rank of the Haiku score among names with
    score > 0 (0 otherwise); tech_rank = percentile rank of
    v1_composite among names with composite > 0 (0 otherwise).
    Mixture = mean of the two ranks. Hold names with mixture >= 0.9
    AND each rank >= 0.5. Panel value = the mixture score for held
    names, 0 elsewhere (engine rank-selects the top names by value).
    """
    haiku_rank = pct_rank_xsec(score.where(score > 0)).fillna(0.0)
    tech_rank = pct_rank_xsec(composite.where(composite > 0)).fillna(0.0)
    mix = (haiku_rank + tech_rank) / 2.0
    sel = (mix >= 0.9) & (haiku_rank >= 0.5) & (tech_rank >= 0.5)
    return mix.where(sel, 0.0)


# ----------------------------------------------------------------------
# H5: native Ridge + native GBDT (numpy only — sklearn is NOT imported)
# ----------------------------------------------------------------------
def ridge_fit_predict(x_train: np.ndarray, y_train: np.ndarray,
                      x_pred: np.ndarray, lam: float = 10.0) -> np.ndarray:
    """Closed-form Ridge on standardized features (intercept unpenalized)."""
    mu = x_train.mean(axis=0)
    sd = x_train.std(axis=0)
    sd = np.where(sd == 0, 1.0, sd)
    xt = (x_train - mu) / sd
    xp = (x_pred - mu) / sd
    ym = y_train.mean()
    yc = y_train - ym
    d = xt.shape[1]
    beta = np.linalg.solve(xt.T @ xt + lam * np.eye(d), xt.T @ yc)
    return ym + xp @ beta


class NativeGBDT:
    """Small binned gradient-boosted tree regressor (squared loss, numpy).

    Histogram binning (32 quantile bins per feature, fit on training
    data), depth-3 trees, learning rate 0.05, min_leaf 200, row
    subsample 0.8 per tree. Written natively for this project — no
    third-party ML library is used.
    """

    def __init__(self, n_estimators: int = 100, max_depth: int = 3,
                 learning_rate: float = 0.05, min_leaf: int = 200,
                 n_bins: int = 32, subsample: float = 0.8, seed: int = 7):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.min_leaf = min_leaf
        self.n_bins = n_bins
        self.subsample = subsample
        self.seed = seed
        self.bin_edges_ = None
        self.trees_ = []
        self.base_ = 0.0

    def _bin(self, x: np.ndarray) -> np.ndarray:
        binned = np.zeros(x.shape, dtype=np.int32)
        for j in range(x.shape[1]):
            edges = self.bin_edges_[j]
            binned[:, j] = np.clip(
                np.searchsorted(edges, x[:, j], side="right") - 1, 0, self.n_bins - 1)
        return binned

    def _best_split(self, xb: np.ndarray, grad: np.ndarray, idx: np.ndarray):
        """Best (feature, bin) split by squared-loss gain. Returns None if
        no split clears min_leaf on both sides."""
        n = len(idx)
        total_g = float(grad[idx].sum())
        total_h = float(n)
        best = None
        for j in range(xb.shape[1]):
            bins = xb[idx, j]
            g = grad[idx]
            # per-bin gradient sums (bins are 0..n_bins-1)
            bin_g = np.bincount(bins, weights=g, minlength=self.n_bins)
            bin_n = np.bincount(bins, minlength=self.n_bins).astype(float)
            cum_g = np.cumsum(bin_g)
            cum_n = np.cumsum(bin_n)
            for b in range(self.n_bins - 1):
                n_l, n_r = cum_n[b], total_h - cum_n[b]
                if n_l < self.min_leaf or n_r < self.min_leaf:
                    continue
                g_l, g_r = cum_g[b], total_g - cum_g[b]
                gain = (g_l * g_l / (n_l + 1.0) + g_r * g_r / (n_r + 1.0)
                        - total_g * total_g / (total_h + 1.0))
                if best is None or gain > best[0]:
                    best = (gain, j, b)
        return best

    def _grow(self, xb: np.ndarray, grad: np.ndarray, idx: np.ndarray, depth: int):
        """Recursively grow one tree; returns (feature, bin, left, right)
        nodes or a leaf value (float)."""
        n = len(idx)
        leaf_val = -float(grad[idx].sum()) / (n + 1.0)
        if depth >= self.max_depth or n < 2 * self.min_leaf:
            return leaf_val
        split = self._best_split(xb, grad, idx)
        if split is None:
            return leaf_val
        _, j, b = split
        left_idx = idx[xb[idx, j] <= b]
        right_idx = idx[xb[idx, j] > b]
        if len(left_idx) < self.min_leaf or len(right_idx) < self.min_leaf:
            return leaf_val
        return (j, b,
                self._grow(xb, grad, left_idx, depth + 1),
                self._grow(xb, grad, right_idx, depth + 1))

    def _apply_batch(self, node, xb: np.ndarray) -> np.ndarray:
        """Vectorized tree application: leaf value for every row at once."""
        out = np.zeros(len(xb))
        stack = [(node, np.ones(len(xb), dtype=bool))]
        while stack:
            node, mask = stack.pop()
            if isinstance(node, tuple):
                j, b, left, right = node
                stack.append((left, mask & (xb[:, j] <= b)))
                stack.append((right, mask & (xb[:, j] > b)))
            else:
                out[mask] = node
        return out

    def fit(self, x: np.ndarray, y: np.ndarray):
        rng = np.random.default_rng(self.seed)
        self.base_ = float(np.mean(y))
        # quantile bin edges per feature (on training data only)
        self.bin_edges_ = []
        for j in range(x.shape[1]):
            qs = np.quantile(x[:, j], np.linspace(0, 1, self.n_bins + 1)[1:-1])
            self.bin_edges_.append(np.unique(qs))
        xb = self._bin(x)
        pred = np.full(len(y), self.base_)
        self.trees_ = []
        for _ in range(self.n_estimators):
            grad = pred - y  # gradient of 0.5*(pred-y)^2
            if self.subsample < 1.0:
                idx = rng.choice(len(y), size=int(len(y) * self.subsample),
                                 replace=False)
            else:
                idx = np.arange(len(y))
            tree = self._grow(xb, grad, idx, 0)
            self.trees_.append(tree)
            # update predictions on ALL rows (standard boosting)
            pred += self.learning_rate * self._apply_batch(tree, xb)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        xb = self._bin(x)
        out = np.full(len(x), self.base_)
        for tree in self.trees_:
            out += self.learning_rate * self._apply_batch(tree, xb)
        return out


ENSEMBLE_FEATURES = ("rvol_20", "ret_63", "mom_126_21", "drawdown_63",
                     "rsi_14", "mfi_14", "kvo_hist", "obv_slope_20",
                     "wad_slope_20", "ret_1d")


def build_ensemble_xy(features: dict, prices: pd.DataFrame,
                      days: pd.DatetimeIndex, horizon: int = 20,
                      require_target: bool = True):
    """Stacked (day, symbol) feature/target table for the ensemble.

    Target = horizon-day forward return. Rows whose forward window
    runs past the last day in ``days`` are EXCLUDED when
    ``require_target`` is True (no holdout-price labels leak into
    training). With ``require_target=False`` (prediction only) those
    rows are kept with a NaN target. Returns (X, y, index) where
    index is a MultiIndex (day, symbol).
    """
    feats = {s: f.loc[days] for s, f in features.items() if s in prices.columns}
    symbols = sorted(feats)
    rows_x, rows_y, rows_idx = [], [], []
    close = prices.loc[days]
    for i, day in enumerate(days):
        if i + horizon >= len(days) and require_target:
            break
        if i + horizon < len(days):
            fwd = close.iloc[i + horizon] / close.iloc[i] - 1.0
        else:
            fwd = None
        for sym in symbols:
            f = feats[sym]
            if day not in f.index:
                continue
            vals = [f.at[day, name] for name in ENSEMBLE_FEATURES]
            if any(v is None or (isinstance(v, float) and np.isnan(v)) for v in vals):
                continue
            if fwd is None:
                y = np.nan
            else:
                y = fwd.get(sym, np.nan)
            if require_target and (y is None or (isinstance(y, float) and np.isnan(y))):
                continue
            rows_x.append(vals)
            rows_y.append(float(y) if y is not None and not (isinstance(y, float) and np.isnan(y)) else np.nan)
            rows_idx.append((day, sym))
    X = np.array(rows_x, dtype=float)
    y = np.array(rows_y, dtype=float)
    idx = pd.MultiIndex.from_tuples(rows_idx, names=["day", "symbol"])
    return X, y, idx


def ensemble_panels(features: dict, prices: pd.DataFrame, score: pd.DataFrame,
                    days: pd.DatetimeIndex, horizon: int = 20,
                    n_splits: int = 4):
    """Walk-forward Ridge + GBDT + Haiku rank ensemble (train window only).

    Expanding-window folds over ``days``: fit Ridge + GBDT on each
    fold's training rows, predict the fold's test rows. Per day,
    percentile-rank the Ridge predictions, GBDT predictions, and Haiku
    scores (unscored names rank 0); ensemble rank = mean of the three;
    disagreement = std of the three ranks. Returns (panel_veto,
    panel_no_veto, diagnostics): panel values are ensemble ranks for
    selected names (ensemble >= 0.9; veto panel additionally requires
    disagreement below the day's 80th percentile), 0 elsewhere.
    """
    X, y, idx = build_ensemble_xy(features, prices, days, horizon=horizon)
    n = len(days)
    fold = n // (n_splits + 1)
    ridge_pred = pd.Series(np.nan, index=idx)
    gbdt_pred = pd.Series(np.nan, index=idx)
    for k in range(1, n_splits + 1):
        test_days = days[fold * k: fold * (k + 1)]
        if len(test_days) == 0:
            continue
        train_mask = np.asarray(idx.get_level_values("day") < test_days[0])
        test_mask = np.asarray(idx.get_level_values("day").isin(test_days))
        if train_mask.sum() < 1000 or test_mask.sum() == 0:
            continue
        ridge_pred.loc[test_mask] = ridge_fit_predict(
            X[train_mask], y[train_mask], X[test_mask])
        gbdt = NativeGBDT(seed=7 + k).fit(X[train_mask], y[train_mask])
        gbdt_pred.loc[test_mask] = gbdt.predict(X[test_mask])
    # Assemble per-day rank panels over the full day index.
    ridge_panel = pd.DataFrame(np.nan, index=days, columns=sorted(set(idx.get_level_values("symbol"))))
    gbdt_panel = pd.DataFrame(np.nan, index=days, columns=ridge_panel.columns)
    for (day, sym), v in ridge_pred.items():
        ridge_panel.at[day, sym] = v
    for (day, sym), v in gbdt_pred.items():
        gbdt_panel.at[day, sym] = v
    haiku = score.loc[days]
    r_ridge = pct_rank_xsec(ridge_panel).fillna(0.0)
    r_gbdt = pct_rank_xsec(gbdt_panel).fillna(0.0)
    r_haiku = pct_rank_xsec(haiku.where(haiku > 0)).fillna(0.0)
    ens = (r_ridge + r_gbdt + r_haiku) / 3.0
    # disagreement: std across the three rank columns per (day, symbol)
    stacked = np.stack([r_ridge.values, r_gbdt.values, r_haiku.values], axis=2)
    dis = pd.DataFrame(np.nanstd(stacked, axis=2), index=days, columns=r_ridge.columns)
    sel = ens >= 0.9
    dis_threshold = dis.quantile(0.80, axis=1)
    veto_ok = dis.le(dis_threshold, axis=0)
    panel_no_veto = ens.where(sel, 0.0)
    panel_veto = ens.where(sel & veto_ok, 0.0)
    diag = {
        "n_rows": int(len(X)),
        "n_predicted": int(ridge_pred.notna().sum()),
        "features": list(ENSEMBLE_FEATURES),
        "horizon_days": horizon,
        "folds": n_splits,
    }
    return panel_veto, panel_no_veto, diag


# ----------------------------------------------------------------------
# H6: high-volume shock + intraday/overnight decomposition
# ----------------------------------------------------------------------
def h6_features(data, days: pd.DatetimeIndex):
    """Volume-shock and intraday/overnight panels (point-in-time).

    vol_shock = today's volume / 49-day rolling median volume, plus
    the rolling 49-day percentile rank of volume.
    intraday_20d = sum of daily (close/open - 1) over the past 20 days.
    overnight_20d = sum of daily (open/prev close - 1) over the past
    20 days. All use only bars dated <= the panel day.
    """
    symbols = sorted(data.bars)
    vol_ratio = pd.DataFrame(np.nan, index=days, columns=symbols)
    vol_pct = pd.DataFrame(np.nan, index=days, columns=symbols)
    intra_20 = pd.DataFrame(np.nan, index=days, columns=symbols)
    over_20 = pd.DataFrame(np.nan, index=days, columns=symbols)
    for sym in symbols:
        frame = data.bars[sym].reindex(days)
        vol = frame["volume"]
        med49 = vol.rolling(49, min_periods=49).median()
        vol_ratio[sym] = vol / med49
        vol_pct[sym] = vol.rolling(49, min_periods=49).rank(pct=True)
        intra = frame["close"] / frame["open"] - 1.0
        over = frame["open"] / frame["close"].shift(1) - 1.0
        intra_20[sym] = intra.rolling(20, min_periods=20).sum()
        over_20[sym] = over.rolling(20, min_periods=20).sum()
    return {"vol_ratio": vol_ratio, "vol_pct": vol_pct,
            "intraday_20d": intra_20, "overnight_20d": over_20}


# ----------------------------------------------------------------------
# H7: round-1 runner-ups (rebuilt exactly as in round 1)
# ----------------------------------------------------------------------
def tone(text: str) -> float:
    toks = WORD_RE.findall(text.lower())
    if not toks:
        return 0.0
    pos_words = {"growth", "strong", "record", "expand", "improve", "increase",
                 "robust", "resilient", "opportunity", "momentum", "outperform"}
    neg_words = {"decline", "risk", "uncertain", "challenge", "weak", "decrease",
                 "pressure", "headwind", "adverse", "loss", "shortfall", "volatile"}
    pos = sum(1 for t in toks if t in pos_words)
    neg = sum(1 for t in toks if t in neg_words)
    return (pos - neg) / max(len(toks), 1)


def c6_rank_panel(filings: pd.DataFrame, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Textual-delta rank panel (verify_sprint.py V3 construction).

    Per filing, YoY deltas vs the previous filing of the same ticker:
    Jaccard similarity, length ratio, tone delta. Carried forward to
    each day, cross-sectional percentile ranks per day, combined as
    rank(tone_delta) + rank(jaccard) - rank(len_ratio). No delta info
    -> 0 (not selected). Point-in-time: only filings dated <= the last
    day of ``days`` contribute, and ranks are computed cross-sectionally
    each day.
    """
    tf = filings[filings["filing_date"] <= pd.Timestamp(days[-1])].copy()
    tf = tf.sort_values(["ticker", "filing_date"])
    delta_rows = []
    for sym, group in tf.groupby("ticker"):
        prev_toks = prev_tone = prev_len = None
        for _, row in group.iterrows():
            toks = set(WORD_RE.findall(row["excerpt_text"].lower()))
            tn = tone(row["excerpt_text"])
            if prev_toks is not None:
                union = toks | prev_toks
                delta_rows.append({
                    "ticker": sym,
                    "filing_date": row["filing_date"],
                    "jaccard": len(toks & prev_toks) / len(union) if union else np.nan,
                    "len_ratio": len(row["excerpt_text"]) / max(prev_len, 1),
                    "tone_delta": tn - prev_tone,
                })
            prev_toks, prev_tone, prev_len = toks, tn, len(row["excerpt_text"])
    delta = pd.DataFrame(delta_rows)
    raw = {}
    for col in ("jaccard", "len_ratio", "tone_delta"):
        panel = pd.DataFrame(np.nan, index=days,
                             columns=sorted(delta["ticker"].unique()))
        for sym, group in delta.groupby("ticker"):
            dates = group["filing_date"].to_numpy()
            vals = group[col].to_numpy()
            pos = np.searchsorted(dates, days.to_numpy(), side="right") - 1
            panel[sym] = np.where(pos >= 0, vals[np.maximum(pos, 0)], np.nan)
        raw[col] = panel
    rank_panels = {c: raw[c].rank(axis=1, pct=True) for c in raw}
    c6 = rank_panels["tone_delta"] + rank_panels["jaccard"] - rank_panels["len_ratio"]
    return c6.fillna(0.0)


def c1c_panel(features: dict, days: pd.DatetimeIndex, symbols: list) -> pd.DataFrame:
    """Reversion + oversold rank score (calibrate_sprint.py C1c).

    z(rvol_20) - z(ret_63) - z(drawdown_63) - z(mom_126_21) - z(rsi_14),
    cross-sectional z-scores per day. Engine rank-selects top-5.
    """
    feats = {s: f.loc[days] for s, f in features.items() if s in symbols}
    panels = {}
    for name in ("rvol_20", "ret_63", "mom_126_21", "drawdown_63", "rsi_14"):
        panels[name] = pd.DataFrame({s: feats[s][name] for s in sorted(feats)})
    z = {name: zscore_xsec(panels[name]) for name in panels}
    return (z["rvol_20"] - z["ret_63"] - z["drawdown_63"]
            - z["mom_126_21"] - z["rsi_14"]).fillna(0.0)
