#!/usr/bin/env python3
"""exp-equibles-pilot-returns: pair frozen pilot Q&A scores with 20-day
post-call returns. Pure stdlib + pandas/numpy only (no scipy/sklearn,
per repo rules). Reads the registered design doc; computes only what
the design specifies.
"""
import json
import math
from pathlib import Path

import pandas as pd

REPO = Path("/home/hatch/workspace/stock-thesis")
RAW = REPO / "experiments" / "equibles-pilot-raw"
SCORING = REPO / "experiments" / "exp-equibles-pilot-scoring.json"


def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function (NR betacf)."""
    MAXIT, EPS, FPMIN = 200, 3e-14, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < EPS:
            break
    return h


def _betai(a, b, x):
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1.0 - x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_pvalue_two_sided(t, df):
    """Two-sided p-value for Student's t (pure stdlib)."""
    x = df / (df + t * t)
    return _betai(df / 2.0, 0.5, x)


def pearson(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def ranks(vals):
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def ic_stats(xs, ys):
    """Pearson + Spearman IC with t and two-sided p. Returns dict."""
    n = len(xs)
    res = {"n": n}
    for name, a, b in (("pearson", xs, ys), ("spearman", ranks(xs), ranks(ys))):
        r = pearson(a, b)
        if r is None:
            res[name] = {"r": None, "t": None, "p": None}
            continue
        r = max(min(r, 0.999999999), -0.999999999)
        t = r * math.sqrt((n - 2) / (1 - r * r)) if n > 2 else None
        p = t_pvalue_two_sided(t, n - 2) if t is not None else None
        res[name] = {"r": r, "t": t, "p": p}
    return res


def main():
    scoring = json.loads(SCORING.read_text())
    calls = scoring["per_call"]

    closes = {}
    for sym in ["ABBV", "ABT", "AMD", "AMGN", "AMT", "AMZN", "SPY"]:
        df = pd.read_csv(RAW / f"prices-adjusted-{sym}.csv", index_col=0, parse_dates=True)
        idx = pd.DatetimeIndex(df.index).tz_localize(None).normalize()
        closes[sym] = pd.Series(df["close"].values, index=idx)
    cal = closes["SPY"].index
    last_day = cal.max()
    print(f"trading calendar: {len(cal)} days, last = {last_day.date()}")

    per_call = []
    excluded = []
    for c in calls:
        t = pd.Timestamp(c["call_date"])
        assert t in cal, f"call date {t.date()} not a trading day"
        pos = cal.get_loc(t)
        if pos + 20 >= len(cal):
            excluded.append({
                "ticker": c["ticker"], "call_date": c["call_date"],
                "fiscal_year": c["fiscal_year"], "fiscal_quarter": c["fiscal_quarter"],
                "score": c["score"],
                "reason": f"T+20 falls after last available trading day {last_day.date()}; no partial window used",
            })
            continue
        t20 = cal[pos + 20]
        sym = c["ticker"]
        r = closes[sym].loc[t20] / closes[sym].loc[t] - 1.0
        r_spy = closes["SPY"].loc[t20] / closes["SPY"].loc[t] - 1.0
        per_call.append({
            "ticker": sym, "call_date": c["call_date"],
            "fiscal_year": c["fiscal_year"], "fiscal_quarter": c["fiscal_quarter"],
            "t_plus_20": t20.strftime("%Y-%m-%d"),
            "score": c["score"], "confidence": c["confidence"],
            "evasion": c["evasion"], "analyst_tone": c["analyst_tone"],
            "qoq_score_delta": c["qoq_score_delta"],
            "close_t": float(closes[sym].loc[t]),
            "close_t20": float(closes[sym].loc[t20]),
            "return_20d": float(r),
            "spy_return_20d": float(r_spy),
            "excess_return_20d": float(r - r_spy),
        })

    print(f"included: {len(per_call)}, excluded: {len(excluded)}")

    signals = {
        "score": [c["score"] for c in per_call],
        "qoq_score_delta": None,  # subset: non-null only
        "evasion": [c["evasion"] for c in per_call],
        "analyst_tone": [c["analyst_tone"] for c in per_call],
    }
    delta_rows = [c for c in per_call if c["qoq_score_delta"] is not None]
    signals["qoq_score_delta"] = [c["qoq_score_delta"] for c in delta_rows]

    ic_table = {}
    for sig, xs in signals.items():
        rows = delta_rows if sig == "qoq_score_delta" else per_call
        for ret_name, key in (("raw", "return_20d"), ("excess", "excess_return_20d")):
            ys = [c[key] for c in rows]
            ic_table[f"{sig}__{ret_name}"] = ic_stats(xs, ys)

    # Median split on score (raw + excess), equal weight.
    scores = sorted(c["score"] for c in per_call)
    n = len(per_call)
    if n % 2 == 0:
        med = (scores[n // 2 - 1] + scores[n // 2]) / 2.0
        hi = [c for c in per_call if c["score"] > med]
        lo = [c for c in per_call if c["score"] < med]
        dropped = [c for c in per_call if c["score"] == med]
    else:
        med = scores[n // 2]
        hi = [c for c in per_call if c["score"] > med]
        lo = [c for c in per_call if c["score"] < med]
        dropped = [c for c in per_call if c["score"] == med]

    def _mean(rows, key):
        return sum(c[key] for c in rows) / len(rows) if rows else None

    median_split = {
        "signal": "score",
        "median": med,
        "n_above": len(hi), "n_below": len(lo),
        "n_dropped_at_median": len(dropped),
        "above_mean_raw": _mean(hi, "return_20d"),
        "below_mean_raw": _mean(lo, "return_20d"),
        "spread_raw": _mean(hi, "return_20d") - _mean(lo, "return_20d"),
        "above_mean_excess": _mean(hi, "excess_return_20d"),
        "below_mean_excess": _mean(lo, "excess_return_20d"),
        "spread_excess": _mean(hi, "excess_return_20d") - _mean(lo, "excess_return_20d"),
    }

    # Evidence read against the pre-registered bar.
    score_raw = ic_table["score__raw"]["pearson"]
    score_excess = ic_table["score__excess"]["pearson"]
    score_raw_sp = ic_table["score__raw"]["spearman"]
    score_excess_sp = ic_table["score__excess"]["spearman"]
    suggestive = any(
        abs(x["r"]) >= 0.3 and x["p"] is not None and x["p"] < 0.10
        for x in (score_raw, score_excess, score_raw_sp, score_excess_sp)
    )
    verdict = "suggestive" if suggestive else "no evidence"

    out = {
        "experiment_id": "exp-equibles-pilot-returns",
        "design_doc": "experiments/exp-equibles-pilot-returns-design.md",
        "date": "2026-11-22",
        "price_source": "Alpaca daily bars, adjustment=all (split+dividend), fetched 2026-11-22; files experiments/equibles-pilot-raw/prices-adjusted-*.csv",
        "last_trading_day": last_day.strftime("%Y-%m-%d"),
        "return_definition": "R = close(T+20)/close(T) - 1, T = call date (even if call was after close); excess = R - R_SPY same window; T+20 = 20th trading day after T",
        "n_scored": len(calls),
        "n_included": len(per_call),
        "n_excluded": len(excluded),
        "excluded": excluded,
        "per_call": per_call,
        "ic_table": ic_table,
        "median_split": median_split,
        "evidence_bar": "|IC| >= 0.3 with p < 0.10 on the score (Pearson or Spearman, raw or excess)",
        "verdict": verdict,
        "spend_usd": 0.0,
        "notes": [
            "QoQ-delta IC uses only the 10 included calls with a non-null qoq_score_delta.",
            "16 ICs computed on one small sample; no formal multiple-testing correction — read p < 0.10 against that multiplicity.",
            "Scores are single-model single-pass Haiku; knowledge-cutoff contamination possible.",
        ],
    }
    (REPO / "experiments" / "exp-equibles-pilot-returns.json").write_text(
        json.dumps(out, indent=2))
    print(json.dumps({"verdict": verdict, "n_included": len(per_call),
                      "n_excluded": len(excluded)}, indent=2))
    for k, v in ic_table.items():
        p_, s_ = v["pearson"], v["spearman"]
        print(f"{k:28s} pearson r={p_['r']:+.3f} t={p_['t']:+.2f} p={p_['p']:.3f} (n={v['n']}) | "
              f"spearman r={s_['r']:+.3f} t={s_['t']:+.2f} p={s_['p']:.3f}")
    print("median split:", json.dumps(median_split, indent=2))
    return out


if __name__ == "__main__":
    main()
