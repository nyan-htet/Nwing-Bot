# Nwing-Bot --- Scanner Business Logic & Architecture

## 1. Purpose

Nwing-Bot is designed as a **progressive stock-selection and
trade-signal pipeline**.

The system starts with a large universe of stocks and ETFs and
progressively reduces it to a manageable set of technically interesting
candidates.

The core principle is:

> **Do not spend expensive API calls or detailed analysis on every
> symbol at the beginning. Filter progressively, then spend more
> computation and intelligence only on survivors.**

The intended flow is:

``` text
3,000+ stocks / ETFs
        │
        ▼
Stage 1 — Universe / Quality Funnel
        │
        ▼
~500 stocks + ETFs
        │
        ▼
Stage 2 — Daily Trend / Regime
        │
        ▼
Daily-qualified candidates
        │
        ▼
Stage 3 — 4H Setup
        │
        ▼
~150 technical candidates
        │
        ▼
Stage 4 — Context
        │
        ├── Earnings
        └── Options / additional context
        │
        ▼
Nightly Watchlist
        │
        ▼
Hourly Scanner
        │
        ├── 4H setup
        └── 1H entry timing
        │
        ▼
Technical Signal
        │
        ▼
News / Fundamental / Macro interpretation
        │
        ▼
LLM confidence score
        │
        ├── Email: all signals
        └── Telegram: qualified alerts
```

------------------------------------------------------------------------

# 2. Core Trading Philosophy

The system deliberately separates **selection**, **setup**, and
**entry**.

### Daily timeframe

The daily timeframe answers:

> **Is this stock in a market environment that is worth watching?**

It is used for higher-timeframe direction and regime.

### 4-hour timeframe

The 4H timeframe answers:

> **Is a tradable technical setup developing?**

It is used for setup identification.

### 1-hour timeframe

The 1H timeframe answers:

> **Is there an entry opportunity now?**

The 1H entry is intentionally handled by the hourly scanner rather than
the nightly scanner.

This separation prevents the nightly process from trying to predict an
exact entry many hours in advance.

------------------------------------------------------------------------

# 3. Universe

The ticker universe is loaded from `tickers.csv`.

The universe contains:

-   S&P 500 companies
-   Nasdaq companies
-   ETFs

Duplicates should be removed so that a company appearing in both the S&P
500 and Nasdaq portions is processed only once.

The scanner therefore works with a unified symbol universe.

## ETFs

ETFs are treated differently from operating companies.

The stock fundamental/tier funnel is **not applied to ETFs**.

This is intentional because company-level concepts such as:

-   company market capitalization
-   company profitability
-   company debt/equity
-   company financial quality

do not apply to an ETF in the same way.

ETFs therefore bypass the stock market-cap quality funnel.

------------------------------------------------------------------------

# 4. Stage 1 --- FMP Universe / Quality Funnel

## Objective

Stage 1 is the cheapest broad filter.

Its purpose is to reduce the 3,000+ stock universe to approximately
**500 stocks** before expensive historical Twelve Data analysis is
performed.

The current architecture uses the **FMP Company Screener** rather than
the old FMP financial-statement bulk workflow.

This distinction is important.

The old approach attempted to obtain bulk fundamental datasets that
returned HTTP 402 under the available FMP access.

The current approach uses the FMP screener information that is actually
available.

------------------------------------------------------------------------

## 4.1 Stock / ETF separation

The universe is divided into:

``` text
Stocks
ETFs
```

Stocks go through Stage 1 screening.

ETFs bypass the stock screening rules.

------------------------------------------------------------------------

# 5. Market-Cap Tiers

The stock universe uses four tiers.

  Tier     Market Capitalization Treatment
  ------ ----------------------- ---------------------------------
  A                      ≥ \$10B Normal processing
  B                \$1B--\<\$10B Allowed, stronger requirements
  C               \$100M--\<\$1B Allowed, strongest requirements
  D                    \< \$100M Skip

