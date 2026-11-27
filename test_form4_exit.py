"""Tests for exp-form4-insider-exit-v1 (form4_exit.py).

Frozen fixtures (from the design doc):
(a) insider sells Monday, files Wednesday after the close -> the first
    eligible simulated action is Thursday's open (exchange calendar).
(b) an amended Form 4 (same sale filed twice) counts as ONE observation.
(c) multiple rows for one sale (split across price bands) count as ONE.
(d) a sale with an unreconcilable denominator -> unknown, excluded,
    counted.

Plus: stake-fraction math, the unusual flag (own-history percentile,
lookback, filing-date knowledge), multi-owner exclusion, the common-
stock scope, the empty-trigger identity of the variant simulator vs
the SMA baseline, trigger exit + strict re-entry, trigger building,
and the trigger IC.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import numpy as np
import pandas as pd
import pytest

from form4_exit import (QuarterTables, build_sale_observations,
                        build_triggers, compute_unusual,
                        first_eligible_action, simulate_form4,
                        trigger_daily_ics, trigger_ic_stats)
from market_calendar import is_trading_day

# ----------------------------------------------------------------------
# Synthetic SEC tables
# ----------------------------------------------------------------------

SUB_COLS = ["ACCESSION_NUMBER", "FILING_DATE", "PERIOD_OF_REPORT",
            "DATE_OF_ORIG_SUB", "DOCUMENT_TYPE", "ISSUERCIK", "ISSUERNAME",
            "ISSUERTRADINGSYMBOL", "AFF10B5ONE"]
OWNER_COLS = ["ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME",
              "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE"]
TRANS_COLS = ["ACCESSION_NUMBER", "SECURITY_TITLE", "TRANS_DATE",
              "TRANS_FORM_TYPE", "TRANS_CODE", "TRANS_SHARES",
              "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD",
              "SHRS_OWND_FOLWNG_TRANS", "DIRECT_INDIRECT_OWNERSHIP",
              "NATURE_OF_OWNERSHIP"]


def _sub(acc, filing, doc="4", cik="0000320193", sym="AAPL", plan=""):
    return {"ACCESSION_NUMBER": acc, "FILING_DATE": filing,
            "PERIOD_OF_REPORT": "31-MAR-2024", "DATE_OF_ORIG_SUB": "",
            "DOCUMENT_TYPE": doc, "ISSUERCIK": cik, "ISSUERNAME": "APPLE INC",
            "ISSUERTRADINGSYMBOL": sym, "AFF10B5ONE": plan}


def _owner(acc, cik="0001209191", name="COOK TIMOTHY D", rel="Officer"):
    return {"ACCESSION_NUMBER": acc, "RPTOWNERCIK": cik,
            "RPTOWNERNAME": name, "RPTOWNER_RELATIONSHIP": rel,
            "RPTOWNER_TITLE": "Chief Executive Officer"}


def _trans(acc, tdate="01-MAR-2024", code="S", shares="1000",
           price="180.00", held="9000", di="D", nature="",
           title="Common Stock", form="4"):
    return {"ACCESSION_NUMBER": acc, "SECURITY_TITLE": title,
            "TRANS_DATE": tdate, "TRANS_FORM_TYPE": form, "TRANS_CODE": code,
            "TRANS_SHARES": shares, "TRANS_PRICEPERSHARE": price,
            "TRANS_ACQUIRED_DISP_CD": "D" if code == "S" else "A",
            "SHRS_OWND_FOLWNG_TRANS": held,
            "DIRECT_INDIRECT_OWNERSHIP": di, "NATURE_OF_OWNERSHIP": nature}


def _tables(subs, owners, trans):
    return [QuarterTables(
        year=2024, quarter=1,
        submissions=pd.DataFrame(subs, columns=SUB_COLS),
        owners=pd.DataFrame(owners, columns=OWNER_COLS),
        trans=pd.DataFrame(trans, columns=TRANS_COLS))]


def _sessions(start: str, end: str) -> pd.DatetimeIndex:
    """NYSE sessions in [start, end] via the real exchange calendar."""
    days = pd.bdate_range(start, end)
    return pd.DatetimeIndex([d for d in days if is_trading_day(d.date())])


# ----------------------------------------------------------------------
# (a) The clock: sell Monday, file Wednesday after the close -> Thursday
# ----------------------------------------------------------------------

def test_fixture_a_clock_mon_wed_after_close():
    # Week of 2026-03-02: Mon 2nd, Tue 3rd, Wed 4th, Thu 5th, Fri 6th —
    # all NYSE sessions (verified via the calendar, not assumed).
    sessions = _sessions("2026-03-02", "2026-03-06")
    assert [str(d.date()) for d in sessions] == [
        "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06"]
    # Filed Wednesday after the close: 21:30 UTC = 16:30 ET.
    accepted = datetime(2026, 3, 4, 21, 30, tzinfo=timezone.utc)
    action = first_eligible_action(accepted, pd.Timestamp("2026-03-04"), sessions)
    assert action == pd.Timestamp("2026-03-05"), (
        "Wednesday-after-close filing must first act at Thursday's open")
    # Filed Wednesday BEFORE the open: 12:00 UTC = 07:00 ET -> Wednesday.
    accepted_early = datetime(2026, 3, 4, 12, 0, tzinfo=timezone.utc)
    assert first_eligible_action(accepted_early, pd.Timestamp("2026-03-04"),
                                 sessions) == pd.Timestamp("2026-03-04")
    # No timestamp (fallback): public at Wednesday's close -> Thursday.
    assert first_eligible_action(None, pd.Timestamp("2026-03-04"),
                                 sessions) == pd.Timestamp("2026-03-05")
    # Filing on the last session in the window -> no eligible action.
    accepted_fri = datetime(2026, 3, 6, 21, 30, tzinfo=timezone.utc)
    assert first_eligible_action(accepted_fri, pd.Timestamp("2026-03-06"),
                                 sessions[:4]) is None


def test_fixture_a_fallback_with_0500_sessions():
    # The panels' sessions are 05:00 timestamps (midnight ET in UTC).
    # The filing-date fallback must still return the NEXT session, not
    # the filing date's own session (05:00 > midnight would otherwise
    # compare as strictly later).
    sessions = pd.DatetimeIndex([
        pd.Timestamp("2026-03-02 05:00"), pd.Timestamp("2026-03-03 05:00"),
        pd.Timestamp("2026-03-04 05:00"), pd.Timestamp("2026-03-05 05:00"),
        pd.Timestamp("2026-03-06 05:00")])
    assert first_eligible_action(None, pd.Timestamp("2026-03-04"),
                                 sessions) == pd.Timestamp("2026-03-05 05:00")


# ----------------------------------------------------------------------
# (b) Amendment: same sale filed twice -> ONE observation, latest wins
# ----------------------------------------------------------------------

def test_fixture_b_amendment_is_one_observation():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4"),
         _sub("0002", "12-MAR-2024", "4/A")],
        [_owner("0001"), _owner("0002")],
        [_trans("0001", shares="1000", held="9000"),
         _trans("0002", shares="1200", held="8800")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 1, f"amended sale counted {len(obs)} times"
    assert obs.iloc[0]["accession"] == "0002"  # the amendment governs
    assert obs.iloc[0]["shares_sold"] == 1200.0
    assert obs.iloc[0]["shares_owned_following"] == 8800.0
    assert cov["n_amendment_superseded"] == 1


def test_amendment_with_changed_security_title_still_one():
    # The amendment re-words the title; the 4/A still supersedes the 4.
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4"),
         _sub("0002", "12-MAR-2024", "4/A")],
        [_owner("0001"), _owner("0002")],
        [_trans("0001", title="Common Stock", shares="1000", held="9000"),
         _trans("0002", title="Common Stock, par value $0.00001",
                shares="1000", held="9000")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 1
    assert obs.iloc[0]["accession"] == "0002"


def test_two_plain_4s_same_day_different_securities_are_two_sales():
    # Two separate plain-4 filings (not amendments) are separate sales.
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4"),
         _sub("0002", "05-MAR-2024", "4")],
        [_owner("0001"), _owner("0002")],
        [_trans("0001", title="Class A Common Stock", shares="1000", held="9000"),
         _trans("0002", title="Class B Common Stock", shares="500", held="4500")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 2


# ----------------------------------------------------------------------
# (c) Price-band split: multiple rows, one sale
# ----------------------------------------------------------------------

def test_fixture_c_price_band_split_is_one_observation():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4")],
        [_owner("0001")],
        [_trans("0001", shares="600", price="180.00", held="9400"),
         _trans("0001", shares="400", price="181.50", held="9000")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 1, f"price-band split counted {len(obs)} times"
    assert obs.iloc[0]["shares_sold"] == 1000.0
    # Holding after the whole sale = the minimum (final) holding.
    assert obs.iloc[0]["shares_owned_following"] == 9000.0
    assert obs.iloc[0]["n_rows"] == 2
    # VWAP: (600*180 + 400*181.5) / 1000 = 180.60
    assert obs.iloc[0]["price_vwap"] == pytest.approx(180.60)


# ----------------------------------------------------------------------
# (d) Unreconcilable denominator -> unknown, excluded, counted
# ----------------------------------------------------------------------

def test_fixture_d_missing_holding_is_unknown():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4"),
         _sub("0002", "06-MAR-2024", "4")],
        [_owner("0001"), _owner("0002")],
        [_trans("0001", shares="1000", held=""),  # no holding reported
         _trans("0002", shares="1000", held="9000")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 2
    assert pd.isna(obs.iloc[0]["stake_fraction"])
    assert obs.iloc[0]["exclusion_reason"] == "holding_missing"
    assert obs.iloc[1]["stake_fraction"] == pytest.approx(0.10)
    assert cov["n_stake_unknown_by_reason"] == {"holding_missing": 1}


def test_missing_shares_is_unknown():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4")],
        [_owner("0001")],
        [_trans("0001", shares="", held="9000")])
    obs, _ = build_sale_observations(tables)
    assert pd.isna(obs.iloc[0]["stake_fraction"])
    assert obs.iloc[0]["exclusion_reason"] == "shares_missing"


def test_typo_year_transaction_date_is_excluded_and_counted():
    # Real bulk-data defect: TRANS_DATE '25-JUL-0025' (a 2-digit year).
    # It is unparseable for timing, so the row is excluded and counted
    # — and it must not poison the date columns (year 25 is out of
    # bounds for datetime64[ns]).
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4"),
         _sub("0002", "06-MAR-2024", "4")],
        [_owner("0001"), _owner("0002")],
        [_trans("0001", tdate="25-JUL-0025", shares="1000", held="9000"),
         _trans("0002", shares="1000", held="9000")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 1
    assert obs.iloc[0]["accession"] == "0002"
    assert cov["n_rows_excluded_bad_dates"] == 1
    assert str(obs["trans_date"].dtype) == "datetime64[ns]"


# ----------------------------------------------------------------------
# Scope, identity, stake math
# ----------------------------------------------------------------------

def test_stake_fraction_math():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4")],
        [_owner("0001")],
        [_trans("0001", shares="1000", held="9000")])
    obs, _ = build_sale_observations(tables)
    # 1000 / (1000 + 9000) = 0.10
    assert obs.iloc[0]["stake_fraction"] == pytest.approx(0.10)


def test_non_common_securities_excluded_purchases_counted():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4")],
        [_owner("0001")],
        [_trans("0001", title="Restricted Stock Units", shares="1000",
                held="9000"),
         _trans("0001", code="P", shares="500", held="9500")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 0  # RSU sale out of scope; purchase kept separate
    assert cov["n_purchase_rows_P"] == 1
    assert cov["n_rows_excluded_not_common"] == 1


def test_multi_owner_accession_excluded():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4")],
        [_owner("0001", cik="0001209191", name="COOK TIMOTHY D"),
         _owner("0001", cik="0001543569", name="OTHER PERSON")],
        [_trans("0001", shares="1000", held="9000")])
    obs, cov = build_sale_observations(tables)
    assert len(obs) == 0
    assert cov["n_accessions_multi_owner"] == 1


def test_plan_flag_values():
    tables = _tables(
        [_sub("0001", "05-MAR-2024", "4", plan="1"),
         _sub("0002", "06-MAR-2024", "4", plan="0"),
         _sub("0003", "07-MAR-2024", "4", plan="")],
        [_owner("0001"), _owner("0002"), _owner("0003")],
        [_trans("0001"), _trans("0002"), _trans("0003")])
    obs, _ = build_sale_observations(tables)
    flags = obs.set_index("accession")["plan_10b5_1"].to_dict()
    assert flags["0001"] is True
    assert flags["0002"] is False
    assert flags["0003"] is None


# ----------------------------------------------------------------------
# The unusual flag: own-history percentile, knowledge, lookback, capacity
# ----------------------------------------------------------------------

def _obs_row(trans_date, filing_date, stake_sold, stake_held, accession,
             owner="0001209191", capacity="D|", security="common stock"):
    sold, held = stake_sold, stake_held
    stake = sold / (sold + held) if sold + held > 0 else np.nan
    return {"issuer_cik": "0000320193", "symbol": "AAPL", "owner_cik": owner,
            "trans_date": pd.Timestamp(trans_date),
            "filing_date": pd.Timestamp(filing_date), "accession": accession,
            "security_norm": security, "capacity": capacity,
            "shares_sold": float(sold), "shares_owned_following": float(held),
            "stake_fraction": stake, "exclusion_reason": None}


def test_unusual_flag_own_history_percentile():
    # Priors (stakes): 0.05, 0.10, 0.15, 0.20 -> p75 = 0.1625 (numpy
    # linear interpolation). Current 0.20 >= 0.1625 -> unusual.
    rows = [
        _obs_row("2023-06-01", "2023-06-03", 50, 950, "p1"),
        _obs_row("2023-09-01", "2023-09-03", 100, 900, "p2"),
        _obs_row("2023-12-01", "2023-12-03", 150, 850, "p3"),
        _obs_row("2024-02-01", "2024-02-03", 200, 800, "p4"),
        _obs_row("2024-03-01", "2024-03-05", 200, 800, "cur"),  # stake 0.20
        _obs_row("2024-03-02", "2024-03-06", 120, 880, "cur2"),  # stake 0.12
    ]
    obs = pd.DataFrame(rows)
    out, counts = compute_unusual(obs)
    by_acc = out.set_index("accession")
    assert bool(by_acc.loc["cur", "unusual"]) is True
    assert by_acc.loc["cur", "prior_p75"] == pytest.approx(0.1625)
    assert by_acc.loc["cur", "n_priors"] == 4
    assert bool(by_acc.loc["cur2", "unusual"]) is False
    # p4 is also unusual (3 priors, p75 = 0.125, stake 0.20); p1-p3
    # have too few priors to be flagged either way.
    assert counts["n_unusual"] == 2
    assert counts["n_not_unusual"] == 1
    assert counts["n_insufficient_history"] == 3


def test_unusual_flag_insufficient_history():
    rows = [
        _obs_row("2023-12-01", "2023-12-03", 150, 850, "p1"),
        _obs_row("2024-02-01", "2024-02-03", 200, 800, "p2"),
        _obs_row("2024-03-01", "2024-03-05", 900, 100, "cur"),  # stake 0.90
    ]
    out, counts = compute_unusual(pd.DataFrame(rows))
    assert bool(out.set_index("accession").loc["cur", "unusual"]) is False
    assert out.set_index("accession").loc["cur", "unusual_reason"] == \
        "insufficient_history"
    # p1 (0 priors) and p2 (1 prior) are insufficient too.
    assert counts["n_insufficient_history"] == 3


def test_unusual_flag_uses_only_history_public_by_filing():
    # A prior sale whose FILING date is AFTER the current sale's filing
    # date is not yet public knowledge -> must not count as a prior,
    # even though its transaction date is earlier.
    rows = [
        _obs_row("2023-06-01", "2023-06-03", 50, 950, "p1"),
        _obs_row("2023-09-01", "2023-09-03", 100, 900, "p2"),
        _obs_row("2023-12-01", "2023-12-03", 150, 850, "p3"),
        # Filed 2024-03-10, AFTER the current sale's 2024-03-05 filing:
        _obs_row("2024-02-01", "2024-03-10", 200, 800, "late_filer"),
        # Stake 0.14: >= p75 of the 3 public priors (0.125) -> unusual,
        # but < p75 of all 4 priors incl. the late filer (0.1625) -> the
        # flag proves the late filer was NOT counted.
        _obs_row("2024-03-01", "2024-03-05", 140, 860, "cur"),
    ]
    out, _ = compute_unusual(pd.DataFrame(rows))
    by_acc = out.set_index("accession")
    assert by_acc.loc["cur", "n_priors"] == 3  # late_filer excluded
    assert bool(by_acc.loc["cur", "unusual"]) is True


def test_unusual_flag_lookback_365_days():
    rows = [
        _obs_row("2022-06-01", "2022-06-03", 50, 950, "old"),  # > 365d old
        _obs_row("2023-06-01", "2023-06-03", 100, 900, "p1"),
        _obs_row("2023-09-01", "2023-09-03", 150, 850, "p2"),
        _obs_row("2023-12-01", "2023-12-03", 200, 800, "p3"),
        _obs_row("2024-03-01", "2024-03-05", 200, 800, "cur"),
    ]
    out, _ = compute_unusual(pd.DataFrame(rows))
    assert out.set_index("accession").loc["cur", "n_priors"] == 3


def test_unusual_flag_capacity_is_part_of_identity():
    # Priors under a DIFFERENT capacity (indirect trust) do not count
    # toward the direct-stake reference set.
    rows = [
        _obs_row("2023-06-01", "2023-06-03", 50, 950, "p1",
                 capacity="I|By CZI Holdings, LLC"),
        _obs_row("2023-09-01", "2023-09-03", 100, 900, "p2",
                 capacity="I|By CZI Holdings, LLC"),
        _obs_row("2023-12-01", "2023-12-03", 150, 850, "p3",
                 capacity="I|By CZI Holdings, LLC"),
        _obs_row("2024-03-01", "2024-03-05", 200, 800, "cur",
                 capacity="D|"),
    ]
    out, counts = compute_unusual(pd.DataFrame(rows))
    assert out.set_index("accession").loc["cur", "unusual_reason"] == \
        "insufficient_history"
    # p1, p2, p3 are insufficient too (0, 1, 2 same-capacity priors).
    assert counts["n_insufficient_history"] == 4


# ----------------------------------------------------------------------
# Simulator: empty-trigger identity, trigger exit, strict re-entry
# ----------------------------------------------------------------------

def _tiny_panels():
    from sma_timing import TrainPanels
    sessions = pd.bdate_range("2024-01-02", periods=12)
    syms = ["AAA", "BBB"]
    closes = pd.DataFrame({"AAA": [100.0 + i for i in range(12)],
                           "BBB": [50.0] * 12}, index=sessions)
    opens = closes.copy()
    sma50 = pd.DataFrame({"AAA": [99.0] * 12, "BBB": [49.0] * 12},
                         index=sessions)
    signal = pd.DataFrame({"AAA": [1.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                                   1.0, 1.0, 1.0, 1.0],
                           "BBB": [0.0] * 12}, index=sessions)
    held = pd.DataFrame({"AAA": [0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
                                 0.0, 1.0, 1.0, 1.0],
                         "BBB": [0.0] * 12}, index=sessions)
    return (TrainPanels(sessions=sessions, symbols=syms, opens=opens,
                        closes=closes, sma50=sma50, signal=signal,
                        spy_closes=pd.Series([400.0] * 12, index=sessions),
                        exclusions=[], n_train_sessions=12), held)


def test_variant_empty_triggers_reproduces_baseline():
    from sma_timing import simulate
    panels, held = _tiny_panels()
    base = simulate(panels, held, "baseline", 10.0)
    variant, fired = simulate_form4(panels, held, {}, 10.0)
    assert fired == []
    pd.testing.assert_series_equal(variant.equity_curve, base.equity_curve)
    assert variant.total_return == base.total_return
    assert variant.n_entries == base.n_entries == 2
    assert [(t.symbol, t.entry_date, t.exit_date, t.exit_reason)
            for t in variant.trades] == [
            (t.symbol, t.entry_date, t.exit_date, t.exit_reason)
            for t in base.trades]


def test_trigger_exits_at_next_open_and_blocks_reentry():
    panels, held = _tiny_panels()
    sessions = panels.sessions
    # Trigger AAA at the open of session 4 (index 4).
    triggers = {sessions[4]: [{"symbol": "AAA", "event_id": "e1"}]}
    variant, fired = simulate_form4(panels, held, triggers, 10.0)
    assert fired == ["e1"]
    assert len(variant.timing_exits) == 1
    tr = variant.timing_exits[0]
    assert tr.exit_reason == "form4_exit"
    assert tr.exit_date == str(sessions[4].date())
    assert tr.min_hold_override is True  # held 2 sessions of the 5-day min
    # The block clears only on a REBALANCE day with a positive signal
    # (fresh_selections: every 5th session -> indices 0, 5, 10). The
    # positive signal on sessions 8-9 is NOT a rebalance day, so the
    # block survives them; it clears on session 10 and AAA re-enters
    # at session 11's open (strict re-entry).
    entries = [(t.symbol, t.entry_date) for t in variant.trades]
    assert ("AAA", str(sessions[9].date())) not in entries
    assert ("AAA", str(sessions[11].date())) in entries
    assert variant.n_reentries_signal == 1
    # No second trigger fire: the event is consumed.
    variant2, fired2 = simulate_form4(panels, held, triggers, 10.0)
    assert fired2 == ["e1"]


def test_trigger_for_symbol_not_held_is_inert():
    panels, held = _tiny_panels()
    triggers = {panels.sessions[4]: [{"symbol": "BBB", "event_id": "e2"}]}
    variant, fired = simulate_form4(panels, held, triggers, 10.0)
    assert fired == []
    assert variant.timing_exits == []


# ----------------------------------------------------------------------
# Trigger building from baseline holdings
# ----------------------------------------------------------------------

def _trade(symbol, entry, exit_d):
    from sma_timing import Trade
    return Trade(symbol=symbol, entry_date=entry, entry_price=100.0,
                 exit_date=exit_d, exit_price=105.0, exit_reason="baseline",
                 weight=0.2, shares=2.0)


def _event_row(symbol="AAA", action="2024-02-05", trans="2024-02-01",
               filing="2024-02-03"):
    return {"symbol": symbol, "accession": "0001", "owner_cik": "0001209191",
            "owner_name": "COOK TIMOTHY D", "trans_date": pd.Timestamp(trans),
            "filing_date": pd.Timestamp(filing), "acceptance_ts": None,
            "has_timestamp": False, "stake_fraction": 0.5, "prior_p75": 0.2,
            "n_priors": 4, "shares_sold": 500.0, "plan_10b5_1": False,
            "capacity": "D|", "direct_indirect": "D", "unusual": True,
            "first_action": pd.Timestamp(action)}


def test_build_triggers_inside_holding_fires():
    sessions = _sessions("2024-01-02", "2024-08-17")
    trades = [_trade("AAA", "2024-01-10", "2024-03-01")]
    obs = pd.DataFrame([_event_row(action="2024-02-05")])
    triggers, by_id, counts = build_triggers(obs, trades, sessions,
                                             "2026-08-17")
    assert counts["n_triggers"] == 1
    assert pd.Timestamp("2024-02-05") in triggers
    assert triggers[pd.Timestamp("2024-02-05")][0]["symbol"] == "AAA"
    assert by_id[triggers[pd.Timestamp("2024-02-05")][0]["event_id"]]["stake_fraction"] == 0.5


def test_build_triggers_filing_before_entry_is_not_a_trigger():
    sessions = _sessions("2024-01-02", "2024-08-17")
    trades = [_trade("AAA", "2024-02-10", "2024-03-01")]
    obs = pd.DataFrame([_event_row(action="2024-02-05")])
    _, _, counts = build_triggers(obs, trades, sessions, "2026-08-17")
    assert counts["n_triggers"] == 0
    assert counts["n_filing_predates_holding"] == 1


def test_build_triggers_never_held_and_after_window():
    sessions = _sessions("2024-01-02", "2024-08-17")
    trades = [_trade("AAA", "2024-01-10", "2024-03-01")]
    obs = pd.DataFrame([
        _event_row(symbol="BBB", action="2024-02-05"),
        _event_row(symbol="AAA", action="2026-08-20"),
    ])
    _, _, counts = build_triggers(obs, trades, sessions, "2026-08-17")
    assert counts["n_never_held"] == 1
    assert counts["n_action_after_window"] == 1
    assert counts["n_triggers"] == 0


def test_build_triggers_action_same_session_as_entry_is_not_a_trigger():
    # Real-data case (ICE, 2024-04-08): the action session and the
    # holding entry are the same session — the filing became public
    # before the holding was entered, so it is NOT a trigger. The
    # panels' sessions are 05:00 timestamps while the trade entry is a
    # midnight date string; a naive `entry < action` compares True
    # (05:00 > 00:00) and wrongly fires.
    sessions = pd.DatetimeIndex([pd.Timestamp("2024-04-08 05:00"),
                                 pd.Timestamp("2024-04-09 05:00")])
    trades = [_trade("AAA", "2024-04-08", "2024-04-15")]
    obs = pd.DataFrame([_event_row(action="2024-04-08 05:00")])
    triggers, _, counts = build_triggers(obs, trades, sessions, "2026-08-17")
    assert counts["n_triggers"] == 0
    assert counts["n_filing_predates_holding"] == 1
    assert triggers == {}


def test_event_id_distinct_across_capacities():
    # One filing, one owner, one date, two capacities (direct + indirect)
    # are two observations and must get two distinct event ids.
    sessions = _sessions("2024-01-02", "2024-08-17")
    trades = [_trade("AAA", "2024-01-10", "2024-03-01")]
    rows = [_event_row(action="2024-02-05"),
            _event_row(action="2024-02-05")]
    rows[1]["capacity"] = "I|By Trust"
    rows[1]["direct_indirect"] = "I"
    obs = pd.DataFrame(rows)
    _, by_id, counts = build_triggers(obs, trades, sessions, "2026-08-17")
    assert counts["n_triggers"] == 2
    assert len(by_id) == 2


# ----------------------------------------------------------------------
# Trigger IC: triggered symbol with the worst forward return -> negative IC
# ----------------------------------------------------------------------

def test_trigger_ic_negative_when_triggered_symbol_lags():
    sessions = pd.bdate_range("2024-01-02", periods=26)
    syms = ["A", "B", "C", "D", "E"]
    # A falls 20 sessions after the action day; the rest rise.
    closes = pd.DataFrame(index=sessions, columns=syms, dtype=float)
    for s in syms:
        closes[s] = 100.0
    closes.loc[sessions[4:], "A"] = np.linspace(100, 90, 22)
    closes.loc[sessions[4:], "B"] = np.linspace(100, 110, 22)
    closes.loc[sessions[4:], "C"] = np.linspace(100, 108, 22)
    closes.loc[sessions[4:], "D"] = np.linspace(100, 106, 22)
    closes.loc[sessions[4:], "E"] = np.linspace(100, 104, 22)
    held = pd.DataFrame(0.2, index=sessions, columns=syms)
    triggers = {sessions[4]: [{"symbol": "A", "event_id": "e1"}]}
    from form4_exit import forward_returns_from_action
    fwd_rel = forward_returns_from_action(closes, 20)  # no sector map here
    daily = trigger_daily_ics(None, held, triggers, fwd_rel, min_n=4)
    assert len(daily) == 1
    assert daily.iloc[0]["n_triggered"] == 1
    assert daily.iloc[0]["ic"] < 0, "triggered laggard must give a negative IC"
    stats = trigger_ic_stats(daily)
    assert stats["n_days"] == 1
    assert stats["mean_ic"] == pytest.approx(daily.iloc[0]["ic"], abs=1e-4)
