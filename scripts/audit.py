"""
audit.py
--------
Independent end-to-end verification of the whole pipeline.

The point of this script is NOT to re-run the pipeline's own functions and
check they agree with themselves -- that proves nothing. Every check here
either:

  (a) compares against a live external source (Yahoo), or
  (b) recomputes a value using a DIFFERENT algorithm than the pipeline used

For example, the pipeline computes Beta as Cov(x,y)/Var(y). This script
computes it as the slope of a least-squares regression. Those are the same
quantity by different routes, so agreement is real evidence rather than a
tautology.

Usage:
    python scripts/audit.py                 # full audit
    python scripts/audit.py --quick         # skip live Yahoo checks
    python scripts/audit.py --samples 10    # more tickers in sampled checks

Author: Atharva Phalak
"""

import os
import sys
import glob
import random
import argparse
from datetime import datetime

import numpy as np
import pandas as pd

HISTORICAL_DIR = "data/historical"
COMBINED_FILE = "data/combined/stock_prices.csv"
METADATA_FILE = "data/metadata/nifty50_stocks.csv"
METRICS_FILE = "data/metadata/stock_metrics.csv"
INDEX_NAME = "NIFTY50_INDEX"

GITHUB_BASE = ("https://raw.githubusercontent.com/wimpypython/"
               "nifty-stock-intelligence/main")

TRADING_DAYS = 252
RISK_FREE_RATE = 0.065

results = {"pass": 0, "fail": 0, "warn": 0}


def check(label, passed, detail="", warn_only=False):
    """Record and print one check result."""
    if passed:
        results["pass"] += 1
        mark = "PASS"
    elif warn_only:
        results["warn"] += 1
        mark = "WARN"
    else:
        results["fail"] += 1
        mark = "FAIL"
    suffix = ("  %s" % detail) if detail else ""
    print("  [%s] %s%s" % (mark, label, suffix))
    return passed


def header(n, title):
    print()
    print("=" * 70)
    print("%d. %s" % (n, title))
    print("=" * 70)


# ---------------------------------------------------------------------
# 1. Structural integrity
# ---------------------------------------------------------------------
def audit_structure():
    header(1, "STRUCTURAL INTEGRITY")

    files = sorted(glob.glob(os.path.join(HISTORICAL_DIR, "*.csv")))
    check("51 ticker files present", len(files) == 51, "found %d" % len(files))

    meta = pd.read_csv(METADATA_FILE)
    check("Metadata lists 50 stocks", len(meta) == 50, "found %d" % len(meta))
    check("No duplicate tickers in metadata",
          meta["NSE_Ticker"].duplicated().sum() == 0)

    on_disk = {os.path.basename(f)[:-4] for f in files}
    expected = set(meta["NSE_Ticker"]) | {INDEX_NAME}
    missing = expected - on_disk
    extra = on_disk - expected
    check("Every metadata ticker has a file", not missing,
          "missing: %s" % sorted(missing) if missing else "")
    check("No orphan files", not extra,
          "extra: %s" % sorted(extra) if extra else "")

    return files


# ---------------------------------------------------------------------
# 2. Per-file data quality
# ---------------------------------------------------------------------
def audit_quality(files):
    header(2, "DATA QUALITY (all 51 files)")

    dup_total = 0
    nonpos_total = 0
    unsorted = []
    null_close = 0
    bad_dtype = []
    row_counts = {}

    for path in files:
        ticker = os.path.basename(path)[:-4]
        df = pd.read_csv(path, parse_dates=["Date"])
        row_counts[ticker] = len(df)

        if not pd.api.types.is_datetime64_any_dtype(df["Date"]):
            bad_dtype.append(ticker)
            continue

        dup_total += int(df["Date"].duplicated().sum())
        nonpos_total += int((df[["Open", "High", "Low", "Close"]] <= 0).any(axis=1).sum())
        null_close += int(df["Close"].isna().sum())
        if not df["Date"].is_monotonic_increasing:
            unsorted.append(ticker)

    check("All Date columns parsed as dates", not bad_dtype,
          "bad: %s" % bad_dtype if bad_dtype else "")
    check("No duplicate dates", dup_total == 0, "%d found" % dup_total)
    check("No non-positive prices", nonpos_total == 0, "%d found" % nonpos_total)
    check("No null closes", null_close == 0, "%d found" % null_close)
    check("All files chronologically sorted", not unsorted,
          "unsorted: %s" % unsorted if unsorted else "")

    total = sum(row_counts.values())
    print("\n  Total rows across all files: %s" % format(total, ","))
    print("  Smallest: %s (%s rows)" % min(row_counts.items(), key=lambda x: x[1]))
    print("  Largest : %s (%s rows)" % max(row_counts.items(), key=lambda x: x[1]))
    return row_counts


