# Nifty 50 Stock Intelligence Platform

A self-updating Power BI dashboard covering all 50 Nifty index constituents, built on
a Python ETL pipeline that runs itself every trading day through GitHub Actions.

The dashboard reads from this repository, and this repository updates itself. Nothing
in the chain needs a person.

Current data freshness is recorded in
[`data/metadata/last_updated.txt`](data/metadata/last_updated.txt), which the pipeline
rewrites on every run.

Topics: power-bi, dax, etl, python, github-actions, data-engineering, nifty50, yfinance, data-pipeline, star-schema

---

## What it does

Most stock dashboards assume you already understand finance. This one doesn't.

Every risk metric comes with a plain-language reading next to it. `Beta 1.41` tells a
first-time investor nothing. "Very high risk, moves 1.41x the market" tells them
something. The thresholds behind those labels are calibrated to the Nifty 50 universe
rather than lifted from a textbook, because the usual ">1.0 is good" convention comes
from fund analysis, and individual stocks measured over twenty years sit well below it.

| Question | Answered on |
|---|---|
| What is this stock worth, and how has it moved? | **Stock Profiler** |
| Has it actually made money, and over what period? | **Returns** |
| How much volatility did it put you through to get there? | **Risk** |
| How does it sit against the rest of its sector? | **Sector Comparison** |

---

## Architecture

Four layers, and data moves through them in one direction.

**Sources.** Yahoo Finance via `yfinance`, covering the 50 constituents plus `^NSEI`
for the index itself. A checked-in `nifty50_stocks.csv` holds the ticker list, company
names and sector mapping, so the universe is version-controlled rather than hardcoded
in a script.

**ETL, on a GitHub Actions runner.** `update_all_stocks.py` orchestrates three stages
and decides whether the run was healthy enough to commit. `fetch_historical.py` pulls
OHLCV incrementally, re-reading a 250-row trailing window so MA200 and RSI stay
continuous across the join, and guards against partial bars from a session still in
progress. `calculate_indicators.py` joins each stock's returns against the index to
produce Beta, Sharpe, correlation, volatility and drawdown. `combine_data.py` folds
the 51 per-ticker files into one long-format fact table.

**The repository is the storage layer.** Per-ticker CSVs live in `data/historical/`,
the Power BI fact table in `data/combined/`, and the dimension tables plus the audit
trail in `data/metadata/`. Everything is committed by a bot account on every run, so
the data has the same history and diffability as the code.

**Power BI Service** reads three files straight from `raw.githubusercontent.com` over
HTTPS. No gateway, no database, no local path. The semantic model is a star schema in
import mode, with a Date dimension generated in DAX and a row-level security role.

Two monitors sit off to the side rather than in the data path. `check_coverage.py`
runs inside the pipeline and records which tickers actually hold the newest session.
`check_staleness.py` runs as a separate workflow three hours later, reads the
committed state, and turns the run red if the data has fallen behind.

Both schedulers are external to the code. A GitHub cron fires the pipeline at 09:00
IST on Tuesday through Saturday, collecting the previous session. Power BI's own
scheduler refreshes at 10:30 and 11:30 IST.

```
Yahoo Finance ──► fetch_historical ──► calculate_indicators ──► combine_data
                         │                      │                     │
                         ▼                      ▼                     ▼
                  data/historical/       data/metadata/        data/combined/
                         │                      │                     │
                         │                      └──────────┬──────────┘
                         ▼                                 ▼
                  check_coverage                  raw.githubusercontent.com
                         │                                 │
                         ▼                                 ▼
                    audit trail ──► check_staleness   Power BI Service
```

| | |
|---|---|
| Tickers | 51 (50 constituents + NIFTY50_INDEX benchmark) |
| Rows | 303,685 |
| History | 2000-01-03 to present |
| Fact table | 44.3 MB |
| DAX measures | 40+ |
| Report pages | 4: Stock Profiler, Returns, Risk, Sector Comparison |
| Pipeline runtime | ~30–50 seconds |
| Refresh duration | ~20 seconds |

---

## Engineering decisions

### Indicators in Python, not DAX