## Tier A --- Large Companies

Companies with market capitalization of at least \$10B.

These are given the normal screening requirements.

The assumption is not that large companies are automatically good.

The purpose is simply that their size generally reduces some of the
liquidity and survivability concerns associated with very small
companies.

------------------------------------------------------------------------

## Tier B --- Mid/Large Companies

Market cap:

``` text
$1B to <$10B
```

These remain eligible, but require stronger evidence than Tier A.

The scanner therefore expects stronger technical quality and liquidity
characteristics.

------------------------------------------------------------------------

## Tier C --- Smaller Companies

Market cap:

``` text
$100M to <$1B
```

These are allowed.

They are **not automatically considered bad companies**.

However, because smaller companies can have:

-   weaker liquidity
-   greater volatility
-   greater financing risk
-   higher execution risk

they must satisfy stronger conditions.

The intended Tier C requirements include:

-   sufficient trading liquidity
-   profitability / acceptable financial quality when data is available
-   acceptable debt characteristics
-   stronger technical score

This is a risk-control mechanism, not a statement that small companies
cannot appreciate.

------------------------------------------------------------------------

## Tier D --- Microcaps

Market cap:

``` text
<$100M
```

These are skipped.

They are intentionally excluded from the main trading universe because
the scanner is designed around a manageable, liquid, repeatable trading
universe.

------------------------------------------------------------------------

# 6. Liquidity Filtering

Liquidity is an important part of Stage 1.

The system should avoid spending detailed technical-analysis resources
on securities that are difficult to trade reliably.

Liquidity filtering can use information such as:

-   trading volume
-   price
-   average dollar volume
-   liquidity ranking

The exact threshold is configurable.

Tier C is deliberately more restrictive.

The principle is:

> **A technically attractive setup is not useful if the underlying
> security is too illiquid to trade reliably.**

------------------------------------------------------------------------

# 7. Stage 1 Funnel Cap

After applying the FMP screener and tier/liquidity logic, the system
keeps at most approximately:

``` text
500 stocks
```

This is a **processing cap**, not necessarily a statement that exactly
500 stocks are good buys.

The goal is to control downstream API consumption.

Example:

``` text
3,085 stocks
     │
     ├── unsuitable / unavailable
     │
     └── ranked survivors
              │
              ▼
         maximum 500
```

The latest successful Stage 1 run demonstrated this behavior:

``` text
Input stocks:       3,085
FMP matched:        1,920
Stage 1 survivors:    500
```

This confirms that the broad funnel is functioning.

------------------------------------------------------------------------

# 8. Why Stage 1 Does Not Do Full Fundamentals

The system previously attempted to use FMP bulk fundamental datasets for
the entire stock universe.

That was inefficient and, more importantly, some required FMP bulk
endpoints returned:

``` text
HTTP 402 — Payment Required
```

Therefore the architecture was changed.

Stage 1 should answer:

> **Is this symbol worth spending more API resources on?**

It does not need to completely understand the company's financial
statements.

Detailed fundamental/news interpretation happens later, after technical
qualification.

This is an intentional separation of concerns.

------------------------------------------------------------------------

# 9. Stage 2 --- Daily Technical / Higher-Timeframe Filter

## Objective

Stage 2 uses Twelve Data daily historical data.

The question is:

> **Is the stock's higher-timeframe trend/regime strong enough to remain
> on the watchlist?**

This stage is more expensive than Stage 1 because historical time-series
data must be retrieved.

That is why only the Stage 1 survivors are sent to Twelve Data.

------------------------------------------------------------------------

# 10. SPY Benchmark

SPY is used as a benchmark for relative-strength analysis.

SPY represents the S&P 500 ETF and is treated as a **reference series**,
not as part of the screened stock universe.

The Stage 2 process should request SPY explicitly.

It must not depend on SPY accidentally appearing in the Stage 1
candidate list.

## SPY preflight

Before spending the main daily-data request budget, Stage 2 performs an
SPY check.

