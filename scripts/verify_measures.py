"""
verify_measures.py
------------------
Computes, in Python, the exact values every Stage 4 DAX measure should
return. Run this, then compare against Power BI.

Why this exists: the dangerous DAX bug is not one that errors. It is one
that returns a plausible number. MAX(Close) with no date context returns
a stock's all-time high, which looks perfectly reasonable next to a label
saying "Current Price". The only way to catch that class of bug is to
compute the expected answer independently and compare.

Usage:
    python scripts/verify_measures.py
    python scripts/verify_measures.py --ticker TCS
    python scripts/verify_measures.py --all      # every ticker, summary only

Author: Atharva Phalak
"""

import os
import argparse
from datetime import timedelta

import numpy as np
import pandas as pd

HISTORICAL_DIR = "data/historical"
METADATA_FILE = "data/metadata/nifty50_stocks.csv"
METRICS_FILE = "data/metadata/stock_metrics.csv"
INDEX_NAME = "NIFTY50_INDEX"

# Tickers that broke something in an earlier stage. Worth checking every time.
EDGE_CASES = ["HDFCLIFE", "TMPV", "ADANIENT", "NESTLEIND", "NIFTY50_INDEX"]


def months_back(date, months):
    """
    Step back N calendar months, matching DAX's EDATE.

    Using 365*years instead drifts by a day or two per leap year, which
    can land on a different trading day and produce a return that looks
    almost-but-not-quite right.
    """
    year = date.year
    month = date.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = date.day
    # Clamp for short months (31 Mar minus 1 month is 28/29 Feb)
    while True:
        try:
            return pd.Timestamp(year=year, month=month, day=day)
        except ValueError:
            day -= 1


def return_over(df, months, latest_date, current_price):
    """
    Percentage return over N calendar months.

    Returns None when the stock has no history reaching that far back --
    the measure should go blank rather than silently anchoring to the
    earliest available price and reporting a number for a shorter period.
    """
    target = months_back(latest_date, months)
    earliest = df["Date"].min()
    if earliest > target:
        return None
    prior = df[df["Date"] <= target]
    if prior.empty:
        return None
    prior_price = prior.iloc[-1]["Close"]
    if prior_price == 0:
        return None
    return (current_price - prior_price) / prior_price * 100


def beta_label(b):
    """Mirror of the DAX Beta Label thresholds."""
    if b is None or pd.isna(b):
        return "Beta not available"
    if b > 1.3:
        tier = "Very high risk"
    elif b > 1.0:
        tier = "High risk"
    elif b > 0.7:
        tier = "Moderate risk"
    else:
        tier = "Low risk"
    return "%s - moves %.2fx the market" % (tier, b)


def sharpe_label(sr):
    """
    Mirror of the DAX Sharpe Label thresholds.

    These are calibrated to the Nifty 50 universe, NOT to textbook values.
    The usual ">1.0 is good" convention comes from portfolio and fund
    analysis; individual stocks measured over a single index cycle sit well
    below it. A threshold at 1.0 would leave a tier no stock could ever
    reach and would label the best performer "reasonable".

    RECALIBRATED after bug 18. Sharpe was previously computed over each
    stock's full price history, which meant every ticker was measured over a
    different period and a few were contaminated by pre-benchmark data. It is
    now measured over the benchmark window (2007-09-17 onward) for all 50,
    which shifted the whole distribution down:

        before   0.11 to 0.86, median 0.47
        after    0.12 to 0.84, median 0.36

    The old boundaries (0.70 / 0.45 / 0.25) sat above the new median, so a
    genuinely mid-table stock would have been labelled "below average". The
    new ones (0.50 / 0.36 / 0.30) land on the quartile boundaries of the
    current distribution, giving a roughly 24/28/24/24 split with the median
    stock on the above/below line rather than inside the lower tier.

    These need revisiting if the risk window ever changes again. The labels
    make a claim about where a stock sits among its peers, so they are only
    true relative to the distribution they were tuned against.
    """
    if sr is None or pd.isna(sr):
        return "Risk-adjusted return not available"
    if sr > 0.50:
        return "Top tier among Nifty 50 stocks"
    if sr > 0.36:
        return "Above average among Nifty 50 stocks"
    if sr > 0.30:
        return "Below average among Nifty 50 stocks"
    if sr > 0:
        return "Bottom tier among Nifty 50 stocks"
    return "Underperformed a risk-free deposit"