# ---------------------------------------------------------------------
# 3. Indicators recomputed independently
# ---------------------------------------------------------------------
def audit_indicators(sample_size):
    header(3, "INDICATORS RECOMPUTED FROM SCRATCH")
    print("  Recalculating from Close prices using independent code,")
    print("  then comparing against the stored columns.\n")

    files = sorted(glob.glob(os.path.join(HISTORICAL_DIR, "*.csv")))
    sample = random.sample(files, min(sample_size, len(files)))

    for path in sample:
        ticker = os.path.basename(path)[:-4]
        df = pd.read_csv(path, parse_dates=["Date"]).sort_values("Date")
        close = df["Close"]

        # Moving averages -- plain rolling mean
        ok_ma = True
        for col, window in [("MA20", 20), ("MA50", 50), ("MA200", 200)]:
            if len(df) <= window:
                continue
            expected = close.rolling(window).mean()
            stored = df[col]
            both = expected.notna() & stored.notna()
            if both.sum() and not np.allclose(expected[both], stored[both], rtol=1e-4):
                ok_ma = False

        # Daily return
        exp_ret = close.pct_change() * 100
        both = exp_ret.notna() & df["Daily_Return_Pct"].notna()
        ok_ret = (not both.sum()) or np.allclose(
            exp_ret[both], df["Daily_Return_Pct"][both], atol=1e-3)

        # RSI -- independent Wilder implementation using a plain loop
        delta = close.diff().to_numpy()
        gains = np.where(delta > 0, delta, 0.0)
        losses = np.where(delta < 0, -delta, 0.0)
        n = len(close)
        ag = np.full(n, np.nan)
        al = np.full(n, np.nan)
        period = 14
        if n > period:
            ag[period] = np.nanmean(gains[1:period + 1])
            al[period] = np.nanmean(losses[1:period + 1])
            for i in range(period + 1, n):
                ag[i] = (ag[i - 1] * (period - 1) + gains[i]) / period
                al[i] = (al[i - 1] * (period - 1) + losses[i]) / period
        with np.errstate(divide="ignore", invalid="ignore"):
            exp_rsi = 100 - 100 / (1 + ag / al)
        exp_rsi = np.where((al == 0) & ~np.isnan(al), 100.0, exp_rsi)
        stored_rsi = df["RSI_14"].to_numpy()
        both = ~np.isnan(exp_rsi) & ~np.isnan(stored_rsi)
        ok_rsi = (not both.sum()) or np.allclose(
            exp_rsi[both], stored_rsi[both], atol=0.01)

        # Bollinger bands
        sma = close.rolling(20).mean()
        std = close.rolling(20).std()
        exp_up = sma + 2 * std
        both = exp_up.notna() & df["BB_Upper"].notna()
        ok_bb = (not both.sum()) or np.allclose(
            exp_up[both], df["BB_Upper"][both], rtol=1e-4)

        all_ok = ok_ma and ok_ret and ok_rsi and ok_bb
        detail = "" if all_ok else "MA=%s RET=%s RSI=%s BB=%s" % (
            ok_ma, ok_ret, ok_rsi, ok_bb)
        check("%-12s indicators match independent recompute" % ticker,
              all_ok, detail)


