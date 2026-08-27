"""
verify_stage6.py
================
Compares two Power BI measure exports taken before and after the Stage 6
changes (Power Query step renames, QuoteStyle change, Date table rewrite).

The point: those changes are all supposed to be value-neutral. Renaming a step
cannot change a number. Changing QuoteStyle should not change a number, because
no field currently contains a quote. Rebuilding the Date table SHOULD NOT change
a number, because the new range still covers all the data.

"Should not" is a hypothesis. This script tests it.

Every bug in this project produced a plausible-looking wrong number rather than
an error, so "it refreshed fine" proves nothing. This compares actual values,
ticker by ticker, measure by measure.

USAGE
-----
    py scripts/verify_stage6.py baseline_before.csv baseline_after.csv

Put both CSVs in the project root (or pass full paths).

EXIT CODES
----------
    0   every value matched within tolerance
    1   at least one value changed, went blank, or a row/column vanished
"""

import sys
import pandas as pd

# ---------------------------------------------------------------------------
# Tolerances
# ---------------------------------------------------------------------------
# Floating point arithmetic is not exactly reproducible. Power BI can return
# 35.107542997649745 one run and 35.10754299764974 the next purely from
# evaluation order. A relative tolerance absorbs that without hiding a real
# change: a genuine bug moves a number by orders of magnitude, not by the
# fifteenth decimal place.
#
# RELATIVE is used for large values, ABSOLUTE for values near zero (where a
# relative comparison would explode).
RELATIVE_TOL = 1e-9
ABSOLUTE_TOL = 1e-9

KEY_COLUMN = "NSE_Ticker"


def load(path):
    """Read a Power BI export.

    encoding='utf-8-sig' matters: Power BI writes a UTF-8 byte order mark, and
    without this the first column name becomes '\ufeffNSE_Ticker' and the merge
    silently finds nothing.
    """
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    if KEY_COLUMN not in df.columns:
        sys.exit(f"FAIL  {path} has no '{KEY_COLUMN}' column. Found: {list(df.columns)}")
    return df.set_index(KEY_COLUMN).sort_index()


def compare_numeric(before, after, col):
    """Return a DataFrame of rows where a numeric value moved beyond tolerance."""
    b = pd.to_numeric(before[col], errors="coerce")
    a = pd.to_numeric(after[col], errors="coerce")

    both_blank = b.isna() & a.isna()
    diff = (b - a).abs()
    scale = b.abs().clip(lower=1.0)
    moved = (diff > (RELATIVE_TOL * scale + ABSOLUTE_TOL)) & ~both_blank

    # A value that existed before and is blank now is the single most important
    # case to catch. It is what a dropped column looks like from the outside.
    vanished = b.notna() & a.isna()
    appeared = b.isna() & a.notna()

    flagged = moved | vanished | appeared
    if not flagged.any():
        return None

    out = pd.DataFrame({"before": b[flagged], "after": a[flagged]})
    out["abs_diff"] = (out["before"] - out["after"]).abs()
    out["note"] = ""
    out.loc[vanished[flagged], "note"] = "WENT BLANK"
    out.loc[appeared[flagged], "note"] = "was blank, now has a value"
    return out


def compare_text(before, after, col):
    """Return a DataFrame of rows where a text value changed at all."""
    b = before[col].astype("string")
    a = after[col].astype("string")
    both_blank = b.isna() & a.isna()
    changed = (b != a) & ~both_blank
    if not changed.any():
        return None
    return pd.DataFrame({"before": b[changed], "after": a[changed]})


def main():
    if len(sys.argv) != 3:
        sys.exit("USAGE: py scripts/verify_stage6.py <before.csv> <after.csv>")

    before_path, after_path = sys.argv[1], sys.argv[2]
    before = load(before_path)
    after = load(after_path)

    print("=" * 68)
    print("STAGE 6 VERIFICATION - measure values before vs after")
    print("=" * 68)
    print(f"before : {before_path}   {before.shape[0]} rows x {before.shape[1]} measures")
    print(f"after  : {after_path}   {after.shape[0]} rows x {after.shape[1]} measures")
    print()

    failures = 0

    # --- structural checks first -------------------------------------------
    # A missing ticker or a missing measure is a bigger problem than a changed
    # value, and would otherwise be invisible in a column-by-column loop.
    lost_rows = before.index.difference(after.index)
    new_rows = after.index.difference(before.index)
    lost_cols = [c for c in before.columns if c not in after.columns]
    new_cols = [c for c in after.columns if c not in before.columns]

    if len(lost_rows):
        print(f"FAIL  {len(lost_rows)} ticker(s) disappeared: {list(lost_rows)}")
        failures += 1
    if len(new_rows):
        print(f"WARN  {len(new_rows)} new ticker(s) appeared: {list(new_rows)}")
    if lost_cols:
        print(f"FAIL  measure(s) missing from the after export: {lost_cols}")
        failures += 1
    if new_cols:
        print(f"WARN  measure(s) only in the after export: {new_cols}")

    shared_cols = [c for c in before.columns if c in after.columns]
    shared_rows = before.index.intersection(after.index)
    before = before.loc[shared_rows]
    after = after.loc[shared_rows]

    # --- value comparison --------------------------------------------------
    for col in shared_cols:
        # Decide numeric vs text by whether the BEFORE column parses as numbers.
        # Measures like 'Beta Percentile' and 'Rank In Sector' return sentences,
        # and comparing those as floats would turn every one into a silent NaN
        # match - the exact kind of false pass this script exists to prevent.
        parsed = pd.to_numeric(before[col], errors="coerce")
        is_numeric = parsed.notna().sum() >= max(1, int(0.5 * len(before)))

        result = compare_numeric(before, after, col) if is_numeric \
            else compare_text(before, after, col)

        if result is None:
            kind = "numeric" if is_numeric else "text"
            print(f"  OK    {col:<32} {len(before):>3} values identical ({kind})")
        else:
            failures += 1
            print()
            print(f"  FAIL  {col}  -  {len(result)} value(s) changed")
            print(result.to_string())
            print()

    print()
    print("=" * 68)
    if failures == 0:
        print("PASS - every measure returned identical values after the changes.")
        print()
        print("This is the expected result. The Stage 6 edits were supposed to")
        print("be value-neutral, and now that is demonstrated rather than assumed.")
        sys.exit(0)
    else:
        print(f"FAIL - {failures} problem(s) found. DO NOT proceed.")
        print()
        print("Something that should not have changed a number, changed a number.")
        print("Undo the last edit and re-export before going further.")
        sys.exit(1)


if __name__ == "__main__":
    main()