def recompute_beta(df, index_df):
    """
    Beta as a least-squares regression slope.

    The pipeline computes it as Cov(x,y)/Var(y). Recomputing by a different
    route means agreement is evidence rather than a tautology.
    """
    if index_df is None or index_df.empty:
        return None
    a = df.set_index("Date")["Daily_Return_Pct"]
    b = index_df.set_index("Date")["Daily_Return_Pct"]
    joined = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(joined) < 60:
        return None
    return float(np.polyfit(joined.iloc[:, 1], joined.iloc[:, 0], 1)[0])


def recompute_sharpe(df, index_df, risk_free=0.065, trading_days=252):
    """
    Sharpe recomputed from daily returns, over the benchmark window.

    The window restriction is not optional here. The pipeline measures Sharpe
    from the index start date (bug 18); recomputing over full history would
    produce a different number for any stock listed before 2007 and make this
    check report a disagreement that is not a bug. A cross-check has to use
    the same definition, or it is checking the wrong thing.
    """
    r = df["Daily_Return_Pct"]
    if index_df is not None and not index_df.empty:
        index_start = index_df["Date"].min()
        r = r[df["Date"] >= index_start]
    r = r.dropna()
    if len(r) < 60:
        return None
    daily_rf = (risk_free / trading_days) * 100
    excess = r - daily_rf
    if excess.std() == 0:
        return None
    return float((excess.mean() / excess.std()) * np.sqrt(trading_days))


def rsi_signal(r):
    """Mirror of the DAX RSI Signal thresholds."""
    if r is None or pd.isna(r):
        return "Not enough history for RSI"
    if r > 70:
        return "Overbought - buying has been heavy lately"
    if r > 55:
        return "Bullish momentum"
    if r > 45:
        return "Neutral - no clear momentum"
    if r > 30:
        return "Bearish momentum"
    return "Oversold - selling has been heavy lately"


def ma_signal(price, ma, window, horizon):
    """Mirror of the DAX Price vs MA measures."""
    if ma is None or pd.isna(ma):
        return "Not enough history for a %d-day average" % window
    if price > ma:
        return "Above its %d-day average - %s uptrend" % (window, horizon)
    return "Below its %d-day average - %s downtrend" % (window, horizon)


def overall_signal(price, ma50, ma200, rsi):
    """
    Mirror of the DAX Overall Signal.

    Availability is counted separately from bullishness on purpose. Without
    that guard a stock too young to have an MA200 would score the missing
    indicator as "not bullish", producing a bearish-looking signal from
    absent data rather than actual weakness.
    """
    available = sum(x is not None and pd.notna(x) for x in (rsi, ma50, ma200))
    if available < 3:
        return "Not enough history for a full signal"

    bullish = 0
    if pd.notna(rsi) and rsi > 50:
        bullish += 1
    if pd.notna(ma50) and price > ma50:
        bullish += 1
    if pd.notna(ma200) and price > ma200:
        bullish += 1

    return {
        3: "All three trend indicators are positive",
        2: "Two of three trend indicators are positive",
        1: "One of three trend indicators is positive",
        0: "None of the three trend indicators are positive",
    }[bullish]


def load(ticker):
    path = os.path.join(HISTORICAL_DIR, ticker + ".csv")
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path, parse_dates=["Date"]).sort_values("Date")
    return df.reset_index(drop=True)


