"""
check_coverage.py
-----------------
Coverage check - does every ticker actually have the newest session?

WHY THIS EXISTS
---------------
On 27 August 2026 the pipeline reported "51 succeeded, 0 failed" while ten
tickers had silently returned no new bar. Twenty percent of the index was a
session behind, under the 30% systemic threshold, so the orchestrator
committed happily.

The lesson: FETCH SUCCESS IS NOT DATA CURRENCY. yfinance returning 200 OK
with the same rows it returned yesterday is a successful call and a stale
dataset. Nothing in the existing failure classification can see that,
because nothing failed.

check_freshness() in the orchestrator catches the case where the WHOLE
dataset stalls - it looks at the newest date across all files. This catches
the quieter case where most tickers advance and a handful do not, which the
max-across-all-files calculation hides completely.

HOW THE EXPECTED SESSION IS DEFINED - AND WHY NOT max()
-------------------------------------------------------
The first version of this script used max(date across all tickers) as the
expected session. That was wrong, and the run of 27 August 2026 at 19:57 IST
proved it within a day.

That run happened after the 15:30 close, so filter #3 in the fetch step no
longer suppressed the in-progress session. Nine tickers - the same nine that
had been lagging - carry Yahoo's same-day bar earlier than the rest. They
picked up 27 August. The other 42, which Yahoo had not yet finalised,
correctly stayed at 26 August.

max() then declared the expected session to be 27 August and reported 42 of
51 tickers "behind" - a false alarm covering 82% of the index, on data that
was entirely correct. Left alone it would have turned the workflow red in
three runs and trained the reader to ignore it. Exactly the cry-wolf failure
the staleness threshold was tuned to avoid.

max() handles laggards well and leaders terribly. It is a one-sided statistic
being asked a two-sided question.

The MEDIAN is robust in both directions. A minority ahead cannot drag the
expectation forward; a minority behind cannot drag it back. Tickers ahead of
the median are reported separately and never counted as lag - being ahead is
not a fault.

    26 Aug run:   42 at 26th,  9 at 25th   -> median 26th ->  9 behind
    27 Aug run:    9 at 27th, 42 at 26th   -> median 26th ->  0 behind,
                                                              9 ahead

The expected session is still DERIVED from the data, never TODAY(). Today is
frequently not a trading day - weekends, exchange holidays and the T+1
design all guarantee it. Same reasoning as bug 6.16: a date the code computes
from what is actually there cannot go stale; a date someone typed in can.

DESIGN: WHY THIS DOES NOT BLOCK THE COMMIT
------------------------------------------
Refusing to commit because some tickers lag would discard the majority's
good data to avoid publishing a stale minority - and the lag is
self-healing. The nine tickers behind on 26 August gained +2 rows and caught
up on their own.

So this mirrors the orchestrator's own transient/persistent split:

  lagging this run          -> record it, commit anyway
  lagging 3+ runs running   -> exit non-zero, because that is no longer
                               vendor lag, it is a ticker that has stopped
                               updating (the TATAMOTORS failure mode, but
                               silent)

In the workflow the enforcing call runs AFTER the commit step, so a red run
never costs you the data.

USAGE
-----
    python scripts/check_coverage.py
        Computes coverage, appends a line to last_updated.txt, updates
        coverage_state.json. ALWAYS exits 0.

    python scripts/check_coverage.py --enforce
        Reads coverage_state.json only. Exits 1 if any ticker has lagged
        for PERSISTENT_LAG_THRESHOLD consecutive runs. Recomputes nothing.

Author: Atharva Phalak
"""

import os
import sys
import json
import glob

import pandas as pd

METADATA_DIR = os.path.join("data", "metadata")
HISTORICAL_DIR = os.path.join("data", "historical")
STATE_FILE = os.path.join(METADATA_DIR, "coverage_state.json")
STATUS_FILE = os.path.join(METADATA_DIR, "last_updated.txt")

# Consecutive runs a ticker may lag before it is treated as broken rather
# than merely slow. Matches PERSISTENT_THRESHOLD in the orchestrator on
# purpose - one concept, one number, so the two checks do not disagree
# about what "persistent" means.
PERSISTENT_LAG_THRESHOLD = 3


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"consecutive_lag": {}}
    try:
        with open(STATE_FILE) as fh:
            data = json.load(fh)
        if "consecutive_lag" not in data:
            data["consecutive_lag"] = {}
        return data
    except (ValueError, OSError):
        # A corrupt state file must not take the pipeline down. Starting
        # from zero costs at most a few runs of detection latency.
        return {"consecutive_lag": {}}


def save_state(state):
    if not os.path.exists(METADATA_DIR):
        os.makedirs(METADATA_DIR)
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)


def latest_date_per_ticker():
    """
    Newest date present in each per-ticker CSV.

    Reads only the Date column. Reading 51 full files would pull ~45 MB
    through pandas for a question that needs one column.
    """
    results = {}
    paths = sorted(glob.glob(os.path.join(HISTORICAL_DIR, "*.csv")))

    for path in paths:
        ticker = os.path.splitext(os.path.basename(path))[0]
        try:
            dates = pd.read_csv(path, usecols=["Date"])["Date"]
            if dates.empty:
                results[ticker] = None
                continue
            results[ticker] = pd.to_datetime(dates).max().date()
        except Exception as exc:
            print("  WARNING: could not read %s (%s)" % (path, exc))
            results[ticker] = None

    return results


