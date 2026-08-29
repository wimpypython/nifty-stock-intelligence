"""
calculate_indicators.py
-----------------------
Computes cross-sectional metrics that compare each stock against the
Nifty 50 index: Beta, Sharpe ratio, correlation, volatility, drawdown.

These live here rather than in DAX because each one needs a statistical
pass over a stock's entire return history against the index's entire
history. Doing that live in Power BI for 50 stocks would be slow; doing
it once per day in pandas costs about a second.

Output: data/metadata/stock_metrics.csv  (one summary row per stock)

Author: Atharva Phalak
"""

import os
from datetime import datetime

import pandas as pd
import numpy as np

HISTORICAL_DIR = "data/historical"
METADATA_FILE = "data/metadata/nifty50_stocks.csv"
OUTPUT_FILE = "data/metadata/stock_metrics.csv"
INDEX_NAME = "NIFTY50_INDEX"

TRADING_DAYS = 252
RISK_FREE_RATE = 0.065     # ~6.5%, roughly the Indian 10-year G-Sec yield
MIN_OVERLAP_DAYS = 60      # below this, Beta/Sharpe are not meaningful


def load_prices(ticker):
    """
    Load one ticker's price history, or None if unusable.

    Guards against a specific real failure: if Excel opens a CSV and saves
    it, dates can be rewritten from YYYY-MM-DD into a regional format like
    DD-MM-YYYY. pandas cannot parse those, silently leaves the column as
    strings, and every later date operation raises a confusing TypeError
    far away from the actual cause. Better to reject the file here with a
    clear message.
    """
    path = os.path.join(HISTORICAL_DIR, ticker + ".csv")
    if not os.path.exists(path):
        return None

    df = pd.read_csv(path, parse_dates=["Date"])
    if df.empty or "Close" not in df.columns:
        return None

    if not pd.api.types.is_datetime64_any_dtype(df["Date"]):
        raise ValueError(
            "Date column did not parse as dates (got %s). The file was "
            "probably opened and re-saved by Excel. Delete it and re-fetch: "
            "python scripts/fetch_historical.py --only %s"
            % (df["Date"].dtype, ticker)
        )

    return df.sort_values("Date").set_index("Date")


def restrict_to_index_window(series, index_start):
    """
    Trim a series to the period the benchmark also covers.

    WHY THIS EXISTS
    ---------------
    Beta and correlation were always safe: they use an inner join against the
    index, so they only ever saw overlapping dates. Sharpe, volatility and
    drawdown did not - they were computed over a stock's ENTIRE history.

    For most tickers that made no difference. For ADANIENT it did. Its
    split-adjusted prices in 2003 and 2004 sit at 0.58 and 2.84 rupees, and at
    that magnitude a single tick is a huge percentage: two daily returns of
    +744% and +109%. Those two rows inflated the standard deviation of the
    whole series, producing 162.78% annualised volatility against a 35.57%
    median across the index. A card labelled "typical yearly swing" was
    reading 162.78.

    The benchmark begins 2007-09-17. Restricting these three metrics to that
    window removes the artefacts and, more importantly, makes every risk
    metric describe the SAME period. Reporting a Beta measured over 2007+
    beside a volatility measured over 2000+ was quietly comparing two
    different things.

    WHAT THIS DOES NOT FIX
    ----------------------
    Structural breaks after the index start date. ADANIENT's 2015 demerger
    (-38.7% in one session) and TMPV's 2025 demerger both sit inside the
    window and still feed Max_Drawdown_Pct. Those are corporate actions, not
    data errors, and separating them from genuine market declines needs a
    corporate-actions feed this project does not have.

    Returns (1Y/3Y/5Y) are deliberately NOT restricted. They anchor from the
    most recent date and look backwards a fixed period, so early history
    cannot reach them.
    """
    if index_start is None:
        return series
    return series[series.index >= index_start]