def compute(ticker, meta, metrics, index_df):
    """Every Stage 4 measure value for one ticker."""
    df = load(ticker)
    if df is None or df.empty:
        return None

    latest = df.iloc[-1]
    latest_date = latest["Date"]

    out = {}

    # ---- Group A: identity -------------------------------------------
    row = meta[meta["NSE_Ticker"] == ticker]
    out["Selected Ticker"] = ticker
    out["Company Name"] = row["Company_Name"].iloc[0] if len(row) else "Nifty 50 Index"
    out["Sector"] = row["Sector"].iloc[0] if len(row) else "Index"

    # ---- Group B: price ----------------------------------------------
    out["Latest Date"] = latest_date.strftime("%Y-%m-%d")
    out["Stock Current Price"] = latest["Close"]

    # Previous TRADING row, not calendar day minus one. Weekends and
    # holidays mean date arithmetic gives the wrong answer here.
    prev_close = df.iloc[-2]["Close"] if len(df) >= 2 else np.nan
    out["Stock Previous Close"] = prev_close
    out["Stock Daily Change %"] = (
        (latest["Close"] - prev_close) / prev_close * 100
        if pd.notna(prev_close) and prev_close != 0 else np.nan
    )

    # 52-week window: trailing 365 calendar days ending at the latest row
    window = df[df["Date"] > latest_date - timedelta(days=365)]
    high_52 = window["High"].max()
    low_52 = window["Low"].min()
    out["Stock 52W High"] = high_52
    out["Stock 52W Low"] = low_52
    out["Distance from 52W High %"] = (
        (latest["Close"] - high_52) / high_52 * 100 if high_52 else np.nan
    )
    out["Distance from 52W Low %"] = (
        (latest["Close"] - low_52) / low_52 * 100 if low_52 else np.nan
    )

    # ---- Group C: returns ---------------------------------------------
    price = latest["Close"]
    for label, months in [("1Y", 12), ("3Y", 36), ("5Y", 60), ("10Y", 120)]:
        val = return_over(df, months, latest_date, price)
        out["Stock %s Return %%" % label] = np.nan if val is None else val

    # 10Y display string, matching the DAX fallback text
    ten_year = out["Stock 10Y Return %"]
    if pd.isna(ten_year):
        out["Stock 10Y Return Display"] = (
            "No 10Y history - listed %s" % df["Date"].min().strftime("%b %Y")
        )
    else:
        out["Stock 10Y Return Display"] = "%.2f%%" % ten_year

    # YTD anchors on the LAST CLOSE OF THE PREVIOUS YEAR, which is the
    # standard convention -- not the first trading day of January.
    year_start = pd.Timestamp(year=latest_date.year, month=1, day=1)
    prior_year = df[df["Date"] < year_start]
    if prior_year.empty:
        out["Stock YTD Return %"] = np.nan
    else:
        base = prior_year.iloc[-1]["Close"]
        out["Stock YTD Return %"] = (price - base) / base * 100

    # Benchmark comparison. The index is hardcoded on purpose -- you always
    # compare against the benchmark, whatever ticker is selected.
    if index_df is not None and not index_df.empty:
        idx_latest = index_df.iloc[-1]
        nifty_1y = return_over(index_df, 12, idx_latest["Date"], idx_latest["Close"])
        out["Nifty 1Y Return %"] = np.nan if nifty_1y is None else nifty_1y

        stock_1y = out["Stock 1Y Return %"]
        if pd.notna(stock_1y) and nifty_1y is not None:
            alpha = stock_1y - nifty_1y
            out["Alpha vs Nifty (1Y) %"] = alpha
            out["Outperform Label"] = (
                "Beat Nifty by %.2f%%" % abs(alpha) if alpha >= 0
                else "Lagged Nifty by %.2f%%" % abs(alpha)
            )
        else:
            out["Alpha vs Nifty (1Y) %"] = np.nan
            out["Outperform Label"] = "Not enough history to compare"

    # ---- Group D: risk -------------------------------------------------
    # The DAX measures read stored values from stock_metrics, so those are
    # what must match. Independent recomputes are shown alongside so a
    # disagreement between the two layers is visible too.
    stored = metrics[metrics["Ticker"] == ticker] if len(metrics) else pd.DataFrame()

    if len(stored):
        beta = stored["Beta"].iloc[0]
        sharpe = stored["Sharpe_Ratio"].iloc[0]
    else:
        beta = np.nan
        sharpe = np.nan

    out["Stock Beta"] = beta
    out["Beta Label"] = beta_label(beta)
    out["Stock Sharpe Ratio"] = sharpe
    out["Sharpe Label"] = sharpe_label(sharpe)

    check_beta = recompute_beta(df, index_df)
    check_sharpe = recompute_sharpe(df, index_df)
    if check_beta is not None and pd.notna(beta):
        out["  (Beta recomputed)"] = check_beta
    if check_sharpe is not None and pd.notna(sharpe):
        out["  (Sharpe recomputed)"] = check_sharpe

    # ---- Group E: technical --------------------------------------------
    rsi = latest.get("RSI_14", np.nan)
    ma50 = latest.get("MA50", np.nan)
    ma200 = latest.get("MA200", np.nan)

    out["Stock RSI"] = rsi
    out["RSI Signal"] = rsi_signal(rsi)
    out["Price vs MA50"] = ma_signal(price, ma50, 50, "short-term")
    out["Price vs MA200"] = ma_signal(price, ma200, 200, "long-term")

    # ---- Group F: composite --------------------------------------------
    out["Overall Signal"] = overall_signal(price, ma50, ma200, rsi)
    out["Last Updated"] = "Data as of %s" % latest_date.strftime("%d %b %Y")

    return out


