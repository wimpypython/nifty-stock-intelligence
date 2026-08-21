"""
update_all_stocks.py
--------------------
Orchestrator run by GitHub Actions every trading night.

Chains fetch_historical -> calculate_indicators, then decides whether the
run was healthy enough to commit.

The important design decision here is failure classification. Not every
failure means the same thing:

  transient  - Yahoo hiccup, network blip. Happens. Retry tomorrow.
  persistent - same ticker failing for days. Probably delisted or renamed,
               the way TATAMOTORS did after the October 2025 demerger.
               Needs a human.
  systemic   - most tickers failing at once. Something is broken upstream.
               Do NOT commit; fail the run loudly instead.

A pipeline that fails silently is worse than one that does not run, so the
systemic case exits non-zero and lets GitHub mark the run red.

Exit codes:
    0  healthy (commit)
    1  systemic failure (do not commit)

Author: Atharva Phalak
"""

import os
import sys
import json
import glob
from datetime import datetime, timezone

import pandas as pd

# Scripts live alongside this file; make them importable regardless of
# where the process was launched from. GitHub Actions runs from repo root,
# local runs may not.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fetch_historical
import calculate_indicators

METADATA_DIR = "data/metadata"
STATE_FILE = os.path.join(METADATA_DIR, "pipeline_state.json")
STATUS_FILE = os.path.join(METADATA_DIR, "last_updated.txt")

# If more than this fraction of tickers fail, treat it as systemic.
SYSTEMIC_FAILURE_RATIO = 0.30
# Consecutive failures before a ticker is called out as probably delisted.
PERSISTENT_THRESHOLD = 3


def load_state():
    """Consecutive-failure counts carried across runs."""
    if not os.path.exists(STATE_FILE):
        return {"consecutive_failures": {}}
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return {"consecutive_failures": {}}