MA20/50/200, RSI, MACD and Bollinger Bands get computed once a day in pandas and
stored as columns. Doing that live in DAX would mean rolling-window math over 303k
rows at query time, for every visual, for every user.

Beta, Sharpe, correlation, volatility and drawdown each need a statistical pass over
a stock's full return history joined against the index's. That's a batch job. It
doesn't belong at query time.

### One fact table, not 51 queries

`combine_data.py` merges the per-ticker files into a single long-format table with a
`Ticker` column. One set of DAX measures then serves every ticker, and adding a 52nd
stock needs no change in Power BI at all.

The alternative was doing this in Power Query, which would mean fetching 51 URLs on
every scheduled refresh. Far more likely to time out in the Service.

### Star schema with a self-sizing Date dimension

Year, Month and Quarter are deliberately kept out of the fact table. In a star schema
they belong to the Date dimension. Duplicating them across 303k rows wastes space and
gives you two copies that can disagree.

The Date dimension generates itself in DAX from the fact table's own min and max, so
it can't go stale. An earlier version hard-coded an end date and would have quietly
stopped working in 2028.

### Failure classification, not a single try/except

Not every failure means the same thing, so the orchestrator sorts them into three
buckets:

| | Meaning | Response |
|---|---|---|
| **Transient** | Network blip, vendor hiccup | Retry next run |
| **Persistent** | Same ticker failing 3+ runs | Probably renamed or delisted, flag for a human |
| **Systemic** | >30% of tickers failing | Upstream breakage. **Refuse to commit** |

The systemic case exits non-zero instead of publishing partial data. A pipeline that
fails silently does more damage than one that doesn't run at all.

### Significant figures, not decimal places

Split- and dividend-adjusting a stock that has appreciated enormously drives its early
prices into fractions of a paisa. ADANIENT's 2002 prices adjust to around `0.0501`.
At that magnitude `round(2)` collapses `0.050081`, `0.050145` and `0.050049` all to
`0.05`, which wipes out the intraday range and makes every daily return exactly zero.

Six significant figures hold the same *relative* precision at any magnitude. That's
what return calculations actually depend on.

### Bad-tick repair with a revert test

A bad tick jumps hugely and snaps straight back. A real event, like a split or a crash
or a demerger, shifts the price level and it stays shifted.

So a row only gets repaired when three things hold: a large move from the previous
day, a large move to the next day, and the two neighbours agreeing with each other.
That third condition is what stops genuine crashes from being flattened.

Repaired rows carry a `Price_Repaired` flag, so the edit stays auditable instead of
quietly rewriting history.

---

## Verification

Every bug this project surfaced produced a plausible-looking wrong number rather than
an error. `MAX(Close)` with no date context returns a stock's all-time high, which
looks perfectly reasonable on a card labelled "Current Price". Nothing throws, nothing
logs, and the dashboard is wrong.

"It ran without an error" therefore isn't treated as evidence of anything. The repo
carries its own test suite:

| Script | What it proves |
|---|---|
| `audit.py` | 44 checks, including recomputation from scratch and live comparison against Yahoo, so it can't be self-confirming |
| `verify_measures.py` | Computes in Python what every DAX measure should return, for independent comparison |
| `verify_data.py` | Indicator warm-up, RSI bounds, implausible daily moves |
| `verify_stage6.py` | Diffs before/after measure exports to prove a change was value-neutral |
| `check_coverage.py` | Whether every ticker actually holds the newest session |
| `check_staleness.py` | Whether the committed data is actually recent |

Independence is the point. The pipeline computes Beta as `Cov(x,y)/Var(y)`; the audit
re-derives it as a least-squares regression slope. Two different methods agreeing is
evidence. A method agreeing with itself is a tautology.

Three cross-checks reach outside the pipeline entirely:

- Sector betas behave the way finance theory predicts, with cyclicals averaging 1.07
  and defensives 0.57. An inverted result would point at a broken join.
- IT sector 1Y returns average −15.4%, against an independently reported Nifty IT
  one-year decline of roughly 16% on the same trading date.