The purpose is to discover a benchmark/API problem early.

If SPY is available:

``` text
Daily trend
+
relative strength vs SPY
```

is used.

If SPY is unavailable:

``` text
Daily trend
+
neutral relative strength
```

can be used as a fallback.

A missing SPY benchmark should be treated as a warning/fallback
condition rather than automatically destroying the entire nightly scan.

------------------------------------------------------------------------

# 11. Daily Technical Logic

The daily analysis is used to assess the higher-timeframe trend.

The existing technical analysis engine can evaluate factors such as:

-   price relative to moving averages
-   EMA structure
-   trend strength
-   momentum
-   relative strength
-   broader daily regime

The daily score is then used to rank candidates.

The exact technical formula remains in the technical analysis module
rather than being duplicated inside the workflow.

This is important because:

> **The workflow decides when to apply the analysis; the analysis module
> decides how the score is calculated.**

------------------------------------------------------------------------

# 12. Tier-Specific Daily Requirements

Smaller companies should have stronger technical requirements.

The current intended thresholds are approximately:

``` text
Tier A → minimum daily trend score: 2.0
Tier B → minimum daily trend score: 2.5
Tier C → minimum daily trend score: 3.0
```

Conceptually:

``` text
Tier A
Normal technical requirement

Tier B
Stronger technical requirement

Tier C
Strongest technical requirement
```

This means a Tier C company does not get rejected simply because it is
small.

Instead:

> **It must prove itself more strongly through technical evidence.**

------------------------------------------------------------------------

# 13. Stage 2 Diagnostics

Stage 2 must distinguish between a genuine technical rejection and a
data/API failure.

These are not the same.

### Genuine rejection

Example:

``` text
Daily trend score below required threshold
```

### Data problem

Example:

``` text
No Twelve Data daily data
```

### History problem

Example:

``` text
Insufficient daily history
```

### Technical-code problem

Example:

``` text
Daily technical calculation error
```

### API problem

Example:

``` text
Twelve Data HTTP 429
```

These should be tracked separately.

This is critical for debugging.

------------------------------------------------------------------------

# 14. Stage 3 --- 4H Setup

After daily filtering, the remaining candidates are evaluated on the
4-hour timeframe.

The question becomes:

> **Does the stock have a useful short-to-medium-term technical setup?**

Examples of setup categories can include:

-   pullback
-   breakout
-   continuation
-   other setups supported by the technical analysis engine

The 4H timeframe bridges the gap between:

``` text
Daily trend
```

and:

``` text
1H entry
```

------------------------------------------------------------------------

# 15. 4H Processing Cap

The nightly scanner should reduce the candidates to a manageable number
before the more expensive context/enrichment stage.

The intended cap is approximately:

``` text
150 stocks
```

Therefore the overall funnel becomes approximately:

``` text
3,000+
   ↓
Stage 1
   ↓
500
   ↓
Stage 2 Daily
   ↓
daily-qualified candidates
   ↓
Stage 3 4H
   ↓
~150
```

The exact number may vary depending on how many candidates pass the
filters.

------------------------------------------------------------------------

# 16. Why 1H Is NOT in the Nightly Scanner

The 1H timeframe is deliberately owned by the hourly scanner.

This is an important architectural boundary.

Nightly:

``` text
Daily → 4H
```

Hourly:

``` text
4H → 1H
```

The nightly scanner asks:

> "What should I watch?"

The hourly scanner asks:

> "Is this the time to enter?"

This prevents unnecessary repeated 1H analysis of thousands of
securities.

------------------------------------------------------------------------

# 17. Stage 4 --- Context / Enrichment

After technical filtering, the remaining candidates receive additional
context.

This stage can include:

-   upcoming earnings
-   options context
-   other event information
-   supporting metadata

The important principle is:

> **Context enriches a technically qualified candidate. It does not
> replace the technical funnel.**

The system should not spend expensive context/API resources on thousands
of securities that already failed the technical requirements.