def calc_beta(stock_ret, market_ret):
    """
    Beta = Cov(stock, market) / Var(market)

    Beta 1.0 means the stock historically moves in line with the index.
    Beta 1.4 means a 10% index move tends to come with a 14% stock move.
    Only overlapping trading days are used, so a stock listed in 2015
    is compared against the index over 2015-onward only.
    """
    joined = pd.concat([stock_ret, market_ret], axis=1, join="inner").dropna()
    if len(joined) < MIN_OVERLAP_DAYS:
        return None
    market_var = joined.iloc[:, 1].var()
    if market_var == 0:
        return None
    cov = joined.cov().iloc[0, 1]
    return round(cov / market_var, 3)


def calc_sharpe(returns):
    """
    Sharpe = (mean excess return / std of excess return) * sqrt(252)

    Answers: for the volatility this stock puts you through, is the
    return actually worth it? Above 1.0 is generally considered good.
    Returns here are percentages, so the daily risk-free rate is
    scaled to percent as well.
    """
    r = returns.dropna()
    if len(r) < MIN_OVERLAP_DAYS:
        return None
    daily_rf = (RISK_FREE_RATE / TRADING_DAYS) * 100
    excess = r - daily_rf
    if excess.std() == 0:
        return None
    return round((excess.mean() / excess.std()) * np.sqrt(TRADING_DAYS), 3)


def calc_max_drawdown(close):
    """
    Worst peak-to-trough decline ever recorded, as a negative percent.
    This is the number that tells a beginner what the stock's bad days
    have actually looked like historically.
    """
    running_peak = close.cummax()
    drawdown = (close - running_peak) / running_peak * 100
    return round(drawdown.min(), 2)


def calc_return_over(close, years):
    """Trailing return over N years, or None if history is too short."""
    if close.empty:
        return None
    end_date = close.index.max()
    start_target = end_date - pd.DateOffset(years=years)
    prior = close[close.index <= start_target]
    if prior.empty:
        return None
    start_price = prior.iloc[-1]
    if start_price == 0:
        return None
    return round((close.iloc[-1] / start_price - 1) * 100, 2)


