# Nifty 50 Stock Intelligence Platform

A self-updating Power BI dashboard covering all 50 Nifty index constituents, built on a Python ETL pipeline that runs itself every trading day through GitHub Actions.

The dashboard reads from this repository, and this repository updates itself. Nothing in the chain needs a person.

Current data freshness is recorded in [`data/metadata/last_updated.txt`](data/metadata/last_updated.txt), which the pipeline rewrites on every run.

---

## What it does

**Most stock dashboards assume you already understand finance. This one doesn't.**

Every risk metric comes with a plain-language reading next to it. `Beta 1.41` tells a first-time investor nothing. "Very high risk, moves 1.41x the market" tells them something. The thresholds behind those labels are calibrated to the Nifty 50 universe rather than lifted from a textbook, because the usual ">1.0 is good" convention comes from fund analysis, and individual stocks measured over twenty years sit well below it.

| Question | Answered on |
|---|---|
| What is this stock worth, and how has it moved? | **Stock Profiler** |
| Has it actually made money, and over what period? | **Returns** |
| How much volatility did it put you through to get there? | **Risk** |
| How does it sit against the rest of its sector? | **Sector Comparison** |

---

## Architecture

Four layers, and data moves through them in one direction.

**Sources.** Yahoo Finance via `yfinance`, covering the 50 constituents plus `^NSEI` for the index itself. A checked-in `nifty50_stocks.csv` holds the ticker list, company names and sector mapping, so the universe is version-controlled rather than hardcoded in a script.

**ETL, on a GitHub Actions runner.** `update_all_stocks.py` orchestrates three stages and decides whether the run was healthy enough to commit. `fetch_historical.py` pulls OHLCV incrementally, re-reading a 250-row trailing window so MA200 and RSI stay continuous across the join, and guards against partial bars from a session still in progress. `calculate_indicators.py` joins each stock's returns against the index to produce Beta, Sharpe, correlation, volatility and drawdown. `combine_data.py` folds the 51 per-ticker files into one long-format fact table.

**The repository is the storage layer.** Per-ticker CSVs live in `data/historical/`, the Power BI fact table in `data/combined/`, and the dimension tables plus the audit trail in `data/metadata/`. Everything is committed by a bot account on every run, so the data has the same history and diffability as the code.

**Power BI Service** reads three files straight from `raw.githubusercontent.com` over HTTPS. No gateway, no database, no local path. The semantic model is a star schema in import mode, with a Date dimension generated in DAX and a row-level security role.

**Two monitors sit off to the side** rather than in the data path. `check_coverage.py` runs inside the pipeline and records which tickers actually hold the newest session. `check_staleness.py` runs as a separate workflow three hours later, reads the committed state, and turns the run red if the data has fallen behind.

**Both schedulers are external to the code.** GitHub cron fires the pipeline twice on Tuesday through Saturday, at 09:00 and 20:00 IST, and Power BI's own scheduler refreshes at 10:30 and 11:30 IST. Two pipeline slots rather than one, for a reason worth stating: neither has to be reliable on its own, because the fetch is incremental and whichever run gets there first collects what it can.

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

MA20/50/200, RSI, MACD and Bollinger Bands get computed once a day in pandas and stored as columns. Doing that live in DAX would mean rolling-window math over 303k rows at query time, for every visual, for every user.

Beta, Sharpe, correlation, volatility and drawdown each need a statistical pass over a stock's full return history joined against the index's. That's a batch job. It doesn't belong at query time.

### One fact table, not 51 queries

`combine_data.py` merges the per-ticker files into a single long-format table with a `Ticker` column. One set of DAX measures then serves every ticker, and adding a 52nd stock needs no change in Power BI at all.

The alternative was doing this in Power Query, which would mean fetching 51 URLs on every scheduled refresh. Far more likely to time out in the Service.

### Star schema with a self-sizing Date dimension

Year, Month and Quarter are deliberately kept out of the fact table. In a star schema they belong to the Date dimension. Duplicating them across 303k rows wastes space and gives you two copies that can disagree.

The Date dimension generates itself in DAX from the fact table's own min and max, so it can't go stale. An earlier version hard-coded an end date and would have quietly stopped working in 2028.