------------------------------------------------------------------------

# 18. Earnings Risk

Upcoming earnings are particularly important.

A technically attractive setup immediately before an earnings
announcement carries a different risk profile than the same setup
without an imminent earnings event.

The nightly process therefore identifies stocks with earnings within the
configured near-term window.

This information is passed to the watchlist and later signal/news
analysis.

------------------------------------------------------------------------

# 19. Nightly Watchlist

The output of the nightly workflow is the next day's candidate universe.

The watchlist is therefore not:

> "Stocks to buy."

It is:

> **"Stocks worth monitoring for a valid entry."**

This distinction is fundamental.

A stock can survive the nightly process and still never produce a buy
signal.

------------------------------------------------------------------------

# 20. Hourly Scanner

The hourly scanner operates on the smaller nightly universe.

Its purpose is real-time/near-real-time entry detection.

The intended logic is:

``` text
Nightly watchlist
       ↓
4H setup
       ↓
1H entry
       ↓
Technical signal
```

The hourly scanner can therefore examine the exact conditions required
for an entry without processing the entire 3,000+ stock universe.

------------------------------------------------------------------------

# 21. Technical Signal

When the hourly scanner finds a qualifying setup, it creates the
technical signal.

The signal can contain:

-   ticker
-   current/entry price
-   take-profit target
-   shares
-   timeframe
-   technical score
-   setup type
-   trend information
-   EMA structure
-   RSI
-   VWAP
-   other technical fields

The technical signal is the trigger for the next stage.

------------------------------------------------------------------------

# 22. News / Fundamental / Macro Interpretation

Once a technical signal exists, the news workflow enriches it.

The LLM is **not** being asked to scan 3,000 companies.

Instead:

``` text
Technical scanner
       ↓
Qualified signal
       ↓
News / fundamentals / sector / macro
       ↓
LLM interpretation
```

This makes much better use of API credits.

The LLM's role is:

> **Evaluate whether the surrounding information supports or weakens an
> already-existing technical thesis.**

It is not the primary stock-selection engine.

------------------------------------------------------------------------

# 23. LLM Assessment

The interpretation can consider:

### Analyst view

-   analyst ratings
-   consensus
-   estimate revisions

### Financial results

-   earnings surprises
-   revenue
-   net income
-   margins
-   cash flow
-   debt

### Valuation

Where available:

-   P/E
-   P/S
-   P/B
-   other relevant valuation metrics

### Earnings

-   next expected earnings date
-   proximity to trade window

### News

-   recent company-specific news
-   meaningful catalysts or risks

### Sector

-   sector condition
-   industry condition

### Macro

-   market risk
-   volatility
-   relevant macro conditions

The purpose is not to mechanically average these fields.

The LLM interprets the combined evidence.

------------------------------------------------------------------------

# 24. Confidence Score

The final interpretation produces a confidence score.

Conceptually:

``` text
Technical setup
       +
Analyst information
       +
Financial information
       +
Valuation
       +
Earnings
       +
News
       +
Sector
       +
Macro
       ↓
Overall confidence
```

For example:

``` text
Confidence: 72%
```

The score is intended to represent:

> **How strongly the available evidence supports the technical trading
> thesis.**

It is not a guaranteed probability of profit.

------------------------------------------------------------------------

# 25. Notification Logic

Notifications are intentionally separated.

## Email

Email receives the full set of signals according to the configured
workflow.

Purpose:

> Complete record / review.

## Telegram

Telegram is intended to be selective.

The intended rule is:

``` text
New qualifying signal
AND
confidence > 60%
```

Then send the Telegram alert.

This prevents Telegram from becoming a duplicate feed of every technical
signal.

------------------------------------------------------------------------

# 26. Why the LLM Comes Late

The architecture deliberately places LLM interpretation near the end.

This saves:

-   OpenAI API usage
-   Anthropic API usage
-   FMP context calls
-   processing time

Instead of:

``` text
3,000 stocks
→ LLM
```