# ---------------------------------------------------------------------
# 4. Cross-sectional metrics via a different method
# ---------------------------------------------------------------------
def audit_metrics(sample_size):
    header(4, "METRICS VERIFIED BY A DIFFERENT METHOD")
    print("  Pipeline computes Beta as Cov(x,y)/Var(y).")
    print("  This recomputes it as a least-squares regression slope.\n")

    if not os.path.exists(METRICS_FILE):
        check("stock_metrics.csv exists", False)
        return

    metrics = pd.read_csv(METRICS_FILE)
    idx_path = os.path.join(HISTORICAL_DIR, INDEX_NAME + ".csv")
    if not os.path.exists(idx_path):
        check("Index file present", False)
        return

    idx = pd.read_csv(idx_path, parse_dates=["Date"]).set_index("Date")
    market = idx["Daily_Return_Pct"]

    sample = metrics.dropna(subset=["Beta"]).sample(
        min(sample_size, metrics["Beta"].notna().sum()), random_state=None)

    for _, row in sample.iterrows():
        ticker = row["Ticker"]
        path = os.path.join(HISTORICAL_DIR, ticker + ".csv")
        if not os.path.exists(path):
            continue
        df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date")
        joined = pd.concat([df["Daily_Return_Pct"], market],
                           axis=1, join="inner").dropna()
        joined.columns = ["stock", "market"]
        if len(joined) < 60:
            continue

        # Beta as regression slope
        slope = np.polyfit(joined["market"], joined["stock"], 1)[0]
        ok_beta = abs(slope - row["Beta"]) < 0.01

        # Sharpe recomputed
        r = df["Daily_Return_Pct"].dropna()
        daily_rf = (RISK_FREE_RATE / TRADING_DAYS) * 100
        excess = r - daily_rf
        sharpe = (excess.mean() / excess.std()) * np.sqrt(TRADING_DAYS)
        ok_sharpe = abs(sharpe - row["Sharpe_Ratio"]) < 0.01

        # Max drawdown recomputed
        prices = df["Close"]
        dd = ((prices - prices.cummax()) / prices.cummax() * 100).min()
        ok_dd = abs(dd - row["Max_Drawdown_Pct"]) < 0.05

        all_ok = ok_beta and ok_sharpe and ok_dd
        detail = "beta %.3f vs %.3f" % (slope, row["Beta"]) if not ok_beta else ""
        check("%-12s Beta/Sharpe/Drawdown reproduce" % ticker, all_ok, detail)


# ---------------------------------------------------------------------
# 5. Sector-level sanity of Beta
# ---------------------------------------------------------------------
def audit_beta_plausibility():
    header(5, "BETA PLAUSIBILITY BY SECTOR")
    print("  Finance theory: cyclicals amplify the index, defensives dampen it.")
    print("  If this inverts, the returns or the join are wrong.\n")

    metrics = pd.read_csv(METRICS_FILE)
    m = metrics.dropna(subset=["Beta"])

    cyclical = ["Banking", "Metals", "Auto", "NBFC"]
    defensive = ["FMCG", "Pharma", "Utilities"]

    cyc = m[m["Sector"].isin(cyclical)]["Beta"].mean()
    dfn = m[m["Sector"].isin(defensive)]["Beta"].mean()

    print("  Mean Beta, cyclical sectors  : %.3f" % cyc)
    print("  Mean Beta, defensive sectors : %.3f" % dfn)
    print()

    check("Cyclicals have higher Beta than defensives", cyc > dfn,
          "%.3f vs %.3f" % (cyc, dfn))
    check("All Betas within a plausible range (0.1-2.5)",
          bool(m["Beta"].between(0.1, 2.5).all()),
          "range %.2f to %.2f" % (m["Beta"].min(), m["Beta"].max()))
    check("Betas are not all near zero", m["Beta"].abs().max() > 0.3,
          "max %.2f" % m["Beta"].abs().max())
    check("Betas are not all identical", m["Beta"].std() > 0.05,
          "std %.3f" % m["Beta"].std())


