"""
Staleness alert - verifies that the data in the REPOSITORY is current.

This is deliberately separate from the pipeline. The pipeline reports whether
a fetch succeeded; this reports whether the committed data is actually recent.
Those are different questions, and the 27 August incident showed the first can
be green while the second is not.

The principle: you cannot make an unreliable scheduler reliable, so detect its
failures instead of trying to prevent them.

Exits non-zero when the newest data point is too old, which turns the workflow
red and triggers GitHub's failure notification.
"""

import os
import re
import sys
from datetime import datetime, timedelta, timezone

LAST_UPDATED = os.path.join("data", "metadata", "last_updated.txt")

# Weekdays of tolerance before alerting. Deliberately loose: this count
# ignores exchange holidays, so a tight threshold would cry wolf every
# Diwali and train you to ignore it. Same reasoning as check_freshness().
MAX_WEEKDAYS_BEHIND = 3

IST = timezone(timedelta(hours=5, minutes=30))


def read_newest_data_point(path):
    """Extract the data date from last_updated.txt, or return None."""
    if not os.path.exists(path):
        return None, path + " does not exist"

    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()

    match = re.search(r"^Newest data point:\s*(\d{4}-\d{2}-\d{2})\s*$",
                      text, re.MULTILINE)
    if not match:
        return None, "could not find a 'Newest data point:' line in " + path

    try:
        parsed = datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None, "unparseable date: " + match.group(1)

    return parsed, None


def weekdays_between(start, end):
    """Count weekdays strictly after `start`, up to and including `end`."""
    if end <= start:
        return 0
    count = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5:      # Mon=0 .. Fri=4
            count += 1
        cursor += timedelta(days=1)
    return count


def main():
    today_ist = datetime.now(IST).date()

    newest, error = read_newest_data_point(LAST_UPDATED)
    if newest is None:
        print("STALENESS CHECK FAILED: " + str(error))
        print("The audit trail is missing or malformed, which is itself a")
        print("problem worth investigating - it means a run wrote nothing")
        print("readable, or the file was corrupted.")
        return 1

    behind = weekdays_between(newest, today_ist)

    print("Today (IST):        " + str(today_ist))
    print("Newest data point:  " + str(newest))
    print("Weekdays behind:    " + str(behind))
    print("Threshold:          " + str(MAX_WEEKDAYS_BEHIND))
    print()

    if behind > MAX_WEEKDAYS_BEHIND:
        print("STALE. The repository data is older than expected.")
        print()
        print("Likely causes, in order of probability:")
        print("  1. GitHub's scheduler dropped one or more runs")
        print("     -> check the Actions tab for missing runs")
        print("  2. Runs fired but committed nothing")
        print("     -> Yahoo returned no new bars, or the 30% systemic")
        print("        failure rule refused the commit")
        print("  3. A long exchange holiday period")
        print("     -> benign; no action needed")
        print()
        print("Note: a manual workflow_dispatch of the daily update will")
        print("collect every missing session in one pass. The pipeline is")
        print("incremental, so this is self-healing.")
        return 1

    print("OK. Repository data is current within tolerance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