### Failure classification, not a single try/except

Not every failure means the same thing, so the orchestrator sorts them into three buckets:

| | Meaning | Response |
|---|---|---|
| **Transient** | Network blip, vendor hiccup | Retry next run |
| **Persistent** | Same ticker failing 3+ runs | Probably renamed or delisted, flag for a human |
| **Systemic** | >30% of tickers failing | Upstream breakage. **Refuse to commit** |

The systemic case exits non-zero instead of publishing partial data. A pipeline that fails silently does more damage than one that doesn't run at all.

### Significant figures, not decimal places

Split- and dividend-adjusting a stock that has appreciated enormously drives its early prices into fractions of a paisa. ADANIENT's 2002 prices adjust to around `0.0501`. At that magnitude `round(2)` collapses `0.050081`, `0.050145` and `0.050049` all to `0.05`, which wipes out the intraday range and makes every daily return exactly zero.

Six significant figures hold the same *relative* precision at any magnitude. That's what return calculations actually depend on.

### One measurement window for every risk metric

Beta and correlation were always computed on an inner join against the benchmark, so they only ever saw overlapping dates. Sharpe, volatility and drawdown were not, and ran over each stock's entire history instead.

That was quietly comparing different things. It also broke on ADANIENT, whose split-adjusted 2003 prices sit at ₹0.58, where a single tick is a +744% daily return. Two rows like that inflated the standard deviation of the whole series and produced 162.78% annualised volatility against a 35.57% median. A card labelled "typical yearly swing" was reading 162.78.

All five now measure from the benchmark's start date, 2007-09-17, and `stock_metrics.csv` carries a `Risk_Window_Start` column so the window is auditable rather than something you'd have to read the code to discover. Returns are deliberately excluded from this, since they anchor on the most recent date and look back a fixed period, so early history can't reach them.

Restricting the window shifted the whole Sharpe distribution down, from a median near 0.47 to 0.36, which meant the plain-language thresholds had to be recalibrated too. They now sit on the quartile boundaries of the current distribution. Those labels make a claim about where a stock sits among its peers, so they're only true relative to the distribution they were tuned against.

### Bad-tick repair with a revert test

A bad tick jumps hugely and snaps straight back. A real event, like a split or a crash or a demerger, shifts the price level and it stays shifted.

So a row only gets repaired when three things hold: a large move from the previous day, a large move to the next day, and the two neighbours agreeing with each other. That third condition is what stops genuine crashes from being flattened.

Repaired rows carry a `Price_Repaired` flag, so the edit stays auditable instead of quietly rewriting history.

---

## Verification

Every bug this project surfaced produced a plausible-looking wrong number rather than an error. `MAX(Close)` with no date context returns a stock's all-time high, which looks perfectly reasonable on a card labelled "Current Price". Nothing throws, nothing logs, and the dashboard is wrong.

"It ran without an error" therefore isn't treated as evidence of anything. The repo carries its own test suite:

| Script | What it proves |
|---|---|
| `audit.py` | 44 checks, including recomputation from scratch and live comparison against Yahoo, so it can't be self-confirming |
| `verify_measures.py` | Computes in Python what every DAX measure should return, for independent comparison |
| `verify_data.py` | Indicator warm-up, RSI bounds, implausible daily moves |
| `verify_stage6.py` | Diffs before/after measure exports to prove a change was value-neutral |
| `check_coverage.py` | Whether every ticker actually holds the newest session |
| `check_staleness.py` | Whether the committed data is actually recent |

The metrics run carries its own guard too: any ticker whose annualised volatility exceeds three times the median gets flagged in the output. A Nifty 50 constituent running at several times the median is far more likely to be a data artefact than a real characteristic, and that is exactly how the ADANIENT contamination announced itself.

**Independence is the point.** The pipeline computes Beta as `Cov(x,y)/Var(y)`; the audit re-derives it as a least-squares regression slope. Two different methods agreeing is evidence. A method agreeing with itself is a tautology.

Three cross-checks reach outside the pipeline entirely:

- Sector betas behave the way finance theory predicts, with cyclicals averaging 1.07 and defensives 0.57. An inverted result would point at a broken join.
- IT sector 1Y returns average −15.4%, against an independently reported Nifty IT one-year decline of roughly 16% on the same trading date.
- The full GitHub → repo → Service → dashboard chain was verified by predicting a specific value change in advance and then watching it arrive at the far end. A refresh that succeeds isn't the same as a refresh that's correct.

---

## Automation

GitHub documents its `schedule` trigger as best-effort, and runs can be delayed under load. So delivery got measured over the first two weeks rather than assumed. Every run was delivered and no data was lost, but the 09:00 IST slot arrived anywhere between 09:34 and 21:05, and on four consecutive weekdays it landed between 13:18 and 14:21 — before the 15:30 market close.

That mattered more than a late dashboard. Asked for a session that has not closed, Yahoo returns a row with a populated volume and a **null close**, because the bar is not final. The fetch drops those rows as worthless, which is correct, so the ticker gains nothing and stays a session behind. It looked like certain tickers publishing late; it was actually whichever tickers were unfinalised at the moment the run happened, which is why the set changed every day.

The fix was a second scheduled slot at 20:00 IST rather than any change to the code. Measured on 5 September:

| Run | Skipped, already current | Unfinalised | Gained rows |
|---|---|---|---|
| 13:40 IST | 39 | 12 | 0 |
| 22:25 IST | 39 | 0 | 12 |

Same twelve tickers, same code, nine hours apart. All 51 level by the evening pass.

**No data has been lost.** The fetch is incremental, so a delayed run picks up every missing session in one pass. That was a design decision made before the delays showed up, and it's the reason they cost nothing.

Two monitors sit behind this:

- **Coverage check.** Catches tickers that quietly return no new bar. Fetch success and data currency are different things: a `200 OK` returning yesterday's rows is a successful call and a stale dataset.
- **Staleness alert.** A separate workflow that reads the committed state and turns red if the data falls more than three weekdays behind.

---

## Row-level security

A `Sector Analyst - IT` role restricts the model to the five IT constituents while leaving `NIFTY50_INDEX` visible, so benchmark comparisons still work under the role. Without that exemption, half the report blanks out.

Verification happened in the published model, not just in Desktop. Under Service "Test as role" the ticker slicer narrows to five entries and ticker-level exports contain no non-IT rows. Enforcement against a second signed-in identity wasn't testable, since no second account existed in the tenant.

---

## Known limitations

These affect how the numbers should be read.

**Structural breaks aren't adjusted for.** TMPV (2025 demerger), ADANIENT (2015 demerger) and BAJAJFINSV (2008 demerger from Bajaj Auto) all show large single-day declines that reflect corporate actions rather than market moves. These feed `Max_Drawdown_Pct`, so a "worst ever fall" figure can include a day the company restructured rather than a day the market sold off. Separating the two needs a corporate-actions feed this project doesn't have.

**A run can encounter unfinalised bars.** Yahoo does not finalise every ticker's daily close at the same time, so a run landing early enough in the day will find some tickers still carrying a null close and leave them a session behind. The evening slot exists to collect them, and the coverage check reports the state on every run. It is self-correcting rather than permanent, but between the two slots the dashboard can briefly hold a mixed set of sessions.

**Sector coverage is partial by design.** The Nifty IT index has ten constituents. This project covers the five that are also in the Nifty 50, so "IT sector" here means the IT stocks within the Nifty 50.

---

## How this was built

Nine stages, each gated on verifying the previous one. Twenty-one bugs found and fixed along the way, and almost none of them threw an error — a hard-coded Date table end that would have expired in 2028, a skip rule that quietly stopped fetching tickers that already held yesterday's bar, an outlier statistic that made one stock look four times more volatile than the index, and a log line that printed the same words for four different outcomes.

The pattern repeated often enough to become the working rule: a wrong number that looks reasonable is more dangerous than a crash, because nothing tells you to look. Every stage therefore ends with a check against something independent — a second calculation, an external source, or a value predicted in advance and then observed.

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
  daily_update.yml          09:00 and 20:00 IST, Tue-Sat
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

Python (pandas, numpy, yfinance) · GitHub Actions · Power BI Desktop and Service · DAX · Power Query M · Git