the architecture aims for:

``` text
3,000
→ 500
→ 150
→ technical signals
→ small number of LLM calls
```

This is one of the most important cost-control principles in the
project.

------------------------------------------------------------------------

# 27. Diagnostics Architecture

Each nightly stage should produce diagnostics.

The diagnostic system records:

``` text
Input
Passed
Failed
Failure reasons
API errors
Ticker-level status
```

The purpose is to distinguish:

### Stock rejection

``` text
Daily trend too weak
```

from:

### Data failure

``` text
No Twelve Data data
```

from:

### API failure

``` text
HTTP 429
HTTP 402
```

from:

### Software failure

``` text
Technical calculation error
```

This allows future debugging without guessing.

------------------------------------------------------------------------

# 28. Example Diagnostic Funnel

A healthy run might look like:

``` text
STAGE 1
Input:       3,085
FMP matched: 1,920
Passed:        500

Reasons:
- FMP data unavailable
- insufficient liquidity
- tier-specific requirements
- market-cap requirements


STAGE 2
Input:         500
Passed:        300

Reasons:
- no daily data
- insufficient history
- weak daily trend
- weak relative strength


STAGE 3
Input:         300
Passed:        120

Reasons:
- no 4H setup
- insufficient 4H history
- setup score too low


STAGE 4
Input:         120
Watchlist:     120
```

The actual numbers are expected to change from day to day.

------------------------------------------------------------------------

# 29. API-Cost Strategy

The system intentionally uses different data sources for different jobs.

### FMP

Best used early for broad company/universe information that is
accessible under the current plan.

### Twelve Data

Used for historical market data and technical analysis after the
universe has been reduced.

### LLM

Used only after a technical signal exists.

This creates a cost hierarchy:

``` text
Cheap / broad
     ↓
FMP screener

Moderate / narrower
     ↓
Twelve Data Daily

More expensive / narrower
     ↓
Twelve Data 4H

Context / enrichment
     ↓
FMP + other context

Most selective
     ↓
LLM
```

------------------------------------------------------------------------

# 30. Important Design Principle: Do Not Filter Everything at Once

The system should **not** attempt to obtain every piece of data for
every ticker at the beginning.

Bad architecture:

``` text
3,000 stocks
 ↓
fundamentals
 ↓
news
 ↓
valuation
 ↓
sector
 ↓
macro
 ↓
daily
 ↓
4H
 ↓
1H
 ↓
LLM
```

This wastes API calls.

Better architecture:

``` text
3,000
 ↓
cheap broad filter
 ↓
500
 ↓
daily
 ↓
smaller universe
 ↓
4H
 ↓
~150
 ↓
context
 ↓
technical signal
 ↓
LLM
```

The expensive analysis is reserved for the candidates that have already
demonstrated enough potential.

------------------------------------------------------------------------

# 31. What the System Is NOT Trying to Do

The scanner is not designed to:

-   predict every winning stock
-   identify the best company in the market
-   guarantee profitable trades
-   buy every nightly survivor
-   treat a high market cap as a buy signal
-   treat a high LLM confidence score as certainty
-   use fundamentals alone to determine entry timing

Instead, it is designed to identify **high-quality opportunities where
multiple layers of evidence align**.

------------------------------------------------------------------------

# 32. Future Performance Validation

The current thresholds are design choices.

They should eventually be validated against actual outcomes.

Important metrics to track include:

### By tier

-   A win rate
-   B win rate
-   C win rate

### By setup

-   pullback
-   breakout
-   continuation

### By confidence

For example:

``` text
Confidence 60–69%
Confidence 70–79%
Confidence 80–89%
Confidence 90%+
```

Then compare those groups with actual trade outcomes.

### By stage

Measure whether each stage actually improves the quality of the
candidates.

For example:

``` text
Stage 1 survivors
vs
Stage 2 survivors
vs
Stage 3 survivors
vs
Final signals
```

