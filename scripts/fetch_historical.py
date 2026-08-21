"""
fetch_historical.py
-------------------
Downloads daily OHLCV data for the Nifty 50 index and all 50 constituent
stocks, computes technical indicators, and writes one CSV per ticker.

Idempotent and incremental:
  - First run: full download from START_DATE
  - Later runs: only fetches rows newer than what is already saved

Indicators are computed here (pandas) rather than in DAX because rolling
window math over ~330k rows is far cheaper in Python than at query time.

Author: Atharva Phalak
"""

import os
import time
import argparse
from datetime import datetime, timedelta

import pandas as pd
import numpy as np
import yfinance as yf

# -- Configuration ------------------------------------------------------
HISTORICAL_DIR = "data/historical"
METADATA_FILE = "data/metadata/nifty50_stocks.csv"
INDEX_TICKER = "^NSEI"
INDEX_NAME = "NIFTY50_INDEX"
START_DATE = "2000-01-01"
REQUEST_DELAY = 0.4

# Fixed column order. Incremental appends use header=False, so this must
# never drift or the CSV will silently corrupt.
COLUMNS = [
    "Date", "Ticker", "Open", "High", "Low", "Close", "Volume",
    "Daily_Return_Pct", "MA20", "MA50", "MA200",
    "RSI_14", "MACD", "MACD_Signal", "MACD_Hist",
    "BB_Upper", "BB_Lower", "BB_Width",
    "Year", "Month", "Quarter", "Year_Month", "Price_Repaired",
]

# MA200 needs 200 prior rows to be correct, so incremental runs re-read a
# trailing window and recompute indicators across the seam.
LOOKBACK_ROWS = 250