def median_date(dates):
    """
    Middle value of a list of dates.

    For an even count this takes the LOWER of the two middle values rather
    than averaging them. Averaging two dates can produce a day the exchange
    never traded, and the expected session must be a real session.
    Rounding down is the conservative direction: it treats fewer tickers as
    behind, and a false alarm is more costly here than a missed one, since
    a persistent stall will still be caught on the following run.
    """
    ordered = sorted(dates)
    return ordered[(len(ordered) - 1) // 2]


def report():
    """Compute coverage, update state, append to the audit trail."""
    per_ticker = latest_date_per_ticker()

    if not per_ticker:
        print("No files found in %s - nothing to check." % HISTORICAL_DIR)
        return 0

    dated = dict((t, d) for t, d in per_ticker.items() if d is not None)
    unreadable = sorted(t for t, d in per_ticker.items() if d is None)

    if not dated:
        print("No readable dates in any file - cannot assess coverage.")
        return 0

    expected = median_date(list(dated.values()))
    newest_anywhere = max(dated.values())

    behind = sorted(t for t, d in dated.items() if d < expected)
    ahead = sorted(t for t, d in dated.items() if d > expected)
    current = len(dated) - len(behind)
    total = len(dated)
    pct_behind = (100.0 * len(behind) / total) if total else 0.0

    print("=" * 64)
    print("COVERAGE CHECK")
    print("=" * 64)
    print("Expected session (median) : %s" % expected)
    print("Newest bar anywhere       : %s" % newest_anywhere)
    print("Tickers at or past it     : %d of %d" % (current, total))
    print("Tickers behind            : %d (%.1f%%)" % (len(behind), pct_behind))
    if ahead:
        print("Tickers ahead             : %d" % len(ahead))
    if unreadable:
        print("Unreadable files          : %s" % ", ".join(unreadable))
    print()

    state = load_state()
    counts = state.get("consecutive_lag", {})

    for ticker in dated:
        if ticker in behind:
            counts[ticker] = counts.get(ticker, 0) + 1
        elif ticker in counts:
            del counts[ticker]

    state["consecutive_lag"] = counts
    state["last_expected_date"] = str(expected)
    save_state(state)

    persistent = sorted(t for t, n in counts.items()
                        if n >= PERSISTENT_LAG_THRESHOLD)

    if ahead:
        print("Ahead of the expected session (NOT a fault):")
        for ticker in ahead:
            print("  %-16s %s" % (ticker, dated[ticker]))
        print()
        print("Some tickers carry Yahoo's same-day bar sooner than others.")
        print("This is a vendor cadence difference, not a data problem.")
        print()

    if behind:
        print("Behind by ticker:")
        for ticker in behind:
            print("  %-16s %s  (%d consecutive run%s)"
                  % (ticker, dated[ticker], counts.get(ticker, 1),
                     "" if counts.get(ticker, 1) == 1 else "s"))
        print()
        print("A ticker one session behind is usually vendor lag, not a")
        print("fault. Yahoo finalises Indian daily bars unevenly and these")
        print("normally catch up on the next run, gaining +2 rows.")
        print()

    if persistent:
        print("!" * 64)
        print("PERSISTENT LAG - these tickers have been behind for %d or"
              % PERSISTENT_LAG_THRESHOLD)
        print("more consecutive runs:")
        for ticker in persistent:
            print("  %s (%d runs, stuck at %s)"
                  % (ticker, counts[ticker], dated[ticker]))
        print()
        print("That is no longer lag. Check whether the NSE symbol changed,")
        print("the way TATAMOTORS did after the October 2025 demerger.")
        print("!" * 64)
    elif not behind:
        print("All tickers carry the expected session or better.")

    append_to_status(expected, newest_anywhere, current, total,
                     behind, ahead, persistent)
    return 0


def append_to_status(expected, newest_anywhere, current, total,
                     behind, ahead, persistent):
    """
    Add coverage lines to last_updated.txt.

    Appending rather than rewriting: the orchestrator owns that file and
    has already written its summary. This adds to the audit trail instead
    of competing for it. The staleness alert prints this file into its
    workflow summary, so these lines surface there too.
    """
    if not os.path.exists(STATUS_FILE):
        return

    lines = ["Expected session (median): %s" % expected,
             "Tickers at or past expected session: %d of %d" % (current, total)]
    if ahead:
        lines.append("Tickers ahead of expected session (%s): %s"
                     % (newest_anywhere, ", ".join(ahead)))
    if behind:
        lines.append("Tickers behind expected session (%s): %s"
                     % (expected, ", ".join(behind)))
    if persistent:
        lines.append("PERSISTENT LAG - not updating: %s" % ", ".join(persistent))

    try:
        with open(STATUS_FILE, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print("  WARNING: could not append to %s (%s)" % (STATUS_FILE, exc))


def enforce():
    """
    Read state and fail if any ticker is persistently behind.

    Recomputes nothing, so it cannot double-increment the counters. Run
    this AFTER the commit step: the data should be published either way,
    and a red run is a notification, not a gate.
    """
    state = load_state()
    counts = state.get("consecutive_lag", {})
    persistent = sorted(t for t, n in counts.items()
                        if n >= PERSISTENT_LAG_THRESHOLD)

    if not persistent:
        lagging = sorted(counts.items())
        if lagging:
            print("Tickers currently behind (not yet persistent):")
            for ticker, n in lagging:
                print("  %-16s %d consecutive run%s"
                      % (ticker, n, "" if n == 1 else "s"))
        else:
            print("No tickers behind. Coverage is complete.")
        return 0

    print("!" * 64)
    print("COVERAGE FAILURE")
    print("!" * 64)
    for ticker in persistent:
        print("  %s has been behind for %d consecutive runs"
              % (ticker, counts[ticker]))
    print()
    print("The data for this run WAS committed - the other tickers are")
    print("fine and withholding them would help nobody. This red run is")
    print("the notification, not a rollback.")
    return 1


def main():
    if "--enforce" in sys.argv:
        return enforce()
    return report()


if __name__ == "__main__":
    sys.exit(main())
