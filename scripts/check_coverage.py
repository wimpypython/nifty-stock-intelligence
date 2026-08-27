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

DESIGN: WHY THIS DOES NOT BLOCK THE COMMIT
------------------------------------------
Refusing to commit because 10 of 51 tickers lag would discard 41 tickers of
good data to avoid publishing 10 stale ones - and the lag is self-healing,
so it would do that repeatedly. On 28 August five of those tickers gained
+2 rows and caught up on their own.

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

    # The expected date is the newest any ticker reached. Deliberately NOT
    # "today": exchange holidays, weekends and the T+1 design all mean
    # today is often not a trading day. The market itself defines the
    # newest session, and at least one ticker will have it.
    expected = max(dated.values())

    behind = sorted(t for t, d in dated.items() if d < expected)
    current = len(dated) - len(behind)
    total = len(dated)
    pct_behind = (100.0 * len(behind) / total) if total else 0.0

    print("=" * 64)
    print("COVERAGE CHECK")
    print("=" * 64)
    print("Newest session in dataset : %s" % expected)
    print("Tickers current           : %d of %d" % (current, total))
    print("Tickers behind            : %d (%.1f%%)" % (len(behind), pct_behind))
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
        print("All tickers carry the newest session.")

    append_to_status(expected, current, total, behind, persistent)
    return 0


def append_to_status(expected, current, total, behind, persistent):
    """
    Add coverage lines to last_updated.txt.

    Appending rather than rewriting: the orchestrator owns that file and
    has already written its summary. This adds to the audit trail instead
    of competing for it. The staleness alert prints this file into its
    workflow summary, so these lines surface there too.
    """
    if not os.path.exists(STATUS_FILE):
        return

    lines = ["Tickers with newest session: %d of %d" % (current, total)]
    if behind:
        lines.append("Tickers behind newest session (%s): %s"
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
