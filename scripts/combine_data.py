"""
combine_data.py
---------------
Merges the 51 per-ticker CSVs into a single long-format fact table for
Power BI.

Why combine at all: Power BI needs ONE table with a Ticker column, not 51
separate tables. With one table, a single set of DAX measures serves every
ticker and adding a 52nd stock requires no changes in Power BI at all.

Why do it here instead of in Power Query: this machine already runs nightly,
Power Query would have to fetch 51 URLs on every refresh, and a scheduled
refresh in the Power BI Service is far more likely to time out doing that.

Date parts (Year, Month, Quarter, Year_Month) are deliberately DROPPED. In a
star schema those attributes belong to the Date dimension, not the fact
table -- duplicating them across 330k rows wastes space and invites the two
copies to disagree.

Output: data/combined/stock_prices.csv

Author: Atharva Phalak
"""

import os
import glob
from datetime import datetime

import pandas as pd

HISTORICAL_DIR = "data/historical"
OUTPUT_DIR = "data/combined"
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "stock_prices.csv")

# Columns the fact table keeps. Date attributes are excluded on purpose --
# the Date dimension in Power BI owns those.
FACT_COLUMNS = [
    "Date", "Ticker", "Open", "High", "Low", "Close", "Volume",
    "Daily_Return_Pct", "MA20", "MA50", "MA200",
    "RSI_14", "MACD", "MACD_Signal", "MACD_Hist",
    "BB_Upper", "BB_Lower", "BB_Width", "Price_Repaired",
]

# GitHub warns above 50 MB and hard-refuses above 100 MB per file.
WARN_MB = 50
LIMIT_MB = 100


def combine():
    """Read every per-ticker CSV and return one long-format DataFrame."""
    files = sorted(glob.glob(os.path.join(HISTORICAL_DIR, "*.csv")))
    if not files:
        raise FileNotFoundError(
            "No CSVs in %s. Run fetch_historical.py first." % HISTORICAL_DIR
        )

    frames = []
    skipped = []

    for path in files:
        ticker = os.path.basename(path)[:-4]
        try:
            df = pd.read_csv(path, parse_dates=["Date"])

            # Same Excel guard as calculate_indicators: a re-saved CSV can
            # come back as DD-MM-YYYY, which pandas leaves as strings.
            if not pd.api.types.is_datetime64_any_dtype(df["Date"]):
                raise ValueError(
                    "Date column did not parse (got %s) -- probably re-saved "
                    "by Excel. Delete and re-fetch." % df["Date"].dtype
                )

            missing = [c for c in FACT_COLUMNS if c not in df.columns]
            if missing:
                raise ValueError("missing columns: %s" % missing)

            frames.append(df[FACT_COLUMNS])

        except Exception as exc:
            # One bad file must never take down the whole combine.
            skipped.append((ticker, str(exc)))

    if not frames:
        raise RuntimeError("Every file failed to load. Nothing to combine.")

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["Ticker", "Date"]).reset_index(drop=True)
    return combined, skipped


def main():
    print("=" * 64)
    print("COMBINING PER-TICKER FILES INTO FACT TABLE")
    print("Started: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)

    combined, skipped = combine()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    combined.to_csv(OUTPUT_FILE, index=False)

    size_mb = os.path.getsize(OUTPUT_FILE) / (1024 * 1024)
    tickers = combined["Ticker"].nunique()

    print("\n  Tickers   : %d" % tickers)
    print("  Rows      : %s" % format(len(combined), ","))
    print("  Columns   : %d" % len(combined.columns))
    print("  Date range: %s to %s" % (
        combined["Date"].min().strftime("%Y-%m-%d"),
        combined["Date"].max().strftime("%Y-%m-%d"),
    ))
    print("  File size : %.1f MB" % size_mb)
    print("  Written   : %s" % OUTPUT_FILE)

    if skipped:
        print("\n  SKIPPED %d file(s):" % len(skipped))
        for ticker, reason in skipped:
            print("    %-12s %s" % (ticker, reason))

    # Growth is roughly 51 rows per trading day, about 2 MB per year, so
    # this is a slow-moving problem -- but a silent one if never checked.
    if size_mb > LIMIT_MB:
        print("\n  ERROR: exceeds GitHub's 100 MB per-file limit.")
        print("  Split by date range, or switch to Parquet.")
        return 1
    if size_mb > WARN_MB:
        print("\n  WARNING: above GitHub's 50 MB soft limit (%.1f MB)." % size_mb)
        print("  Still works, but plan for Parquet or a date split.")

    print("\n" + "=" * 64)
    print("Power BI Web connector URL:")
    print("https://raw.githubusercontent.com/wimpypython/"
          "nifty-stock-intelligence/main/data/combined/stock_prices.csv")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