This allows thresholds to be tuned based on evidence rather than
intuition.

------------------------------------------------------------------------

# 33. Target Architecture

The intended final architecture is:

``` text
                    ┌───────────────────┐
                    │    tickers.csv    │
                    │ S&P + Nasdaq + ETF│
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │     STAGE 1       │
                    │   FMP Screener    │
                    │                   │
                    │ Market cap        │
                    │ Liquidity          │
                    │ Tier A/B/C/D       │
                    └─────────┬─────────┘
                              │
                         ≤500 stocks
                              │
                              ▼
                    ┌───────────────────┐
                    │     STAGE 2       │
                    │  Twelve Data      │
                    │      Daily        │
                    │                   │
                    │ Daily regime      │
                    │ Trend              │
                    │ Relative strength │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │     STAGE 3       │
                    │  Twelve Data 4H   │
                    │                   │
                    │ Pullback           │
                    │ Breakout           │
                    │ Continuation       │
                    └─────────┬─────────┘
                              │
                         ~150 max
                              │
                              ▼
                    ┌───────────────────┐
                    │     STAGE 4       │
                    │     Context       │
                    │                   │
                    │ Earnings           │
                    │ Options            │
                    │ Event information  │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │  NIGHTLY WATCHLIST│
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │   HOURLY SCANNER  │
                    │                   │
                    │ 4H setup           │
                    │ 1H entry           │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ TECHNICAL SIGNAL  │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ NEWS / LLM        │
                    │                   │
                    │ Analyst            │
                    │ Financials         │
                    │ Valuation          │
                    │ Earnings           │
                    │ News               │
                    │ Sector             │
                    │ Macro              │
                    └─────────┬─────────┘
                              │
                              ▼
                    ┌───────────────────┐
                    │ CONFIDENCE SCORE  │
                    └─────────┬─────────┘
                              │
                 ┌────────────┴────────────┐
                 │                         │
                 ▼                         ▼
          Email — all signals      Telegram — qualified
                                   new signals >60%
```

------------------------------------------------------------------------

# 34. Operational Rule

When modifying the codebase, **the current repository is the source of
truth**.

Do not reintroduce an older architecture or an obsolete function simply
because it existed in a previous version.

Before modifying a workflow:

1.  Inspect the current files.
2.  Identify the current function interfaces.
3.  Preserve the current stage architecture.
4.  Change only the requested behavior.
5.  Verify imports and function calls.
6.  Syntax-check the changed Python files.
7.  Validate YAML syntax.
8.  Run the smallest relevant stage first.
9.  Check diagnostics before proceeding to the next stage.

This is especially important because the project has evolved from the
original FMP bulk-fundamentals design to the current staged
FMP-screener + Twelve Data architecture.

------------------------------------------------------------------------

# 35. Current Architecture Summary

### Nightly

``` text
FMP Screener
    ↓
A/B/C/D + liquidity
    ↓
≤500 stocks
    ↓
Daily technical regime
    ↓
4H technical setup
    ↓
Context / earnings / options
    ↓
Nightly watchlist
```

### Hourly

``` text
Nightly watchlist
    ↓
4H setup
    ↓
1H entry
    ↓
Technical signal
    ↓
News / LLM interpretation
    ↓
Confidence score
```

### Notifications

``` text
Email
→ all relevant signals

Telegram
→ new qualifying signals
→ confidence >60%
```

### Fundamental philosophy

``` text
Fundamentals/context support the thesis.

Technical analysis determines the trading setup.

The LLM interprets the combination.

No individual layer guarantees a trade.
```

------------------------------------------------------------------------

# 36. Final Principle

The entire system can be summarized as:

> **Find a liquid, sufficiently qualified security → confirm its
> higher-timeframe trend → wait for a 4H setup → wait for a 1H entry →
> evaluate the surrounding fundamental/news context → assign confidence
> → alert selectively.**

The scanner is therefore designed as a **progressive evidence funnel**,
not as a single giant stock-ranking calculation.