- The full GitHub → repo → Service → dashboard chain was verified by predicting a
  specific value change in advance and then watching it arrive at the far end. A
  refresh that succeeds isn't the same as a refresh that's correct.

---

## Automation

GitHub documents its `schedule` trigger as best-effort, and runs can be delayed under
load. So delivery got measured over the first week rather than assumed:

| | Delivered | Within expected window |
|---|---|---|
| Pipeline (GitHub Actions) | 6 of 6 | 3, the rest delayed |
| Power BI scheduled refresh | 4 of 4 | 4 |

No data has been lost. The fetch is incremental, so a delayed run picks up every
missing session in one pass. That was a design decision made before the delays showed
up, and it's the reason they cost nothing.

Two monitors sit behind this:

- **Coverage check.** Catches tickers that quietly return no new bar. Fetch success
  and data currency are different things: a `200 OK` returning yesterday's rows is a
  successful call and a stale dataset.
- **Staleness alert.** A separate workflow that reads the committed state and turns
  red if the data falls more than three weekdays behind.

---

## Row-level security

A `Sector Analyst - IT` role restricts the model to the five IT constituents while
leaving `NIFTY50_INDEX` visible, so benchmark comparisons still work under the role.
Without that exemption, half the report blanks out.

Verification happened in the published model, not just in Desktop. Under Service "Test
as role" the ticker slicer narrows to five entries and ticker-level exports contain no
non-IT rows. Enforcement against a second signed-in identity wasn't testable, since no
second account existed in the tenant.

---

## Known limitations

These affect how the numbers should be read.

**ADANIENT's Sharpe and volatility are inflated.** Both get computed over the full
return history without the index join that Beta and correlation use, so pre-2007
split-adjusted prices distort them. One ticker out of fifty. Beta, correlation and
returns are unaffected. Queued for the next iteration.

**Not every ticker carries the same session on a given day.** Nine of them become
available to the pipeline later than the other 42, so the dashboard can sit at T+1 for
most stocks and T+2 for those nine. The coverage check reports it on every run, and
the following run resolves it.

**Structural breaks aren't adjusted for.** TMPV (2025 demerger) and ADANIENT (2015
demerger) both show large single-day declines that reflect corporate actions rather
than market moves. These feed `Max_Drawdown_Pct`.

**Sector coverage is partial by design.** The Nifty IT index has ten constituents.
This project covers the five that are also in the Nifty 50, so "IT sector" here means
the IT stocks within the Nifty 50.

---

## Repository layout

```
scripts/
  fetch_historical.py       incremental OHLCV fetch + indicators
  calculate_indicators.py   cross-sectional risk metrics
  combine_data.py           per-ticker files -> one fact table
  update_all_stocks.py      orchestrator + failure classification
  check_coverage.py         per-ticker session coverage
  check_staleness.py        committed-data freshness alert
  audit.py                  44-check pipeline audit
  verify_data.py            post-fetch sanity checks
  verify_measures.py        expected DAX values, computed in Python
  verify_stage6.py          before/after measure diff

data/
  historical/               50 stock CSVs + NIFTY50_INDEX.csv
  combined/                 stock_prices.csv  (Power BI fact table)
  metadata/                 stock_metrics.csv, nifty50_stocks.csv,
                            last_updated.txt, coverage_state.json

.github/workflows/
  daily_update.yml          09:00 IST, Tue-Sat
  staleness_alert.yml       12:00 IST, Tue-Sat
```

---

## Running it locally

Windows, Python 3.12:

```
pip install -r requirements.txt

py scripts\fetch_historical.py            # full fetch on first run
py scripts\calculate_indicators.py
py scripts\combine_data.py

py scripts\audit.py                       # verify before trusting anything
```

Single ticker:

```
py scripts\fetch_historical.py --only RELIANCE
py scripts\fetch_historical.py --only "M&M"
```

> Quote `"M&M"` on Windows. `cmd.exe` reads a bare `&` as a command separator.

Local runs rewrite tracked files under `data/`. Discard them before pulling:

```
git checkout -- data/
```

---

## Tech

Python (pandas, numpy, yfinance) · GitHub Actions · Power BI Desktop and Service ·
DAX · Power Query M · Git
