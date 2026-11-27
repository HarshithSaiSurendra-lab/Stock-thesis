"""form4_exit.py — Form 4 unusual insider-sales exit signal (exp-form4-insider-exit-v1).

Research-only. Ingests the SEC Insider Transactions bulk data sets
(Forms 3/4/5, quarterly ZIPs), builds one-sale observations for
non-derivative common-stock sales coded S, computes the stake
fraction sold relative to the insider's own prior sales, flags
UNUSUAL sales (>= 75th percentile of the insider's own prior
12-month stake fractions, >= 3 known priors), and tests whether
exiting a v2-engine holding at the next eligible open after such a
sale becomes public improves the outcome vs continuing to hold.

Definitions are frozen in experiments/exp-form4-insider-exit-v1-design.md.
No LLM calls, $0 spend. Baseline holdings are reconstructed by
reusing the SMA harness (sma_timing.build_train_panels +
engine_weights) unchanged, so the baseline is identical to
exp-sma50-timing-v1's by construction.

Data sources (public, no auth):
- Bulk ZIPs: https://www.sec.gov/files/structureddata/data/
  insider-transactions-data-sets/{YYYY}q{N}_form345.zip
  (cached in .cache/form4/, never committed).
- Acceptance timestamps: https://data.sec.gov/submissions/CIK{cik}.json
  (cached in .cache/form4/submissions/; fallback = filing-date close).
"""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parent
FORM4_CACHE_DIR = REPO / ".cache" / "form4"
SUBMISSIONS_CACHE_DIR = FORM4_CACHE_DIR / "submissions"

SEC_UA = "ATS-Research HarshithSaiSurendra research contact hsurendra6@gatech.edu"
# The SEC moved the newest quarterly files to a new directory
# (2026q2+ live under datastandardsinnovation, not structureddata).
# Try the new path first, then the old one.
BULK_URLS = [
    ("https://www.sec.gov/files/datastandardsinnovation/data/"
     "insider-transactions-data-sets/{y}q{q}_form345.zip"),
    ("https://www.sec.gov/files/structureddata/data/"
     "insider-transactions-data-sets/{y}q{q}_form345.zip"),
]
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_FILE_URL = "https://data.sec.gov/submissions/{name}"  # name already ends .json

# Quarters to ingest: 2023 (12-month lookback for early-2024 sales)
# through 2026 Q3 (covers filings through the train end 2026-08-17).
QUARTERS = [(y, q) for y in (2023, 2024, 2025, 2026) for q in (1, 2, 3, 4)
            if not (y == 2026 and q == 4)]
LOOKBACK_DAYS = 365
MIN_PRIORS = 3
UNUSUAL_PCT = 75.0

ET = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)

COMMON_RE = re.compile(r"common stock", re.IGNORECASE)


def _sec_get(url: str, retries: int = 3) -> bytes:
    """GET with the SEC fair-access User-Agent; retries with backoff."""
    req = urllib.request.Request(
        url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate",
                      "Host": urllib.request.urlparse(url).netloc})
    last = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # noqa: BLE001 - retry transient SEC errors
            last = e
            time.sleep(2.0 * (attempt + 1))
    raise RuntimeError(f"SEC GET failed after {retries} attempts: {url}: {last}")


def download_quarterly_zips(quarters=None, cache_dir: Path | None = None,
                            sleep_s: float = 0.5) -> list[Path]:
    """Download missing quarterly bulk ZIPs into the cache dir.

    Returns the paths for all requested quarters (downloaded or cached).
    """
    quarters = quarters or QUARTERS
    cache_dir = cache_dir or FORM4_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for y, q in quarters:
        name = f"{y}q{q}_form345.zip"
        path = cache_dir / name
        if not path.exists():
            data = None
            last_err = None
            for pattern in BULK_URLS:
                url = pattern.format(y=y, q=q)
                try:
                    data = _sec_get(url)
                    break
                except Exception as e:  # noqa: BLE001 - try the next URL
                    last_err = e
            if data is None:
                raise RuntimeError(
                    f"SEC GET failed for {name} at all known URLs: {last_err}")
            path.write_bytes(data)
            print(f"downloaded {name} ({len(data)/1e6:.1f} MB)", flush=True)
            time.sleep(sleep_s)
        out.append(path)
    return out


def _norm_symbol(sym: str) -> str:
    """Trading symbol as the SEC files it, normalized to our universe form."""
    return str(sym or "").strip().upper().replace("/", ".")