def price_at_or_before(df, target_date):
    """
    Closing price on the last trading day at or before target_date.

    Returns None when the stock has no history that far back -- the
    measure should go blank rather than invent a number. HDFCLIFE
    (listed 2017) is the case that matters here.
    """
    prior = df[df["Date"] <= target_date]
    if prior.empty:
        return None
    return prior.iloc[-1]["Close"]


def show(ticker, values):
    print()
    print("-" * 66)
    print("  %s" % ticker)
    print("-" * 66)
    for key, val in values.items():
        if isinstance(val, float):
            if pd.isna(val):
                shown = "(blank)"
            elif abs(val) < 1:
                shown = "%.6f" % val
            elif abs(val) < 1000:
                shown = "%.4f" % val
            else:
                shown = "%,.2f".replace("%,", "{:,").format(val) if False else "{:,.2f}".format(val)
        else:
            shown = str(val)
        print("  %-28s %s" % (key, shown))


def main():
    parser = argparse.ArgumentParser(description="Expected values for Stage 4 measures")
    parser.add_argument("--ticker", default=None, help="Check one ticker")
    parser.add_argument("--all", action="store_true", help="Summary across all tickers")
    args = parser.parse_args()

    meta = pd.read_csv(METADATA_FILE)
    metrics = pd.read_csv(METRICS_FILE) if os.path.exists(METRICS_FILE) else pd.DataFrame()
    index_df = load(INDEX_NAME)

    print("=" * 66)
    print("EXPECTED MEASURE VALUES")
    print("Compare these against Power BI. They should match exactly.")
    print("=" * 66)

    if args.all:
        rows = []
        for ticker in [INDEX_NAME] + list(meta["NSE_Ticker"]):
            v = compute(ticker, meta, metrics, index_df)
            if v:
                rows.append({
                    "Ticker": ticker,
                    "Price": v["Stock Current Price"],
                    "Change%": v["Stock Daily Change %"],
                    "52W High": v["Stock 52W High"],
                    "From High%": v["Distance from 52W High %"],
                })
        summary = pd.DataFrame(rows)
        print()
        print(summary.to_string(index=False, float_format=lambda x: "%.4f" % x))
        return

    targets = [args.ticker] if args.ticker else EDGE_CASES

    for ticker in targets:
        values = compute(ticker, meta, metrics, index_df)
        if values is None:
            print("\n  %s -- no data file" % ticker)
            continue
        show(ticker, values)

    print()
    print("=" * 66)
    print("Edge cases these deliberately cover:")
    print("  HDFCLIFE       short history (2,167 rows)")
    print("  TMPV           structural break at the Oct 2025 demerger")
    print("  ADANIENT       prices span 5 orders of magnitude")
    print("  NESTLEIND      1,722 zero-volume flat days")
    print("  NIFTY50_INDEX  the default when nothing is selected")
    print("=" * 66)


if __name__ == "__main__":
    main()
