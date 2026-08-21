"""
verify_data.py
--------------
Post-fetch sanity checks. Run this after fetch_historical.py and
calculate_indicators.py to confirm the data is real and the indicators
warmed up correctly.

Usage (from project root):
    python scripts/verify_data.py
    python scripts/verify_data.py --ticker TCS

Author: Atharva Phalak
"""

import os
import argparse

import pandas as pd

HISTORICAL_DIR = "data/historical"
METRICS_FILE = "data/metadata/stock_metrics.csv"
INDEX_NAME = "NIFTY50_INDEX"


def line(char="-", n=64):
    print(char * n)


def check_file_inventory():
    """How many ticker files exist, and how big are they."""
    line("=")
    print("1. FILE INVENTORY")
    line("=")

    if not os.path.isdir(HISTORICAL_DIR):
        print("  MISSING: %s does not exist." % HISTORICAL_DIR)
        print("  Run: python scripts/fetch_historical.py")
        return []

    files = sorted(f for f in os.listdir(HISTORICAL_DIR) if f.endswith(".csv"))
    if not files:
        print("  No CSV files found. Run fetch_historical.py first.")
        return []

    total_mb = sum(
        os.path.getsize(os.path.join(HISTORICAL_DIR, f)) for f in files
    ) / (1024 * 1024)

    print("  Files found : %d" % len(files))
    print("  Total size  : %.1f MB" % total_mb)
    print("  Expected    : 51 (index + 50 stocks)")

    if len(files) < 51:
        missing = 51 - len(files)
        print("  NOTE: %d ticker(s) missing. Retry them individually:" % missing)
        print("        python scripts/fetch_historical.py --only TICKER")
    return files


def check_prices(ticker):
    """Show recent rows so you can eyeball them against a broker app."""
    line("=")
    print("2. PRICE SPOT-CHECK: %s" % ticker)
    line("=")

    path = os.path.join(HISTORICAL_DIR, ticker + ".csv")
    if not os.path.exists(path):
        print("  File not found: %s" % path)
        return None

    df = pd.read_csv(path, parse_dates=["Date"])
    print("  Rows        : %s" % format(len(df), ","))
    print("  Date range  : %s to %s" % (
        df["Date"].min().strftime("%Y-%m-%d"),
        df["Date"].max().strftime("%Y-%m-%d"),
    ))
    print()
    print("  Last 3 trading days -- compare these against Google Finance")
    print("  or your broker app. High/Low/Volume should match closely.")
    print("  Close may differ slightly (split/dividend adjusted).")
    print()

    tail = df[["Date", "Open", "High", "Low", "Close", "Volume"]].tail(3).copy()
    tail["Date"] = tail["Date"].dt.strftime("%Y-%m-%d")
    print(tail.to_string(index=False))
    return df


def check_indicators(df, ticker):
    """Confirm rolling indicators warmed up rather than being broken."""
    line("=")
    print("3. INDICATOR WARM-UP: %s" % ticker)
    line("=")

    if df is None:
        print("  Skipped (no data loaded).")
        return

    checks = []

    # Rolling windows must be blank early then populate. Blank early rows
    # are correct behaviour, not a bug.
    for col, window in [("MA20", 20), ("MA50", 50), ("MA200", 200)]:
        if col not in df.columns:
            checks.append((col + " present", False, "column missing"))
            continue
        early_blank = df[col].head(window - 1).isna().all()
        later_ok = len(df) > window and df[col].iloc[window:].notna().any()
        checks.append((
            "%s blank for first %d rows" % (col, window - 1),
            bool(early_blank), ""
        ))
        checks.append(("%s populated after warm-up" % col, bool(later_ok), ""))

    if "RSI_14" in df.columns:
        rsi = df["RSI_14"].dropna()
        in_range = bool(rsi.between(0, 100).all()) if len(rsi) else False
        checks.append(("RSI within 0-100", in_range,
                       "min %.1f max %.1f" % (rsi.min(), rsi.max()) if len(rsi) else "no values"))

    if {"BB_Upper", "BB_Lower"}.issubset(df.columns):
        bb = df[["BB_Upper", "BB_Lower"]].dropna()
        ok = bool((bb["BB_Upper"] > bb["BB_Lower"]).all()) if len(bb) else False
        checks.append(("Bollinger upper > lower", ok, ""))

    if "Daily_Return_Pct" in df.columns:
        r = df["Daily_Return_Pct"].dropna()
        # A single-day move beyond +/-25% on an index constituent usually
        # means an unadjusted split rather than a genuine move.
        extreme = int((r.abs() > 25).sum())
        checks.append(("No implausible daily moves (>25%)", extreme == 0,
                       "%d found" % extreme if extreme else ""))

    for label, passed, note in checks:
        mark = "PASS" if passed else "FAIL"
        suffix = ("  (%s)" % note) if note else ""
        print("  [%s] %s%s" % (mark, label, suffix))

    if not all(c[1] for c in checks):
        print()
        print("  One or more checks failed. Do not build the Power BI model")
        print("  on this data until resolved.")


def check_metrics():
    """Beta/Sharpe sanity across the whole universe."""
    line("=")
    print("4. CROSS-SECTIONAL METRICS")
    line("=")

    if not os.path.exists(METRICS_FILE):
        print("  Not found: %s" % METRICS_FILE)
        print("  Run: python scripts/calculate_indicators.py")
        return

    m = pd.read_csv(METRICS_FILE)
    complete = int(m["Beta"].notna().sum())
    print("  Stocks with full metrics : %d / %d" % (complete, len(m)))

    if complete == 0:
        print()
        print("  All Beta values are empty. The usual cause is that")
        print("  NIFTY50_INDEX.csv failed to download, so every stock was")
        print("  compared against nothing. Check that file exists first.")
        return

    print("  Beta range               : %.2f to %.2f" % (m["Beta"].min(), m["Beta"].max()))
    print("  Median Beta              : %.2f" % m["Beta"].median())

    # Sector-level expectation: banking/auto/metals tend to run high beta,
    # FMCG/pharma tend to run low. If this inverts, investigate.
    print()
    print("  Highest beta (expect Banking / Auto / Metals):")
    print(m.nlargest(3, "Beta")[["Ticker", "Sector", "Beta"]].to_string(index=False))
    print()
    print("  Lowest beta (expect FMCG / Pharma / Utilities):")
    print(m.nsmallest(3, "Beta")[["Ticker", "Sector", "Beta"]].to_string(index=False))

    if m["Beta"].abs().max() < 0.1:
        print()
        print("  WARNING: every beta is near zero. Stocks are not correlating")
        print("  with the index at all, which is not realistic. Check that")
        print("  NIFTY50_INDEX.csv contains real data.")

    stale = m[m["Data_Points"].fillna(0) < 100]
    if len(stale):
        print()
        print("  Tickers with very little history (may be delisted/renamed):")
        print(stale[["Ticker", "Data_Points"]].to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="Verify fetched Nifty data")
    parser.add_argument("--ticker", default="RELIANCE",
                        help="Which ticker to spot-check (default: RELIANCE)")
    args = parser.parse_args()

    print()
    files = check_file_inventory()
    print()

    ticker = args.ticker
    if files and (ticker + ".csv") not in files:
        fallback = files[0].replace(".csv", "")
        print("  '%s' not found, spot-checking '%s' instead.\n" % (ticker, fallback))
        ticker = fallback

    df = check_prices(ticker) if files else None
    print()
    check_indicators(df, ticker)
    print()
    check_metrics()
    print()
    line("=")
    print("Verification complete.")
    line("=")
    print()


if __name__ == "__main__":
    main()