def _norm_security(title: str) -> str:
    """Normalized security title (lowercased, punctuation/space collapsed)."""
    t = re.sub(r"[^a-z0-9 ]", " ", str(title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def _is_common_stock(title: str) -> bool:
    """Frozen scope: non-derivative common stock (see design doc)."""
    t = str(title or "").strip().lower()
    return "common stock" in t or t == "common"


def _parse_sec_date(s) -> pd.Timestamp | None:
    """DD-MON-YYYY (e.g. 31-JAN-2024) -> Timestamp (NaT-safe).

    Idempotent: a Timestamp/date input is returned unchanged, so the
    function is safe to call on already-parsed tables.

    Data-quality guard: the bulk sets cover 2006-2026 and the raw files
    contain typo'd years (e.g. TRANS_DATE '25-JUL-0025', '01-APR-2029'
    — a 2-digit year, or a future year). A year outside 2006..2026 is
    treated as unparseable (None) rather than a real date; a year-25
    Timestamp would otherwise break datetime64[ns] (out of bounds) and
    silently poison every date column it touches.
    """
    if s is None or (isinstance(s, float) and np.isnan(s)):
        return None
    if isinstance(s, pd.Timestamp):
        return s if 2006 <= s.year <= 2026 else None
    if isinstance(s, datetime):
        return pd.Timestamp(s) if 2006 <= s.year <= 2026 else None
    s = str(s).strip()
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%d-%b-%Y")
    except ValueError:
        return None
    if not 2006 <= dt.year <= 2026:
        return None
    return pd.Timestamp(dt.date())


# ----------------------------------------------------------------------
# Bulk-file loading (one quarter at a time; filtered to our universe)
# ----------------------------------------------------------------------

@dataclass
class QuarterTables:
    year: int
    quarter: int
    submissions: pd.DataFrame = field(default_factory=pd.DataFrame)
    owners: pd.DataFrame = field(default_factory=pd.DataFrame)
    trans: pd.DataFrame = field(default_factory=pd.DataFrame)


def load_quarter_tables(zip_path: Path, symbols: set[str]) -> QuarterTables:
    """Load one quarterly ZIP, filtered to Form 4/4A filings for `symbols`.

    Two-stage filter: (1) SUBMISSION rows whose ISSUERTRADINGSYMBOL
    (normalized) is in the universe; (2) re-filter by the ISSUERCIK
    set from stage 1, catching filings whose symbol field is blank.
    Returns the three tables (SUBMISSION, REPORTINGOWNER,
    NONDERIV_TRANS) restricted to the matched accessions.
    """
    yq = zip_path.stem.split("_")[0]  # e.g. 2024q1
    year, quarter = int(yq[:4]), int(yq[5])
    z = zipfile.ZipFile(zip_path)

    def _read(name: str) -> pd.DataFrame:
        with z.open(f"{name}.tsv") as f:
            return pd.read_csv(f, sep="\t", dtype=str, keep_default_na=False)

    subs = _read("SUBMISSION")
    subs = subs[subs["DOCUMENT_TYPE"].isin(["4", "4/A"])].copy()
    subs["SYM_NORM"] = subs["ISSUERTRADINGSYMBOL"].map(_norm_symbol)
    hit = subs[subs["SYM_NORM"].isin(symbols)]
    ciks = set(hit["ISSUERCIK"].unique())
    subs = subs[subs["SYM_NORM"].isin(symbols) | subs["ISSUERCIK"].isin(ciks)].copy()
    subs["FILING_DATE"] = subs["FILING_DATE"].map(_parse_sec_date)
    subs["PERIOD_OF_REPORT"] = subs["PERIOD_OF_REPORT"].map(_parse_sec_date)
    subs["DATE_OF_ORIG_SUB"] = subs["DATE_OF_ORIG_SUB"].map(_parse_sec_date)
    acc = set(subs["ACCESSION_NUMBER"].unique())

    owners = _read("REPORTINGOWNER")
    owners = owners[owners["ACCESSION_NUMBER"].isin(acc)].copy()

    trans = _read("NONDERIV_TRANS")
    trans = trans[trans["ACCESSION_NUMBER"].isin(acc)].copy()
    trans["TRANS_DATE"] = trans["TRANS_DATE"].map(_parse_sec_date)
    for col in ("TRANS_SHARES", "TRANS_PRICEPERSHARE", "SHRS_OWND_FOLWNG_TRANS"):
        trans[col] = pd.to_numeric(trans[col], errors="coerce")
    return QuarterTables(year=year, quarter=quarter, submissions=subs,
                         owners=owners, trans=trans)


def load_all_quarters(symbols: set[str], quarters=None) -> list[QuarterTables]:
    """Load every requested quarter (downloading missing ZIPs first)."""
    paths = download_quarterly_zips(quarters)
    return [load_quarter_tables(p, symbols) for p in paths]


# ----------------------------------------------------------------------
# Observation building: one sale = one observation
# ----------------------------------------------------------------------

def _plan_flag(value) -> bool | None:
    """AFF10B5ONE: '1' -> True; '0'/'false' -> False; blank -> None."""
    v = str(value or "").strip().lower()
    if v == "1":
        return True
    if v in ("0", "false"):
        return False
    return None


def build_sale_observations(tables: list[QuarterTables]) -> tuple[pd.DataFrame, dict]:
    """Aggregate Form 4 rows into one-sale observations (frozen rules).

    Returns (observations DataFrame, coverage dict). An observation is
    one sale by one owner in one capacity on one date, with amendments
    resolved to the latest filing. Multi-owner accessions are excluded
    (owner ambiguous). Purchases are counted separately and returned in
    the coverage dict (never mixed into the sale signal).
    """
    subs = pd.concat([t.submissions for t in tables], ignore_index=True)
    owners = pd.concat([t.owners for t in tables], ignore_index=True)
    trans = pd.concat([t.trans for t in tables], ignore_index=True)
    if "SYM_NORM" not in subs.columns:
        subs["SYM_NORM"] = subs["ISSUERTRADINGSYMBOL"].map(_norm_symbol)
    # Normalize raw bulk tables (idempotent: safe on already-parsed
    # tables from load_quarter_tables, and on synthetic test tables).
    for col in ("FILING_DATE", "PERIOD_OF_REPORT", "DATE_OF_ORIG_SUB"):
        if col in subs.columns:
            subs[col] = subs[col].map(_parse_sec_date)
    if "TRANS_DATE" in trans.columns:
        trans["TRANS_DATE"] = trans["TRANS_DATE"].map(_parse_sec_date)
    for col in ("TRANS_SHARES", "TRANS_PRICEPERSHARE", "SHRS_OWND_FOLWNG_TRANS"):
        if col in trans.columns:
            trans[col] = pd.to_numeric(trans[col], errors="coerce")
    coverage: dict = {
        "n_submissions_4_4a": int(len(subs)),
        "n_accessions_multi_owner": 0,
        "n_trans_rows_total": int(len(trans)),
        "n_purchase_rows_P": 0,
        "n_sale_rows_S": 0,
        "n_rows_excluded_not_common": 0,
        "n_rows_excluded_form_type": 0,
        "n_observations": 0,
        "n_amendment_superseded": 0,
    }

    # Owner per accession (single-owner only).
    owner_counts = owners.groupby("ACCESSION_NUMBER")["RPTOWNERCIK"].nunique()
    multi_owner = set(owner_counts[owner_counts > 1].index)
    coverage["n_accessions_multi_owner"] = int(len(multi_owner))
    owner_of = (owners.drop_duplicates("ACCESSION_NUMBER")
                .set_index("ACCESSION_NUMBER")["RPTOWNERCIK"].to_dict())
    owner_name = (owners.drop_duplicates("ACCESSION_NUMBER")
                  .set_index("ACCESSION_NUMBER")["RPTOWNERNAME"].to_dict())
    owner_rel = (owners.drop_duplicates("ACCESSION_NUMBER")
                 .set_index("ACCESSION_NUMBER")["RPTOWNER_RELATIONSHIP"].to_dict())

    sub_by_acc = subs.drop_duplicates("ACCESSION_NUMBER").set_index("ACCESSION_NUMBER")
    trans = trans[~trans["ACCESSION_NUMBER"].isin(multi_owner)].copy()
    trans["OWNER_CIK"] = trans["ACCESSION_NUMBER"].map(owner_of)
    trans = trans[trans["OWNER_CIK"].notna()].copy()
    # Join submission fields onto each row.
    trans["FILING_DATE"] = trans["ACCESSION_NUMBER"].map(sub_by_acc["FILING_DATE"])
    trans["DOCUMENT_TYPE"] = trans["ACCESSION_NUMBER"].map(sub_by_acc["DOCUMENT_TYPE"])
    trans["ISSUER_CIK"] = trans["ACCESSION_NUMBER"].map(sub_by_acc["ISSUERCIK"])
    trans["SYMBOL"] = trans["ACCESSION_NUMBER"].map(sub_by_acc["SYM_NORM"])
    trans["PLAN_10B5_1"] = trans["ACCESSION_NUMBER"].map(
        sub_by_acc["AFF10B5ONE"].map(_plan_flag)
        if "AFF10B5ONE" in sub_by_acc.columns
        else pd.Series(None, index=sub_by_acc.index))
    # Rows whose filing or transaction date is missing/unparseable
    # (incl. the typo'd years rejected by _parse_sec_date) are dropped
    # and counted; they cannot be timed, so they cannot be observations.
    coverage["n_rows_excluded_bad_dates"] = int(
        (~(trans["FILING_DATE"].notna() & trans["TRANS_DATE"].notna())).sum())
    trans = trans[trans["FILING_DATE"].notna() & trans["TRANS_DATE"].notna()].copy()

    purchases = trans[trans["TRANS_CODE"] == "P"]
    coverage["n_purchase_rows_P"] = int(len(purchases))

    sales = trans[(trans["TRANS_CODE"] == "S")
                  & (trans["TRANS_ACQUIRED_DISP_CD"] == "D")].copy()
    coverage["n_sale_rows_S"] = int(len(sales))
    sales = sales[sales["TRANS_FORM_TYPE"] == "4"].copy()
    coverage["n_rows_excluded_form_type"] = int(
        coverage["n_sale_rows_S"] - len(sales))
    sales["IS_COMMON"] = sales["SECURITY_TITLE"].map(_is_common_stock)
    coverage["n_rows_excluded_not_common"] = int((~sales["IS_COMMON"]).sum())
    # astype(bool): on an empty frame IS_COMMON is object-dtype and a
    # bare boolean mask would be read as column selection.
    sales = sales.loc[sales["IS_COMMON"].astype(bool)].copy()
    sales["SECURITY_NORM"] = sales["SECURITY_TITLE"].map(_norm_security)
    sales["CAPACITY"] = (sales["DIRECT_INDIRECT_OWNERSHIP"].fillna("") + "|"
                         + sales["NATURE_OF_OWNERSHIP"].fillna(""))
    # Price-band rows of one sale share (accession, owner, date, code,
    # security, capacity); aggregate within the accession first.
    grouped = sales.groupby(
        ["ACCESSION_NUMBER", "ISSUER_CIK", "SYMBOL", "OWNER_CIK",
         "TRANS_DATE", "TRANS_CODE", "SECURITY_NORM", "CAPACITY"],
        dropna=False, sort=False)
    agg_rows = []
    for key, g in grouped:
        (acc, issuer_cik, symbol, owner_cik, tdate, tcode, sec_norm, cap) = key
        shares = g["TRANS_SHARES"]
        held = g["SHRS_OWND_FOLWNG_TRANS"]
        agg_rows.append({
            "accession": acc,
            "issuer_cik": issuer_cik,
            "symbol": symbol,
            "owner_cik": owner_cik,
            "owner_name": owner_name.get(acc, ""),
            "owner_relationship": owner_rel.get(acc, ""),
            "trans_date": tdate,
            "trans_code": tcode,
            "security_norm": sec_norm,
            "capacity": cap,
            "direct_indirect": cap.split("|")[0],
            "filing_date": g["FILING_DATE"].iloc[0],
            "document_type": g["DOCUMENT_TYPE"].iloc[0],
            "plan_10b5_1": g["PLAN_10B5_1"].iloc[0],
            "shares_sold": float(shares.sum()) if shares.notna().any() else np.nan,
            "shares_sold_known": bool(shares.notna().any()),
            "shares_owned_following": float(held.min()) if held.notna().any() else np.nan,
            "holding_known": bool(held.notna().any()),
            "n_rows": int(len(g)),
            "price_vwap": (float((g["TRANS_SHARES"] * g["TRANS_PRICEPERSHARE"]).sum()
                                 / g["TRANS_SHARES"].sum())
                           if shares.notna().any() and g["TRANS_PRICEPERSHARE"].notna().any()
                           and float(shares.sum()) > 0 else np.nan),
        })
    agg = pd.DataFrame(agg_rows)
    if agg.empty:
        coverage["n_observations"] = 0
        coverage["n_stake_known"] = 0
        coverage["n_stake_unknown_by_reason"] = {}
        empty_cols = ["accession", "issuer_cik", "symbol", "owner_cik",
                      "owner_name", "owner_relationship", "trans_date",
                      "trans_code", "security_norm", "capacity",
                      "direct_indirect", "filing_date", "document_type",
                      "plan_10b5_1", "shares_sold", "shares_sold_known",
                      "shares_owned_following", "holding_known", "n_rows",
                      "price_vwap", "stake_fraction", "exclusion_reason"]
        return pd.DataFrame(columns=empty_cols), coverage
    # Coerce explicitly: a DataFrame built from row dicts leaves the
    # groupby-key date columns as object dtype even when every value
    # is a Timestamp (observed at full scale).
    agg["trans_date"] = pd.to_datetime(agg["trans_date"])
    agg["filing_date"] = pd.to_datetime(agg["filing_date"])

    # Amendment resolution (frozen): within a coarse sale key
    # (issuer, owner, date, code, capacity), a 4/A supersedes everything
    # filed before it; plain 4s accumulate. Walk accessions in filing
    # order; a 4/A clears the set built so far.
    agg = agg.sort_values(["filing_date", "accession"], kind="stable")
    keep_idx: list[int] = []
    superseded = 0
    coarse_keys = ["issuer_cik", "owner_cik", "trans_date", "trans_code", "capacity"]
    for _, g in agg.groupby(coarse_keys, dropna=False, sort=False):
        # Group by security within the coarse key: a sale's identity
        # includes its security; the amendment's version of a sale wins
        # as a whole (see design doc).
        current: dict[str, list] = {}
        for _, row in g.iterrows():
            if row["document_type"] == "4/A":
                # An amendment replaces all earlier filings of this sale.
                for sec_rows in current.values():
                    superseded += len(sec_rows)
                current = {}
            current.setdefault(row["security_norm"], []).append(row.name)
        for sec_rows in current.values():
            keep_idx.extend(sec_rows)
    obs = agg.loc[sorted(keep_idx)].reset_index(drop=True)
    coverage["n_amendment_superseded"] = int(superseded)

    # Stake fraction (frozen): only where the denominator reconciles.
    def _stake(row) -> tuple[float | None, str | None]:
        if not row["shares_sold_known"] or pd.isna(row["shares_sold"]) \
                or row["shares_sold"] <= 0:
            return None, "shares_missing"
        if not row["holding_known"] or pd.isna(row["shares_owned_following"]) \
                or row["shares_owned_following"] < 0:
            return None, "holding_missing"
        if not row["capacity"] or row["capacity"].startswith("|"):
            return None, "capacity_missing"
        denom = row["shares_sold"] + row["shares_owned_following"]
        if denom <= 0:
            return None, "holding_missing"
        return float(row["shares_sold"] / denom), None

    stakes = obs.apply(_stake, axis=1)
    obs["stake_fraction"] = [s[0] for s in stakes]
    obs["exclusion_reason"] = [s[1] for s in stakes]
    coverage["n_observations"] = int(len(obs))
    coverage["n_stake_known"] = int(obs["stake_fraction"].notna().sum())
    coverage["n_stake_unknown_by_reason"] = (
        obs.loc[obs["stake_fraction"].isna(), "exclusion_reason"]
        .value_counts().to_dict())
    return obs, coverage


# ----------------------------------------------------------------------
# Acceptance timestamps (data.sec.gov submissions API)
# ----------------------------------------------------------------------

def fetch_acceptance_timestamps(ciks: set[str], cache_dir: Path | None = None,
                               sleep_s: float = 0.15,
                               refresh: bool = False) -> dict[str, datetime]:
    """accession -> acceptance datetime (UTC) for Form 4 filings.

    One submissions-API call per issuer CIK (recent list + any older
    files whose range overlaps 2023). Results are cached as JSON in
    .cache/form4/submissions/ and reused unless refresh=True.
    """
    cache_dir = cache_dir or SUBMISSIONS_CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    out: dict[str, datetime] = {}
    for cik in sorted(ciks):
        cik10 = str(cik).zfill(10)
        cache_path = cache_dir / f"CIK{cik10}.json"
        if cache_path.exists() and not refresh:
            data = json.loads(cache_path.read_text())
        else:
            data = json.loads(_sec_get(
                SUBMISSIONS_URL.format(cik=int(cik))).decode("utf-8"))
            cache_path.write_text(json.dumps(data))
            time.sleep(sleep_s)
        acc_map = _acc_map_from_submissions(data)
        # Older files, if the recent list does not reach 2023-01-01.
        recent_from = _earliest_filing_date(data)
        if recent_from is None or recent_from > date(2023, 1, 1):
            for f in data.get("filings", {}).get("files", []):
                name = f.get("name", "")
                if not name.endswith(".json"):
                    continue
                fpath = cache_dir / name
                if fpath.exists() and not refresh:
                    fdata = json.loads(fpath.read_text())
                else:
                    fdata = json.loads(_sec_get(
                        SUBMISSIONS_FILE_URL.format(name=name)).decode("utf-8"))
                    fpath.write_text(json.dumps(fdata))
                    time.sleep(sleep_s)
                acc_map.update(_acc_map_from_submissions(fdata))
        out.update(acc_map)
    return out


def _acc_map_from_submissions(data: dict) -> dict[str, datetime]:
    """accession -> acceptance datetime from one submissions JSON."""
    out = {}
    rec = data.get("filings", {}).get("recent", {})
    accs = rec.get("accessionNumber", [])
    dts = rec.get("acceptanceDateTime", [])
    forms = rec.get("form", [])
    for acc, dt, form in zip(accs, dts, forms):
        if form not in ("4", "4/A"):
            continue
        try:
            out[str(acc)] = datetime.fromisoformat(str(dt).replace("Z", "+00:00"))
        except ValueError:
            continue
    return out


def _earliest_filing_date(data: dict):
    """Earliest filingDate in the recent list (None if empty)."""
    dates = data.get("filings", {}).get("recent", {}).get("filingDate", [])
    if not dates:
        return None
    try:
        return min(date.fromisoformat(str(d)) for d in dates if d)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Timing: first eligible action after a filing becomes public
# ----------------------------------------------------------------------

def first_eligible_action(acceptance_ts: datetime | None, filing_date: pd.Timestamp,
                          sessions: pd.DatetimeIndex) -> pd.Timestamp | None:
    """First session whose open is strictly after the filing became public.

    acceptance_ts (UTC) when known; otherwise the filing is treated as
    public at the close of filing_date (conservative fallback). Returns
    None when no session in `sessions` qualifies (e.g. the action would
    fall after the window end -> boundary exclusion).
    """
    if acceptance_ts is not None:
        ts_et = acceptance_ts.astimezone(ET)
        for s in sessions:
            open_et = datetime.combine(s.date(), MARKET_OPEN, tzinfo=ET)
            if open_et > ts_et:
                return s
        return None
    # Fallback: public at the close of the filing date -> the NEXT
    # session. Compare by DATE: the panels' sessions are 05:00
    # timestamps (midnight ET expressed in UTC), so a bare `>` against
    # the filing date at midnight would wrongly return the filing
    # date's own session.
    filing_day = pd.Timestamp(filing_date).normalize()
    later = sessions[sessions.normalize() > filing_day]
    return later[0] if len(later) else None


# ----------------------------------------------------------------------
# Unusual flag: stake fraction vs the insider's own prior sales
# ----------------------------------------------------------------------

def compute_unusual(obs: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Flag UNUSUAL sales (frozen): stake >= 75th percentile of the
    insider's own prior 12-month stake fractions (>= 3 known priors),
    using only history public by the filing date.

    Returns (observations with stake/unusual columns, coverage dict).
    """
    obs = obs.copy()
    obs["n_priors"] = 0
    obs["prior_p75"] = np.nan
    obs["unusual"] = False
    obs["unusual_reason"] = None
    counts = {"n_unusual": 0, "n_insufficient_history": 0,
              "n_not_unusual": 0, "n_unknown_stake": 0}
    known = obs[obs["stake_fraction"].notna()].copy()
    known = known.sort_values(["trans_date", "filing_date", "accession"],
                              kind="stable")
    # Prior reference set per (issuer, owner, capacity, security).
    keys = ["issuer_cik", "owner_cik", "capacity", "security_norm"]
    by_key: dict[tuple, list] = {}
    for _, row in known.iterrows():
        by_key.setdefault(tuple(row[k] for k in keys), []).append(row)
    for _, row in known.iterrows():
        key = tuple(row[k] for k in keys)
        priors = []
        for p in by_key[key]:
            if p.name == row.name:
                continue
            if p["trans_date"] >= row["trans_date"]:
                continue
            if p["trans_date"] < row["trans_date"] - timedelta(days=LOOKBACK_DAYS):
                continue
            if p["filing_date"] >= row["filing_date"]:
                continue
            priors.append(float(p["stake_fraction"]))
        idx = row.name
        obs.loc[idx, "n_priors"] = len(priors)
        if len(priors) < MIN_PRIORS:
            obs.loc[idx, "unusual_reason"] = "insufficient_history"
            counts["n_insufficient_history"] += 1
            continue
        p75 = float(np.percentile(priors, UNUSUAL_PCT))
        obs.loc[idx, "prior_p75"] = p75
        if float(row["stake_fraction"]) >= p75:
            obs.loc[idx, "unusual"] = True
            obs.loc[idx, "unusual_reason"] = "unusual"
            counts["n_unusual"] += 1
        else:
            obs.loc[idx, "unusual_reason"] = "not_unusual"
            counts["n_not_unusual"] += 1
    counts["n_unknown_stake"] = int(obs["stake_fraction"].isna().sum())
    return obs, counts


def attach_first_actions(obs: pd.DataFrame, timestamps: dict[str, datetime],
                         sessions: pd.DatetimeIndex) -> pd.DataFrame:
    """Add acceptance timestamp / first eligible action per observation."""
    obs = obs.copy()
    obs["acceptance_ts"] = obs["accession"].map(timestamps)
    obs["has_timestamp"] = obs["acceptance_ts"].notna()
    actions = []
    for _, row in obs.iterrows():
        ts = row["acceptance_ts"]
        # .map() leaves NaT for unmapped accessions; NaT is a datetime
        # instance, so the isinstance check alone does not catch it.
        if not isinstance(ts, datetime) or pd.isna(ts):
            ts = None
        actions.append(first_eligible_action(ts, row["filing_date"], sessions))
    obs["first_action"] = actions
    obs["first_action"] = pd.to_datetime(obs["first_action"])
    return obs


# ----------------------------------------------------------------------
# Trigger building: unusual sales vs baseline holdings
# ----------------------------------------------------------------------

def build_triggers(obs: pd.DataFrame, baseline_trades, sessions: pd.DatetimeIndex,
                   train_end: str) -> tuple[dict, dict, list]:
    """Map unusual sale events to (action session -> symbols) triggers.

    A trigger fires for a baseline holding when the event's first
    eligible action falls strictly after the holding's entry open and
    on or before the baseline's own exit (or the window end). Returns
    (triggers, event_by_id, exclusion counts). triggers maps a session
    to a list of {"symbol", "event_id"}; each event fires at most once
    (consumed on first use by the simulator).
    """
    train_end_ts = pd.Timestamp(train_end)
    triggers: dict = {}
    event_by_id: dict = {}
    counts = {"n_unusual": 0, "n_triggers": 0, "n_action_after_window": 0,
              "n_filing_predates_holding": 0, "n_never_held": 0,
              "n_no_first_action": 0}
    unusual = obs[obs["unusual"]].copy()
    counts["n_unusual"] = int(len(unusual))
    # Baseline holdings: (symbol, entry_date, exit_date or window end).
    holdings = []
    for tr in baseline_trades:
        exit_d = tr.exit_date if tr.exit_date else str(train_end_ts.date())
        holdings.append((tr.symbol, pd.Timestamp(tr.entry_date),
                         pd.Timestamp(exit_d)))
    by_symbol: dict[str, list] = {}
    for sym, entry_d, exit_d in holdings:
        by_symbol.setdefault(sym, []).append((entry_d, exit_d))
    held_symbols = set(by_symbol)
    for _, ev in unusual.iterrows():
        sym = ev["symbol"]
        action = ev["first_action"]
        # The event id identifies one observation: capacity and security
        # are part of the identity (one filing can report a direct and
        # an indirect sale of the same stock on the same date).
        event_id = (f"{sym}|{ev['accession']}|{ev['owner_cik']}|"
                    f"{pd.Timestamp(ev['trans_date']).date()}|"
                    f"{ev.get('capacity', '')}|{ev.get('security_norm', '')}")
        event_by_id[event_id] = {
            "event_id": event_id, "symbol": sym, "accession": ev["accession"],
            "owner_cik": ev["owner_cik"], "owner_name": ev.get("owner_name", ""),
            "trans_date": str(pd.Timestamp(ev["trans_date"]).date()),
            "filing_date": str(pd.Timestamp(ev["filing_date"]).date()),
            "acceptance_ts": (str(ev["acceptance_ts"])
                             if pd.notna(ev.get("acceptance_ts")) else None),
            "has_timestamp": bool(ev.get("has_timestamp", False)),
            "stake_fraction": float(ev["stake_fraction"]),
            "prior_p75": float(ev["prior_p75"]) if pd.notna(ev.get("prior_p75")) else None,
            "n_priors": int(ev.get("n_priors", 0)),
            "shares_sold": float(ev["shares_sold"]),
            "plan_10b5_1": (None if pd.isna(ev.get("plan_10b5_1"))
                           else bool(ev["plan_10b5_1"])),
            "capacity": ev.get("capacity", ""),
            "direct_indirect": ev.get("direct_indirect", ""),
        }
        if sym not in held_symbols:
            counts["n_never_held"] += 1
            continue
        if pd.isna(action):
            counts["n_no_first_action"] += 1
            continue
        # The trigger map is keyed by the session Timestamp as it
        # appears in panels.sessions (05:00) so the simulator's lookup
        # hits; the COMPARISONS use normalized dates (see below).
        action = pd.Timestamp(action)
        action_day = action.normalize()
        if action_day > train_end_ts:
            counts["n_action_after_window"] += 1
            continue
        # The event must fall inside a holding: entry < action <= exit.
        fired = False
        for entry_d, exit_d in by_symbol[sym]:
            entry_d = pd.Timestamp(entry_d).normalize()
            exit_d = pd.Timestamp(exit_d).normalize()
            if entry_d < action_day <= exit_d:
                triggers.setdefault(action, []).append(
                    {"symbol": sym, "event_id": event_id})
                event_by_id[event_id]["action_session"] = str(action.date())
                event_by_id[event_id]["holding_entry"] = str(entry_d.date())
                event_by_id[event_id]["holding_exit"] = str(exit_d.date())
                counts["n_triggers"] += 1
                fired = True
                break
        if not fired:
            counts["n_filing_predates_holding"] += 1
    return triggers, event_by_id, counts


# ----------------------------------------------------------------------
# Simulation: v2 replay + Form 4 exit overlay (port of sma_timing.simulate)
# ----------------------------------------------------------------------

def simulate_form4(panels, held, triggers: dict, cost_bps: float,
                   start_capital: float = 1000.0,
                   exit_delay_sessions: int = 0):
    """Replay the baseline held-weight trajectory with open fills, plus
    the Form 4 exit overlay.

    triggers: {action session -> [{"symbol", "event_id"}]}. A trigger
    exits the holding at that session's open (overriding the 5-day
    min-hold, counted). Each event fires at most once (consumed).
    exit_delay_sessions shifts every trigger one extra session later
    (sensitivity only). Re-entry is strict (frozen): a block clears
    only on a fresh engine selection WITH a positive v2 signal.
    With no triggers this reproduces the baseline exactly (asserted
    in tests).
    """
    from sma_timing import (Trade, V2_CFG, _is_whipsaw, fresh_selections,
                            rebalance_days)
    sessions = panels.sessions
    fresh = fresh_selections(panels.signal)
    n = len(sessions)
    # Shift triggers for the exit-delay sensitivity.
    trig: dict = {}
    if exit_delay_sessions > 0:
        for action, evs in triggers.items():
            loc = sessions.get_loc(action)
            if loc + exit_delay_sessions < n:
                trig.setdefault(sessions[loc + exit_delay_sessions], []).extend(evs)
    else:
        trig = {a: list(evs) for a, evs in triggers.items()}

    cash = start_capital
    positions: dict[str, dict] = {}
    blocked: dict[str, pd.Timestamp] = {}
    consumed: set[str] = set()
    trades: list = []
    open_trades: dict[str, Trade] = {}
    equity = pd.Series(np.nan, index=sessions, dtype=float)
    exposure = pd.Series(0.0, index=sessions, dtype=float)
    prev_actual_w = pd.Series(0.0, index=held.columns)
    turnover_sum = 0.0
    exposure_sum = 0.0
    invested_days = 0
    n_missing_open = 0
    n_block_cleared_signal = 0
    n_block_refused_all_zero = 0
    n_reentries_signal = 0
    clear_kind: dict[str, str] = {}
    fired_events: list[str] = []

    def price_at(t, s, col):
        frame = panels.opens if col == "open" else panels.closes
        v = frame.loc[t, s]
        if pd.notna(v):
            return float(v)
        prior = panels.closes[s].loc[:t].dropna()
        return float(prior.iloc[-1]) if not prior.empty else float("nan")

    def pv_at_open(t):
        return cash + sum(pos["shares"] * price_at(t, s, "open")
                          for s, pos in positions.items())

    for i, t in enumerate(sessions):
        prev_day = sessions[i - 1] if i > 0 else None
        # (a) clear Form 4 blocks via fresh selections on the latest
        #     completed session (strict: only a positive-signal selection
        #     clears; all-zero selections are counted as refused).
        if prev_day is not None and prev_day in fresh:
            for s in list(blocked):
                if s in fresh[prev_day] and blocked[s] < prev_day:
                    sig = float(panels.signal.loc[prev_day, s]) > 0
                    if not sig:
                        n_block_refused_all_zero += 1
                        continue
                    n_block_cleared_signal += 1
                    clear_kind[s] = "signal"
                    del blocked[s]
        # (b) Form 4 triggers whose action is today's open.
        form4_exits: set[str] = set()
        for ev in trig.get(t, []):
            s = ev["symbol"]
            if ev["event_id"] in consumed:
                continue
            if s in positions:
                form4_exits.add(s)
                consumed.add(ev["event_id"])
                fired_events.append(ev["event_id"])
        # --- target weights for today (baseline trajectory) ---
        target_w = held.loc[t].fillna(0.0)
        # --- execute exits at today's open ---
        for s in list(positions):
            baseline_exit = target_w[s] == 0
            form4_exit = s in form4_exits
            if not (baseline_exit or form4_exit):
                continue
            open_px = panels.opens.loc[t, s]
            if pd.isna(open_px):
                n_missing_open += 1
                continue
            pos = positions.pop(s)
            notional = pos["shares"] * open_px
            cost = notional * cost_bps / 1e4
            cash += notional - cost
            tr = open_trades.pop(s)
            tr.exit_date = str(t.date())
            tr.exit_price = round(float(open_px), 4)
            tr.exit_reason = "form4_exit" if form4_exit else "baseline"
            if form4_exit:
                tr.min_hold_override = (i - pos["entry_idx"]) < V2_CFG["min_hold_days"]
                tr.whipsaw = _is_whipsaw(panels.closes[s], panels.sma50[s],
                                         prev_day, sessions) if prev_day is not None else False
                blocked[s] = t
            trades.append(tr)
        # --- execute entries at today's open ---
        pv_open = pv_at_open(t)

        def _enter(s, w, day_idx):
            nonlocal cash, n_missing_open
            open_px = panels.opens.loc[t, s]
            if pd.isna(open_px):
                n_missing_open += 1
                return False
            notional = w * pv_open
            cost = notional * cost_bps / 1e4
            if cash < notional + cost:
                notional = max(cash - cost, 0.0)
                if notional <= 0:
                    return False
            shares = notional / open_px
            cash -= notional + cost
            positions[s] = {"shares": shares, "entry_date": t,
                            "entry_price": float(open_px), "weight": w,
                            "entry_idx": day_idx}
            tr = Trade(symbol=s, entry_date=str(t.date()),
                       entry_price=round(float(open_px), 4), weight=w, shares=shares)
            open_trades[s] = tr
            return True

        for s in held.columns:
            w = float(target_w[s])
            if w <= 0 or s in positions or s in blocked:
                if w <= 0:
                    clear_kind.pop(s, None)
                continue
            if _enter(s, w, i):
                kind = clear_kind.pop(s, None)
                if kind == "signal":
                    open_trades[s].reentry_kind = kind
                    n_reentries_signal += 1
        # --- actual weights after today's trades (open prices) ---
        actual_w = pd.Series(0.0, index=held.columns)
        for s, pos in positions.items():
            actual_w[s] = pos["shares"] * price_at(t, s, "open") / pv_open if pv_open > 0 else 0.0
        turnover_sum += float((actual_w - prev_actual_w).abs().sum())
        prev_actual_w = actual_w
        exposure.loc[t] = float(actual_w.sum())
        pv_close = cash + sum(pos["shares"] * price_at(t, s, "close")
                              for s, pos in positions.items())
        equity.loc[t] = pv_close
        exposure_sum += float(actual_w.sum())
        if float(actual_w.sum()) > 0:
            invested_days += 1

    last = sessions[-1]
    for s, tr in open_trades.items():
        tr.exit_date = str(last.date())
        tr.exit_price = (round(float(panels.closes.loc[last, s]), 4)
                         if pd.notna(panels.closes.loc[last, s]) else None)
        tr.exit_reason = "window_end"
        trades.append(tr)

    equity = equity.ffill()
    daily_returns = equity.pct_change().fillna(0.0)
    daily_returns.iloc[0] = 0.0
    total_return = float(equity.iloc[-1] / start_capital - 1)
    dd = float((equity / equity.cummax() - 1).min())
    timing = [t for t in trades if t.exit_reason == "form4_exit"]
    from sma_timing import SimResult
    return SimResult(
        equity_curve=equity, daily_returns=daily_returns,
        daily_exposure=exposure, trades=trades, timing_exits=timing,
        avg_daily_turnover=round(turnover_sum / n, 4),
        avg_gross_exposure=round(exposure_sum / n, 4),
        time_invested=round(invested_days / n, 4),
        n_entries=sum(1 for t in trades),
        n_baseline_exits=sum(1 for t in trades if t.exit_reason == "baseline"),
        n_sma_exits=len(timing),
        n_min_hold_overrides=sum(1 for t in timing if t.min_hold_override),
        n_insufficient=0, n_missing_open=n_missing_open,
        n_block_cleared_signal=n_block_cleared_signal,
        n_block_cleared_all_zero=0,
        n_block_refused_all_zero=n_block_refused_all_zero,
        n_reentries_signal=n_reentries_signal, n_reentries_all_zero=0,
        total_return=round(total_return, 4), max_drawdown=round(dd, 4),
    ), fired_events


def matched_exit_records(baseline_trades, variant_timing_exits) -> list[dict]:
    """One record per Form 4 exit with a baseline counterfactual price
    (same symbol + entry date; the baseline's eventual exit or
    window-end mark)."""
    base_by_key = {}
    for tr in baseline_trades:
        base_by_key[(tr.symbol, tr.entry_date)] = tr
    records = []
    for tr in variant_timing_exits:
        base = base_by_key.get((tr.symbol, tr.entry_date))
        if base is None or base.exit_price is None or tr.exit_price is None:
            continue
        effect = (tr.exit_price - base.exit_price) * tr.shares
        records.append({
            "symbol": tr.symbol, "entry_date": tr.entry_date,
            "entry_price": tr.entry_price,
            "timing_exit_date": tr.exit_date,
            "timing_exit_price": tr.exit_price,
            "baseline_exit_date": base.exit_date,
            "baseline_exit_price": base.exit_price,
            "baseline_exit_reason": base.exit_reason,
            "effect_usd": round(float(effect), 2),
            "whipsaw": bool(tr.whipsaw),
            "min_hold_override": bool(tr.min_hold_override),
        })
    return records


def timing_effect_breakdown(baseline_trades, variant_timing_exits) -> dict:
    """Losses avoided / gains forfeited per Form 4 exit (SMA-identical)."""
    records = matched_exit_records(baseline_trades, variant_timing_exits)
    avoided = sum(r["effect_usd"] for r in records if r["effect_usd"] > 0)
    forfeited = sum(r["effect_usd"] for r in records if r["effect_usd"] < 0)
    return {
        "losses_avoided_usd": round(avoided, 2),
        "gains_forfeited_usd": round(forfeited, 2),
        "n_avoided": sum(1 for r in records if r["effect_usd"] > 0),
        "n_forfeited": sum(1 for r in records if r["effect_usd"] < 0),
        "net_price_effect_usd": round(avoided + forfeited, 2),
        "n_unmatched": len(variant_timing_exits) - len(records),
        "records": records,
    }


# ----------------------------------------------------------------------
# Trigger IC + uncertainty (patterns from scripts/run_filing_delta.py)
# ----------------------------------------------------------------------

def forward_returns_from_action(closes: pd.DataFrame, horizon: int = 20) -> pd.DataFrame:
    """20-session forward return anchored at the action session's close:
    close(t+horizon)/close(t) - 1. The exit happens at open(t), so the
    return from close(t) is exactly the return the exit avoids/forfeits
    (the exit-day open->close move is not captured by the exit)."""
    return closes.shift(-horizon) / closes - 1.0


def sector_relative_fwd(fwd: pd.DataFrame) -> pd.DataFrame:
    """Stock forward return minus same-sector peer mean (excl. self).
    Sectors from sector_map.py; unmapped symbols -> NaN (excluded)."""
    from sector_map import sector_benchmark_for
    symbols = list(fwd.columns)
    sector_of = {s: sector_benchmark_for(s) for s in symbols}
    by_sector: dict[str, list[str]] = {}
    for s, b in sector_of.items():
        if b:
            by_sector.setdefault(b, []).append(s)
    out = pd.DataFrame(np.nan, index=fwd.index, columns=symbols)
    for s in symbols:
        b = sector_of[s]
        if not b:
            continue
        peers = [p for p in by_sector[b] if p != s]
        if not peers:
            continue
        out[s] = fwd[s] - fwd[peers].mean(axis=1)
    return out


def newey_west_se(x, lag: int) -> float:
    """Newey-West SE of the mean (Bartlett weights, given lag)."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n < 3:
        return float("nan")
    mean = x.mean()
    resid = x - mean
    var = float((resid ** 2).sum()) / n
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1)
        var += 2 * w * float((resid[k:] * resid[:-k]).sum()) / n
    return float(np.sqrt(var / n))


def trigger_daily_ics(panels, held: pd.DataFrame, triggers: dict,
                      fwd_rel: pd.DataFrame, min_n: int = 4) -> pd.DataFrame:
    """Per trigger day: Spearman IC of the trigger indicator (1 = held
    symbol triggered that day, 0 = other held symbols) vs the 20d
    sector-relative forward return from that day's close.

    The held set comes from the baseline held-weight matrix (the
    holdings under test). Only days with >= min_n held symbols with
    both values are used. Returns a DataFrame with one row per used
    trigger day: (day, n_held, n_triggered, ic).
    """
    rows = []
    for action, evs in sorted(triggers.items()):
        if action not in held.index:
            continue
        triggered = {e["symbol"] for e in evs}
        held_today = [s for s in held.columns if held.loc[action, s] > 0]
        fwd = fwd_rel.loc[action]
        vals = [(s, 1 if s in triggered else 0, float(fwd[s]))
                for s in held_today if pd.notna(fwd.get(s))]
        if len(vals) < min_n:
            continue
        ind = pd.Series([v[1] for v in vals], dtype=float)
        ret = pd.Series([v[2] for v in vals], dtype=float)
        if ind.nunique() < 2 or ret.nunique() < 2:
            continue
        ic = float(ind.rank().corr(ret.rank(), method="pearson"))
        if np.isnan(ic):
            continue
        rows.append({"day": action, "n_held": len(vals),
                     "n_triggered": sum(1 for v in vals if v[1] == 1),
                     "ic": ic})
    return pd.DataFrame(rows)


def trigger_ic_stats(daily: pd.DataFrame) -> dict:
    """Mean trigger IC with Newey-West (lag 19) inference."""
    if daily.empty:
        return {"n_days": 0, "mean_ic": None, "nw_se": None, "t_stat": None}
    ics = daily["ic"].astype(float)
    se = newey_west_se(ics, lag=19)
    mean = float(ics.mean())
    return {"n_days": int(len(daily)), "mean_ic": round(mean, 4),
            "nw_se": round(se, 4) if not np.isnan(se) else None,
            "t_stat": round(mean / se, 2) if se and not np.isnan(se) and se > 0 else None}


def issuer_cluster_bootstrap_ic(panels, held: pd.DataFrame, triggers: dict,
                                fwd_rel: pd.DataFrame, min_n: int = 4,
                                n_draws: int = 500, seed: int = 7) -> dict:
    """Issuer-cluster bootstrap of the mean trigger IC: resample the
    held symbols (issuers) with replacement, recompute the per-day ICs
    and their mean. 95% CI + share of draws with a negative mean."""
    rng = np.random.default_rng(seed)
    # Build the (day, symbol, indicator, fwd_rel) event table once.
    events: list[tuple] = []
    for action, evs in sorted(triggers.items()):
        if action not in held.index:
            continue
        triggered = {e["symbol"] for e in evs}
        held_today = [s for s in held.columns if held.loc[action, s] > 0]
        fwd = fwd_rel.loc[action]
        for s in held_today:
            if pd.isna(fwd.get(s)):
                continue
            events.append((action, s, 1 if s in triggered else 0, float(fwd[s])))
    if not events:
        return {"n_draws": 0, "ci_low": None, "ci_high": None,
                "share_negative": None}
    symbols = sorted({e[1] for e in events})
    by_symbol: dict[str, list] = {}
    for e in events:
        by_symbol.setdefault(e[1], []).append(e)
    means = []
    for _ in range(n_draws):
        chosen = rng.choice(symbols, size=len(symbols), replace=True)
        resampled = [ev for s in chosen for ev in by_symbol[s]]
        by_day: dict = {}
        for day, sym, ind, fwd in resampled:
            by_day.setdefault(day, []).append((ind, fwd))
        ics = []
        for day, vals in by_day.items():
            if len(vals) < min_n:
                continue
            ind = pd.Series([v[0] for v in vals], dtype=float)
            ret = pd.Series([v[1] for v in vals], dtype=float)
            if ind.nunique() < 2 or ret.nunique() < 2:
                continue
            ic = float(ind.rank().corr(ret.rank(), method="pearson"))
            if not np.isnan(ic):
                ics.append(ic)
        if ics:
            means.append(float(np.mean(ics)))
    if not means:
        return {"n_draws": 0, "ci_low": None, "ci_high": None,
                "share_negative": None}
    means = np.array(means)
    return {"n_draws": int(len(means)),
            "ci_low": round(float(np.percentile(means, 2.5)), 4),
            "ci_high": round(float(np.percentile(means, 97.5)), 4),
            "share_negative": round(float((means < 0).mean()), 3)}


def block_bootstrap_return_diff(variant, baseline, block: int = 20,
                                n_draws: int = 500, seed: int = 7) -> dict:
    """Time-block bootstrap of the net-return difference (variant minus
    baseline): resample 20-session blocks of the daily return
    difference; 95% CI of the total difference in pp."""
    rng = np.random.default_rng(seed)
    diff = (variant.daily_returns - baseline.daily_returns).to_numpy()
    n = len(diff)
    if n < block:
        return {"n_draws": 0, "ci_low_pp": None, "ci_high_pp": None,
                "share_positive": None}
    n_blocks = int(np.ceil(n / block))
    starts = rng.integers(0, n - block + 1, size=(n_draws, n_blocks))
    totals = []
    for draw in starts:
        seq = np.concatenate([diff[s:s + block] for s in draw])[:n]
        totals.append(float((1 + seq).prod() - 1) * 100)
    totals = np.array(totals)
    return {"n_draws": int(len(totals)),
            "ci_low_pp": round(float(np.percentile(totals, 2.5)), 2),
            "ci_high_pp": round(float(np.percentile(totals, 97.5)), 2),
            "share_positive": round(float((totals > 0).mean()), 3)}