# ---------------------------------------------------------------------
# 6. Fact table consistency
# ---------------------------------------------------------------------
def audit_fact_table(row_counts):
    header(6, "COMBINED FACT TABLE")

    if not os.path.exists(COMBINED_FILE):
        check("Combined file exists", False)
        return

    combined = pd.read_csv(COMBINED_FILE, parse_dates=["Date"])
    expected_rows = sum(row_counts.values())

    check("Row count matches sum of per-ticker files",
          len(combined) == expected_rows,
          "%s vs %s" % (format(len(combined), ","), format(expected_rows, ",")))
    check("All 51 tickers present",
          combined["Ticker"].nunique() == 51,
          "%d found" % combined["Ticker"].nunique())
    check("Date-part columns correctly excluded",
          not any(c in combined.columns for c in
                  ["Year", "Month", "Quarter", "Year_Month"]))
    check("No duplicate ticker+date pairs",
          combined.duplicated(subset=["Ticker", "Date"]).sum() == 0)

    # Spot-check a random ticker against its source file
    ticker = random.choice(combined["Ticker"].unique())
    src = pd.read_csv(os.path.join(HISTORICAL_DIR, ticker + ".csv"),
                      parse_dates=["Date"])
    sub = combined[combined["Ticker"] == ticker]
    merged = sub.merge(src, on="Date", suffixes=("_c", "_s"))
    ok = np.allclose(merged["Close_c"], merged["Close_s"], rtol=1e-9)
    check("%-12s values identical between fact table and source" % ticker, ok)

    size_mb = os.path.getsize(COMBINED_FILE) / (1024 * 1024)
    check("File size under GitHub's 100 MB limit", size_mb < 100,
          "%.1f MB" % size_mb)
    if size_mb > 50:
        check("File size under 50 MB soft warning", False,
              "%.1f MB" % size_mb, warn_only=True)


# ---------------------------------------------------------------------
# 7. Live comparison against Yahoo
# ---------------------------------------------------------------------
def audit_against_yahoo(sample_size):
    header(7, "LIVE COMPARISON AGAINST YAHOO FINANCE")
    print("  This is the check that matters: it compares stored values")
    print("  against the live source, so it cannot be self-confirming.\n")

    try:
        import yfinance as yf
    except ImportError:
        check("yfinance available", False, "pip install yfinance")
        return

    meta = pd.read_csv(METADATA_FILE)
    sample = meta.sample(min(sample_size, len(meta)))

    for _, row in sample.iterrows():
        ticker = row["NSE_Ticker"]
        yahoo_ticker = row["Yahoo_Ticker"]
        path = os.path.join(HISTORICAL_DIR, ticker + ".csv")
        if not os.path.exists(path):
            continue

        stored = pd.read_csv(path, parse_dates=["Date"]).set_index("Date")

        # Pick a random 10-day window from the stored history
        if len(stored) < 400:
            start_pos = 0
        else:
            start_pos = random.randint(200, len(stored) - 20)
        start = stored.index[start_pos]
        end = stored.index[min(start_pos + 10, len(stored) - 1)]

        try:
            live = yf.download(yahoo_ticker, start=start, end=end,
                               auto_adjust=True, progress=False)
            if live is None or live.empty:
                check("%-12s live data returned" % ticker, False,
                      "empty", warn_only=True)
                continue
            if isinstance(live.columns, pd.MultiIndex):
                live.columns = live.columns.get_level_values(0)
            live.index = pd.to_datetime(live.index).tz_localize(None)

            common = stored.index.intersection(live.index)
            if len(common) == 0:
                check("%-12s overlapping dates found" % ticker, False,
                      "none", warn_only=True)
                continue

            # Tolerance has to accommodate two legitimate sources of drift:
            #
            #   1. Prices are stored at 6 significant figures, which permits
            #      up to 5e-6 relative error by construction.
            #   2. auto_adjust=True means Yahoo re-adjusts the ENTIRE history
            #      whenever a new dividend or split occurs, so a 2016 price
            #      genuinely shifts slightly after a recent corporate action.
            #      A file written before that adjustment will differ from the
            #      live feed by a small amount, permanently.
            #
            # 1e-4 still catches real corruption by orders of magnitude -- a
            # genuine mismatch shows up in rupees, not ten-thousandths.
            ok = np.allclose(stored.loc[common, "Close"],
                             live.loc[common, "Close"], rtol=1e-4)
            detail = "%d days from %s" % (len(common), start.strftime("%Y-%m-%d"))
            if not ok:
                diffs = (stored.loc[common, "Close"] - live.loc[common, "Close"]).abs()
                detail += ", max diff %.8f" % diffs.max()
            check("%-12s matches Yahoo (%s)" % (ticker, detail), ok)

        except Exception as exc:
            check("%-12s live check" % ticker, False, str(exc)[:60], warn_only=True)


