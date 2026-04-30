---
name: market-analysis
description: Polymarket prediction market research — event analysis, probability assessment, edge identification, bet sizing. USE WHEN user wants to analyse a Polymarket market, assess event probability, find mispriced markets, or research an upcoming event for the bot.
---

# Market Analysis Skill — Polymarket Bot

Structured analysis of Polymarket prediction markets to identify edge and inform bot trading decisions.

## Analysis Framework

### Step 1 — Market Overview
For a given market, collect:
- **Question:** exact resolution criteria
- **Current price:** YES / NO probabilities (implied odds)
- **Volume:** total liquidity + 24h volume (signals market efficiency)
- **Closing date:** time to resolution
- **Resolution source:** who resolves, what oracle/source is used

### Step 2 — Base Rate Research
Find historical base rates for similar events:
- Political: election polling averages, historical prediction accuracy
- Sports: head-to-head records, form, injury reports
- Economic: analyst consensus, futures markets (compare to Polymarket price)
- News events: precedent cases, legal/regulatory history

Use: brave-search + fetch for current data. Cross-reference with:
- Metaculus (metaculus.com) — aggregated forecasts
- Manifold Markets (manifold.markets) — community probabilities
- Prediction book archives
- Bloomberg/Reuters for market consensus

### Step 3 — Edge Calculation
```
Edge = True Probability - Market Implied Probability

If YES trading at 0.45 and you assess true P = 0.60:
Edge = 0.60 - 0.45 = +0.15 (15 percentage points)

Minimum edge threshold: ≥0.08 (8pp) to trade
```

### Step 4 — Kelly Criterion Sizing
```
f = (bp - q) / b

Where:
  b = odds received (1/price - 1)
  p = estimated true probability
  q = 1 - p

Use HALF-KELLY for safety: f* = f / 2
```

### Step 5 — Risk Flags
Before trading, check:
- [ ] Resolution criteria unambiguous?
- [ ] Oracle/source reliable and verifiable?
- [ ] No obvious manipulation (thin order book)?
- [ ] Information advantage exists (not just noise)?
- [ ] No correlated exposure with existing open positions?

## Output Format
```markdown
## Market: [Question]
URL: polymarket.com/event/[slug]
Close: YYYY-MM-DD
Current price: YES @ $X.XX | NO @ $X.XX
Volume: $X,XXX (24h: $XXX)

### Base Rate Research
[findings with sources]

### Probability Assessment
True P(YES): XX%
Market implied: XX%
Edge: +/-XX pp

### Sizing (bankroll = $XXX)
Kelly fraction: X.X%
Half-Kelly: X.X%
Recommended position: $XX

### Decision: TRADE / PASS / WATCH
Reason: [one line]
Risks: [key risks]
```

Save research to: `~/my-second-brain/projects/polymarket-bot/research/[date]-[slug].md`

## Bot Integration Notes
- Feed analysis output into bot's signal scoring system
- Log all trades with rationale for post-hoc calibration
- Track predicted vs actual probability at close — update model regularly