# -- Indicator calculations ---------------------------------------------
def compute_rsi(close, period=14):
    """
    RSI using Wilder's smoothing (the standard method).
    RSI = 100 - (100 / (1 + RS)), where RS = avg gain / avg loss.

    Wilder's smoothing is an EMA with alpha = 1/period. Using a plain
    rolling mean instead is a common bug that makes values drift away
    from what TradingView and broker terminals display.
    """
    delta = close.diff()
    gain = delta.clip(lower=0).to_numpy(dtype=float)
    loss = (-delta.clip(upper=0)).to_numpy(dtype=float)
    n = len(close)

    avg_gain = np.full(n, np.nan)
    avg_loss = np.full(n, np.nan)

    if n > period:
        # Seed with a SIMPLE average of the first `period` changes, then
        # apply Wilder smoothing. Seeding with ewm's default (first value)
        # instead is the classic bug that makes RSI disagree with brokers.
        avg_gain[period] = np.nanmean(gain[1:period + 1])
        avg_loss[period] = np.nanmean(loss[1:period + 1])

        for i in range(period + 1, n):
            avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gain[i]) / period
            avg_loss[i] = (avg_loss[i - 1] * (period - 1) + loss[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

    # No losses in the window -> RS infinite -> RSI saturates at 100
    rsi = np.where((avg_loss == 0) & ~np.isnan(avg_loss), 100.0, rsi)
    return pd.Series(rsi, index=close.index)


def compute_macd(close):
    """
    MACD      = EMA(12) - EMA(26)
    Signal    = EMA(9) of MACD
    Histogram = MACD - Signal   (positive = bullish momentum building)
    """
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal, macd - signal


def compute_bollinger(close, period=20, num_std=2):
    """
    20-day moving average with bands at +/- 2 standard deviations.
    Width (as a percent of the mean) is a volatility proxy; narrow bands
    often precede large moves.
    """
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()
    upper = sma + num_std * std
    lower = sma - num_std * std
    width = (upper - lower) / sma * 100
    return upper, lower, width


def repair_price_spikes(df, jump=0.40, revert=0.20):
    """
    Repair isolated single-day price spikes -- bad ticks in the source feed.

    A bad tick jumps hugely then snaps straight back. A real event (split,
    crash, demerger) shifts the price level and it STAYS shifted. So a row
    is only repaired when all three hold:
      1. large move from the previous day
      2. large move to the next day
      3. previous and next day agree with each other   <- the revert test

    Condition 3 is what stops genuine crashes from being flattened.

    Example this catches: RELIANCE 2005-07-28 shows 187.09 sitting between
    neighbours around 42.8, then returns to 43.04 the next day. Left alone
    it corrupts every rolling window that contains it.

    Repaired rows are flagged in Price_Repaired so the edit stays auditable
    rather than silently rewriting history.
    """
    df = df.sort_values("Date").reset_index(drop=True)
    close = df["Close"].astype(float)
    prev_c, next_c = close.shift(1), close.shift(-1)

    from_prev = (close / prev_c - 1).abs()
    to_next = (close / next_c - 1).abs()
    neighbours_agree = (next_c / prev_c - 1).abs()

    bad = ((from_prev > jump) & (to_next > jump) &
           (neighbours_agree < revert)).fillna(False)

    df["Price_Repaired"] = bad
    if not bad.any():
        return df, 0

    # Scale the whole OHLC row by one factor so intraday shape is preserved
    fixed_close = (prev_c + next_c) / 2
    factor = (fixed_close / close).where(bad, 1.0)
    for col in ["Open", "High", "Low", "Close"]:
        df[col] = (df[col] * factor).round(2)

    return df, int(bad.sum())


def add_indicators(df):
    """Attach every derived column to a frame holding Date/OHLCV."""
    df = df.sort_values("Date").reset_index(drop=True)

    # Repair bad ticks BEFORE anything is derived from Close, otherwise a
    # single bad price propagates into 200 days of moving averages.
    df, repaired = repair_price_spikes(df)

    close = df["Close"]

    df["Daily_Return_Pct"] = close.pct_change() * 100
    df["MA20"] = close.rolling(20).mean()
    df["MA50"] = close.rolling(50).mean()
    df["MA200"] = close.rolling(200).mean()
    df["RSI_14"] = compute_rsi(close)
    df["MACD"], df["MACD_Signal"], df["MACD_Hist"] = compute_macd(close)
    df["BB_Upper"], df["BB_Lower"], df["BB_Width"] = compute_bollinger(close)

    dates = pd.to_datetime(df["Date"])
    df["Year"] = dates.dt.year
    df["Month"] = dates.dt.month
    df["Quarter"] = dates.dt.quarter
    df["Year_Month"] = dates.dt.to_period("M").astype(str)

    price_cols = ["Open", "High", "Low", "Close", "MA20", "MA50", "MA200",
                  "BB_Upper", "BB_Lower"]
    for col in price_cols:
        df[col] = df[col].round(2)
    for col in ["Daily_Return_Pct", "RSI_14", "BB_Width"]:
        df[col] = df[col].round(3)
    for col in ["MACD", "MACD_Signal", "MACD_Hist"]:
        df[col] = df[col].round(4)

    df.attrs["repaired"] = repaired
    return df


# -- Download helpers ---------------------------------------------------
def normalise_download(raw, ticker_name):
    """
    yfinance returns a MultiIndex column frame in some versions and a flat
    one in others. Normalise both shapes into a plain Date/OHLCV frame.
    """
    df = raw.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.reset_index()

    date_col = "Date" if "Date" in df.columns else df.columns[0]
    df = df.rename(columns={date_col: "Date"})
    df["Date"] = pd.to_datetime(df["Date"])
    if getattr(df["Date"].dt, "tz", None) is not None:
        df["Date"] = df["Date"].dt.tz_localize(None)

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError("missing columns from download: %s" % missing)

    df = df[["Date"] + required]
    df["Ticker"] = ticker_name
    return df.dropna(subset=["Close"])


def get_last_saved_date(path):
    """Most recent Date already stored, or None if the file does not exist."""
    if not os.path.exists(path):
        return None
    try:
        existing = pd.read_csv(path, usecols=["Date"], parse_dates=["Date"])
        return None if existing.empty else existing["Date"].max()
    except Exception:
        return None


def fetch_ticker(yahoo_ticker, save_name):
    """
    Download or top up one ticker. Returns a short status string.

    Incremental logic: re-read a trailing window from the existing file,
    download only recent dates, concatenate, recompute indicators over the
    combined tail, then append only genuinely new rows. This keeps MA200
    and RSI continuous across the join instead of restarting them at NaN.
    """
    path = os.path.join(HISTORICAL_DIR, save_name + ".csv")
    last_date = get_last_saved_date(path)
    today = datetime.today().date()

    if last_date is None:
        fetch_from = START_DATE
        mode = "full"
    else:
        if last_date.date() >= today - timedelta(days=1):
            return "up to date"
        fetch_from = (last_date - timedelta(days=LOOKBACK_ROWS * 2)).strftime("%Y-%m-%d")
        mode = "incremental"

    raw = yf.download(
        yahoo_ticker,
        start=fetch_from,
        end=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
        progress=False,
        auto_adjust=True,
    )
    if raw is None or raw.empty:
        return "no data returned"

    fresh = normalise_download(raw, save_name)

    if mode == "full":
        enriched = add_indicators(fresh)
        repaired = enriched.attrs.get("repaired", 0)
        out = enriched[COLUMNS]
        out.to_csv(path, index=False)
        note = ", %d bad tick(s) repaired" % repaired if repaired else ""
        return "created, %s rows%s" % (format(len(out), ","), note)

    existing = pd.read_csv(path, parse_dates=["Date"])
    combined = pd.concat([existing, fresh], ignore_index=True)
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = add_indicators(combined)[COLUMNS]

    new_rows = combined[combined["Date"] > last_date]
    if new_rows.empty:
        return "up to date"

    new_rows.to_csv(path, mode="a", header=False, index=False)
    return "+%d rows" % len(new_rows)


# -- Entry point --------------------------------------------------------
def fetch_all(limit=None, only=None, quiet=False):
    """
    Fetch every configured ticker and return a structured result.

    Separated from main() so the orchestrator can call this directly and
    inspect what happened, rather than scraping printed output.

    Returns dict with:
        succeeded  list of tickers that worked
        failed     list of (ticker, error message)
        no_data    tickers Yahoo returned nothing for (possible delisting)
        repaired   total bad ticks repaired this run
    """
    os.makedirs(HISTORICAL_DIR, exist_ok=True)
    stocks = pd.read_csv(METADATA_FILE)

    if only:
        stocks = stocks[stocks["NSE_Ticker"] == only]
        if stocks.empty:
            if not quiet:
                print("No stock found matching '%s'" % only)
            return {"succeeded": [], "failed": [], "no_data": [], "repaired": 0}
        targets = []
    else:
        targets = [(INDEX_TICKER, INDEX_NAME)]
        if limit:
            stocks = stocks.head(limit)

    targets += list(zip(stocks["Yahoo_Ticker"], stocks["NSE_Ticker"]))

    result = {"succeeded": [], "failed": [], "no_data": [], "repaired": 0}

    for i, (yahoo_ticker, save_name) in enumerate(targets, start=1):
        label = "[%d/%d] %-12s" % (i, len(targets), save_name)
        try:
            status = fetch_ticker(yahoo_ticker, save_name)
            if not quiet:
                print("%s %s" % (label, status))

            # "no data returned" is not an exception but is not success
            # either -- it is the signature of a delisted or renamed symbol.
            if "no data" in status:
                result["no_data"].append(save_name)
            else:
                result["succeeded"].append(save_name)

            if "repaired" in status:
                try:
                    result["repaired"] += int(status.split(",")[-1].strip().split()[0])
                except (ValueError, IndexError):
                    pass

        except Exception as exc:
            if not quiet:
                print("%s FAILED - %s" % (label, exc))
            result["failed"].append((save_name, str(exc)))

        time.sleep(REQUEST_DELAY)

    return result


def main():
    parser = argparse.ArgumentParser(description="Fetch Nifty 50 historical data")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N stocks (quick test run)")
    parser.add_argument("--only", type=str, default=None,
                        help="Process a single NSE ticker, e.g. --only RELIANCE")
    args = parser.parse_args()

    print("=" * 64)
    print("NIFTY 50 HISTORICAL DATA FETCH")
    print("Started: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)

    r = fetch_all(limit=args.limit, only=args.only)

    print("=" * 64)
    print("Done. %d succeeded, %d failed." % (len(r["succeeded"]), len(r["failed"])))
    if r["no_data"]:
        print("No data (check for delisting/rename): %s" % ", ".join(r["no_data"]))
    if r["failed"]:
        print("Failed tickers: %s" % ", ".join(t for t, _ in r["failed"]))
        print("Retry individually: python scripts/fetch_historical.py --only TICKER")
    print("=" * 64)


if __name__ == "__main__":
    main()