def main():
    print("=" * 64)
    print("CROSS-SECTIONAL METRICS")
    print("Started: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)

    index_df = load_prices(INDEX_NAME)
    if index_df is None:
        print("ERROR: %s.csv not found in %s" % (INDEX_NAME, HISTORICAL_DIR))
        print("Run fetch_historical.py first.")
        return

    market_ret = index_df["Daily_Return_Pct"].rename("market")
    index_start = index_df.index.min()
    print("Benchmark loaded: %d trading days" % len(index_df))
    print("Benchmark starts: %s" % index_start.strftime("%Y-%m-%d"))
    print("Sharpe, volatility and drawdown are measured from this date so")
    print("every risk metric describes the same period.\n")

    stocks = pd.read_csv(METADATA_FILE)
    rows = []

    for _, meta in stocks.iterrows():
        ticker = meta["NSE_Ticker"]
        try:
            df = load_prices(ticker)
        except Exception as exc:
            # Degrade to "this ticker has no metrics", never kill the run.
            print("  %-12s SKIPPED - %s" % (ticker, exc))
            rows.append({
                "Ticker": ticker,
                "Company": meta["Company_Name"],
                "Sector": meta["Sector"],
                "Data_Points": 0,
            })
            continue

        if df is None:
            print("  %-12s SKIPPED (no data file)" % ticker)
            rows.append({
                "Ticker": ticker,
                "Company": meta["Company_Name"],
                "Sector": meta["Sector"],
                "Data_Points": 0,
            })
            continue

        try:
            stock_ret = df["Daily_Return_Pct"].rename("stock")
            close = df["Close"]

            # Sharpe, volatility and drawdown are measured over the benchmark
            # window so they cannot be distorted by pre-benchmark history and
            # so they describe the same period as Beta and correlation.
            # See restrict_to_index_window() for why.
            windowed_ret = restrict_to_index_window(stock_ret, index_start)
            windowed_close = restrict_to_index_window(close, index_start)

            joined = pd.concat([stock_ret, market_ret], axis=1, join="inner").dropna()
            correlation = (round(joined.corr().iloc[0, 1], 3)
                           if len(joined) >= MIN_OVERLAP_DAYS else None)

            row = {
                "Ticker": ticker,
                "Company": meta["Company_Name"],
                "Sector": meta["Sector"],
                "Beta": calc_beta(stock_ret, market_ret),
                "Sharpe_Ratio": calc_sharpe(windowed_ret),
                "Nifty_Correlation": correlation,
                "Annual_Volatility_Pct": round(
                    windowed_ret.std() * np.sqrt(TRADING_DAYS), 2
                ) if windowed_ret.notna().sum() >= MIN_OVERLAP_DAYS else None,
                "Max_Drawdown_Pct": (calc_max_drawdown(windowed_close)
                                     if len(windowed_close) else None),
                "Return_1Y_Pct": calc_return_over(close, 1),
                "Return_3Y_Pct": calc_return_over(close, 3),
                "Return_5Y_Pct": calc_return_over(close, 5),
                "Latest_RSI": (round(df["RSI_14"].dropna().iloc[-1], 2)
                               if "RSI_14" in df and df["RSI_14"].notna().any() else None),
                "Latest_Close": round(close.iloc[-1], 2),
                "Last_Date": close.index.max().strftime("%Y-%m-%d"),
                "Data_Points": len(df),
                # The window actually used for the risk metrics. Stored so the
                # figure is auditable rather than something a reader has to
                # infer from the code.
                "Risk_Window_Start": (windowed_close.index.min().strftime("%Y-%m-%d")
                                      if len(windowed_close) else None),
            }
            rows.append(row)

            print("  %-12s Beta=%-7s Sharpe=%-7s 1Y=%-8s pts=%d" % (
                ticker,
                row["Beta"], row["Sharpe_Ratio"], row["Return_1Y_Pct"], row["Data_Points"]
            ))

        except Exception as exc:
            print("  %-12s SKIPPED - metric calculation failed: %s" % (ticker, exc))
            rows.append({
                "Ticker": ticker,
                "Company": meta["Company_Name"],
                "Sector": meta["Sector"],
                "Data_Points": len(df) if df is not None else 0,
            })

    out = pd.DataFrame(rows)
    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    out.to_csv(OUTPUT_FILE, index=False)

    print("\n" + "=" * 64)
    print("Wrote %s (%d rows)" % (OUTPUT_FILE, len(out)))

    complete = out["Beta"].notna().sum()
    print("Stocks with full metrics: %d / %d" % (complete, len(out)))
    if complete:
        print("Beta range   : %.2f to %.2f" % (out["Beta"].min(), out["Beta"].max()))
        print("Median Sharpe: %.2f" % out["Sharpe_Ratio"].median())

    # Volatility outlier guard. A Nifty 50 constituent running at several
    # times the median annualised volatility is far more likely to be a data
    # artefact than a real characteristic - that is exactly how the ADANIENT
    # contamination showed itself. Warn rather than fail: the metrics are
    # still written, but the run says so out loud.
    vol = out["Annual_Volatility_Pct"].dropna()
    if len(vol):
        median_vol = vol.median()
        outliers = out[out["Annual_Volatility_Pct"] > 3 * median_vol]
        print("Median annual volatility: %.2f%%" % median_vol)
        if len(outliers):
            print()
            print("WARNING: %d ticker(s) above 3x the median volatility."
                  % len(outliers))
            print("Check for split-adjustment artefacts or structural breaks:")
            for _, r in outliers.iterrows():
                print("  %-12s %.2f%%  (from %s)"
                      % (r["Ticker"], r["Annual_Volatility_Pct"],
                         r.get("Risk_Window_Start", "?")))
    print("=" * 64)


if __name__ == "__main__":
    main()