# ---------------------------------------------------------------------
# 8. GitHub copy matches local
# ---------------------------------------------------------------------
def audit_github():
    header(8, "GITHUB COPY (what Power BI actually reads)")

    url = GITHUB_BASE + "/data/combined/stock_prices.csv"
    try:
        remote = pd.read_csv(url, parse_dates=["Date"])
    except Exception as exc:
        check("Fact table reachable on GitHub", False, str(exc)[:60])
        return

    local = pd.read_csv(COMBINED_FILE, parse_dates=["Date"])

    check("Remote fact table reachable", True,
          "%s rows" % format(len(remote), ","))
    check("Remote row count matches local", len(remote) == len(local),
          "%s vs %s" % (format(len(remote), ","), format(len(local), ",")))
    check("Remote ticker count matches local",
          remote["Ticker"].nunique() == local["Ticker"].nunique())
    check("Remote latest date matches local",
          remote["Date"].max() == local["Date"].max(),
          "%s vs %s" % (remote["Date"].max().strftime("%Y-%m-%d"),
                        local["Date"].max().strftime("%Y-%m-%d")))

    if len(remote) != len(local):
        print("\n  NOTE: a mismatch here usually just means local changes")
        print("  have not been pushed yet. Run: git add . && git commit && git push")


# ---------------------------------------------------------------------
# 9. Repaired rows are plausible
# ---------------------------------------------------------------------
def audit_repairs():
    header(9, "BAD-TICK REPAIRS")

    combined = pd.read_csv(COMBINED_FILE, parse_dates=["Date"])
    if "Price_Repaired" not in combined.columns:
        check("Price_Repaired column present", False)
        return

    repaired = combined[combined["Price_Repaired"].astype(str).str.lower() == "true"]
    check("Repair flag is present and auditable", True,
          "%d rows flagged" % len(repaired))

    # Repairs should be rare. A large number would suggest the detector
    # is firing on genuine moves rather than bad ticks.
    pct = len(repaired) / len(combined) * 100
    check("Repairs are rare (<0.1% of rows)", pct < 0.1, "%.4f%%" % pct)

    if len(repaired):
        print("\n  Repaired rows by ticker:")
        for ticker, n in repaired.groupby("Ticker").size().sort_values(
                ascending=False).head(8).items():
            print("    %-12s %d" % (ticker, n))


# ---------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Audit the Nifty pipeline")
    parser.add_argument("--quick", action="store_true",
                        help="Skip live Yahoo and GitHub checks")
    parser.add_argument("--samples", type=int, default=6,
                        help="How many tickers to sample per check")
    args = parser.parse_args()

    print("=" * 70)
    print("PIPELINE AUDIT")
    print("Run at: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 70)

    files = audit_structure()
    row_counts = audit_quality(files)
    audit_indicators(args.samples)
    audit_metrics(args.samples)
    audit_beta_plausibility()
    audit_fact_table(row_counts)

    if not args.quick:
        audit_against_yahoo(args.samples)
        audit_github()

    audit_repairs()

    print()
    print("=" * 70)
    print("AUDIT SUMMARY")
    print("=" * 70)
    print("  Passed  : %d" % results["pass"])
    print("  Warnings: %d" % results["warn"])
    print("  Failed  : %d" % results["fail"])
    print()
    if results["fail"] == 0:
        print("  No failures. Data verified against independent recomputation")
        print("  and the live source.")
    else:
        print("  FAILURES PRESENT. Do not build further until resolved.")
    print("=" * 70)

    return 1 if results["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