def save_state(state):
    os.makedirs(METADATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


def update_failure_counts(state, problem_tickers, all_attempted):
    """
    Increment counters for tickers that failed, reset those that worked.

    A ticker failing once is noise. The same ticker failing three nights
    running is a signal that the symbol itself has changed.
    """
    counts = state.get("consecutive_failures", {})

    for ticker in all_attempted:
        if ticker in problem_tickers:
            counts[ticker] = counts.get(ticker, 0) + 1
        elif ticker in counts:
            del counts[ticker]

    state["consecutive_failures"] = counts
    return [t for t, n in counts.items() if n >= PERSISTENT_THRESHOLD]


def check_freshness(max_stale_weekdays=3):
    """
    Detect a silently stalled feed.

    The orchestrator already catches loud failures, but there is a quieter
    one: Yahoo can return rows with a valid date and a NaN close. Those get
    dropped as worthless, so every ticker reports "up to date" and the run
    looks perfectly healthy while the dataset quietly ages.

    This compares the newest date on disk against today, counting weekdays
    only, and returns a warning string when the gap is too wide.

    Weekday counting is a deliberate approximation -- it ignores exchange
    holidays, so the threshold is set loose enough that a long weekend
    plus a holiday will not trip it.
    """
    files = glob.glob(os.path.join("data", "historical", "*.csv"))
    if not files:
        return None, None

    latest = None
    for path in files:
        try:
            dates = pd.read_csv(path, usecols=["Date"])["Date"]
            if dates.empty:
                continue
            newest = pd.to_datetime(dates).max()
            if latest is None or newest > latest:
                latest = newest
        except Exception:
            continue

    if latest is None:
        return None, None

    today = pd.Timestamp(datetime.now(timezone.utc).date())
    weekdays = len(pd.bdate_range(latest + pd.Timedelta(days=1), today)) if latest < today else 0

    if weekdays > max_stale_weekdays:
        return latest, (
            "DATA IS STALE: newest row is %s, roughly %d weekdays behind.\n"
            "  Every ticker may still report 'up to date' because the source\n"
            "  is returning empty rows. Check the feed before trusting the\n"
            "  dashboard."
            % (latest.strftime("%Y-%m-%d"), weekdays)
        )

    return latest, None


def write_status(summary):
    os.makedirs(METADATA_DIR, exist_ok=True)
    with open(STATUS_FILE, "w") as fh:
        fh.write(summary)


def main():
    started = datetime.now(timezone.utc)

    print("=" * 64)
    print("NIFTY 50 PIPELINE - AUTOMATED UPDATE")
    print("Started (UTC): %s" % started.strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 64)

    # ---- Step 1: fetch price data ---------------------------------
    print("\n[1/2] Fetching price data")
    print("-" * 64)
    try:
        result = fetch_historical.fetch_all()
    except Exception as exc:
        print("\nFATAL: fetch step crashed outright - %s" % exc)
        write_status("FAILED %s UTC - fetch crashed: %s\n"
                     % (started.strftime("%Y-%m-%d %H:%M:%S"), exc))
        return 1

    succeeded = result["succeeded"]
    failed = [t for t, _ in result["failed"]]
    no_data = result["no_data"]
    problems = failed + no_data
    attempted = succeeded + problems
    total = len(attempted)

    print("\n  succeeded : %d" % len(succeeded))
    print("  failed    : %d" % len(failed))
    print("  no data   : %d" % len(no_data))
    print("  bad ticks repaired: %d" % result["repaired"])

    # ---- Step 2: classify the failures ----------------------------
    if total == 0:
        print("\nFATAL: no tickers attempted. Check nifty50_stocks.csv exists.")
        write_status("FAILED %s UTC - no tickers attempted\n"
                     % started.strftime("%Y-%m-%d %H:%M:%S"))
        return 1

    failure_ratio = len(problems) / float(total)

    state = load_state()
    persistent = update_failure_counts(state, problems, attempted)
    save_state(state)

    if failure_ratio > SYSTEMIC_FAILURE_RATIO:
        print("\n" + "!" * 64)
        print("SYSTEMIC FAILURE: %d of %d tickers failed (%.0f%%)."
              % (len(problems), total, failure_ratio * 100))
        print("This looks like an upstream problem, not individual delistings.")
        print("Refusing to commit so partial data is not published.")
        print("!" * 64)
        write_status(
            "FAILED %s UTC - systemic: %d/%d tickers failed\n"
            % (started.strftime("%Y-%m-%d %H:%M:%S"), len(problems), total)
        )
        return 1

    if persistent:
        print("\n  ACTION NEEDED - failing %d+ runs in a row:" % PERSISTENT_THRESHOLD)
        for ticker in persistent:
            print("    %s (%d consecutive)" % (ticker, state["consecutive_failures"][ticker]))
        print("  These are probably delisted or renamed. Check the NSE symbol")
        print("  and update data/metadata/nifty50_stocks.csv.")
    elif problems:
        print("\n  Transient issues (will retry next run): %s" % ", ".join(problems))

    # ---- Step 3: recompute cross-sectional metrics ----------------
    print("\n[2/2] Recalculating metrics")
    print("-" * 64)
    try:
        calculate_indicators.main()
    except Exception as exc:
        print("\nFATAL: metrics step crashed - %s" % exc)
        write_status("FAILED %s UTC - metrics crashed: %s\n"
                     % (started.strftime("%Y-%m-%d %H:%M:%S"), exc))
        return 1

    # ---- Step 4: write the audit trail ----------------------------
    finished = datetime.now(timezone.utc)
    duration = (finished - started).total_seconds()

    latest_date, stale_warning = check_freshness()

    if stale_warning:
        print("\n" + "!" * 64)
        print(stale_warning)
        print("!" * 64)

    lines = [
        "Last successful update: %s UTC" % finished.strftime("%Y-%m-%d %H:%M:%S"),
        "Duration: %.0f seconds" % duration,
        "Tickers updated: %d of %d" % (len(succeeded), total),
        "Bad ticks repaired this run: %d" % result["repaired"],
    ]
    if latest_date is not None:
        lines.insert(3, "Newest data point: %s" % latest_date.strftime("%Y-%m-%d"))
    if stale_warning:
        lines.append("WARNING: %s" % stale_warning.split("\n")[0])
    if problems:
        lines.append("Tickers with issues: %s" % ", ".join(problems))
    if persistent:
        lines.append("NEEDS ATTENTION (likely delisted/renamed): %s" % ", ".join(persistent))
    summary = "\n".join(lines) + "\n"

    write_status(summary)

    print("\n" + "=" * 64)
    print(summary.strip())
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
