# Station expansion plan (written 2026-10-25, for a fresh session)

Goal: trade the full set of Polymarket temperature stations, not just the 14 in
`weather/iem_client.py:_STATION_REGISTRY`, without contaminating the
pre-registered gate evidence and without risking live money on uncalibrated stations.

## Why only 14 stations trade today (two independent gaps)

1. **Discovery.** `WeatherMarketScanner._search_keywords()` uses
   `config.WEATHER_SEARCH_TERMS` (~13 city names). Gamma's
   `/events?tag_slug=weather&closed=false&limit=100&offset=N&order=volume24hr`
   lists ~48 stations / 410 events. The other 34 stations' markets are NEVER fetched,
   so a registry entry alone changes nothing. `scripts/discover_stations.py` has the
   same blind spot (found 15 stations) and a bad country guess for non-`L*` ICAOs
   (`{"L": "IL"}` only; Warsaw EPWA wrongly "no IEM entry").
2. **Registry.** `market_scanner._parse_market` adopts a station only if
   `station_meta(icao)` is registered; unregistered → `station_icao=""` → rejected
   at `gate0.5_no_station_truth` (REQUIRE_STATION_TRUTH).

Context: Polymarket rewrote all temperature rules on 2026-10-23 (METAR via
aviationweather.gov, whole °C everywhere incl. US). Parser fixed in `f4d73b7`
(`ids=ICAO` / "(ICAO)"). Hong Kong resolves off the HK Observatory (no ICAO) and
stays out. Other tag-feed oddities: Moscow/Istanbul/Tel Aviv have empty
`resolutionSource` (rules text still parses).

## Verified 2026-10-25: all 34 are registry-ready

Each is in IEM (network, sid, tz, coords) and returns live METARs from
`aviationweather.gov/api/data/metar?ids=ICAO&format=raw&hours=26`. US stations carry the
T-group (tenths); others whole degrees only (matches Polymarket's body-group
fallback rule). MOSMIX wmo = None for all (never guess; look up later — Munich,
Amsterdam, Warsaw, Milan etc. are likely in MOSMIX; verify per
docs/PHASE3_STATION_FORECAST_PLAN.md).

Paste-ready registry lines (wmo None):

```python
    "EDDM": ("DE__ASOS", "EDDM", "Europe/Berlin", 48.3583, 11.8092, None),  # Munich
    "EFHK": ("FI__ASOS", "EFHK", "Europe/Helsinki", 60.3172, 24.9633, None),  # Helsinki
    "EHAM": ("NL__ASOS", "EHAM", "Europe/Amsterdam", 52.3154, 4.7902, None),  # Amsterdam
    "EPWA": ("PL__ASOS", "EPWA", "Europe/Warsaw", 52.1628, 20.9611, None),  # Warsaw
    "FACT": ("ZA__ASOS", "FACT", "Africa/Johannesburg", -33.9667, 18.6, None),  # Cape Town
    "KAUS": ("TX_ASOS", "AUS", "America/Chicago", 30.183, -97.6799, None),  # Austin
    "KDEN": ("CO_ASOS", "DEN", "America/Denver", 39.8328, -104.6575, None),  # Denver
    "KHOU": ("TX_ASOS", "HOU", "America/Chicago", 29.6375, -95.2824, None),  # Houston
    "KLAX": ("CA_ASOS", "LAX", "America/Los_Angeles", 33.9382, -118.3865, None),  # Los Angeles
    "KORD": ("IL_ASOS", "ORD", "America/Chicago", 41.9602, -87.9316, None),  # Chicago
    "KSEA": ("WA_ASOS", "SEA", "America/Los_Angeles", 47.4447, -122.3144, None),  # Seattle
    "KSFO": ("CA_ASOS", "SFO", "America/Los_Angeles", 37.619, -122.3749, None),  # San Francisco
    "LIMC": ("IT__ASOS", "LIMC", "Europe/Rome", 45.63, 8.7231, None),  # Milan
    "LTAC": ("TR__ASOS", "LTAC", "Europe/Istanbul", 40.1281, 32.9951, None),  # Ankara
    "LTFM": ("TR__ASOS", "LTFM", "Europe/Istanbul", 41.2629, 28.7413, None),  # Istanbul
    "MMMX": ("MX__ASOS", "MMMX", "America/Mexico_City", 19.4363, -99.0721, None),  # Mexico City
    "MPMG": ("PA__ASOS", "MPMG", "America/Panama", 8.9833, -79.5167, None),  # Panama City
    "NZWN": ("NF__ASOS", "NZWN", "Pacific/Auckland", -41.3272, 174.8053, None),  # Wellington
    "OEJN": ("SA__ASOS", "OEJN", "Asia/Riyadh", 21.6598, 39.1222, None),  # Jeddah
    "OPKC": ("PK__ASOS", "OPKC", "Asia/Karachi", 24.8456, 67.1614, None),  # Karachi
    "RCSS": ("TW__ASOS", "RCSS", "Asia/Taipei", 25.0694, 121.5517, None),  # Taipei
    "RKPK": ("KR__ASOS", "RKPK", "Asia/Seoul", 35.1795, 128.9382, None),  # Busan
    "RPLL": ("PH__ASOS", "RPLL", "Asia/Manila", 14.5069, 121.0042, None),  # Manila
    "SAEZ": ("AR__ASOS", "SAEZ", "America/Argentina/Buenos_Aires", -34.8222, -58.5358, None),  # Buenos Aires
    "SBGR": ("BR__ASOS", "SBGR", "America/Sao_Paulo", -23.4321, -46.4695, None),  # Sao Paulo
    "UUWW": ("RU__ASOS", "UUWW", "Europe/Moscow", 55.5915, 37.2615, None),  # Moscow
    "VILK": ("IN__ASOS", "VILK", "Asia/Kolkata", 26.7606, 80.8893, None),  # Lucknow
    "WMKK": ("MY__ASOS", "WMKK", "Asia/Kuala_Lumpur", 2.7167, 101.7, None),  # Kuala Lumpur
    "ZBAA": ("CN__ASOS", "ZBAA", "Asia/Shanghai", 40.0741, 116.587, None),  # Beijing
    "ZGGG": ("CN__ASOS", "ZGGG", "Asia/Shanghai", 23.3964, 113.3008, None),  # Guangzhou
    "ZHHH": ("CN__ASOS", "ZHHH", "Asia/Shanghai", 30.62, 114.13, None),  # Wuhan
    "ZSQD": ("CN__ASOS", "ZSQD", "Asia/Shanghai", 36.0667, 120.3333, None),  # Qingdao
    "ZUCK": ("CN__ASOS", "ZUCK", "Asia/Chongqing", 29.52, 106.48, None),  # Chongqing
    "ZUUU": ("CN__ASOS", "ZUUU", "Asia/Chongqing", 30.6667, 104.0167, None),  # Chengdu
```

Truth chain check (done for the 12 registered stations on Oct 23): station_truth
→ IEM METAR peak reproduced Polymarket's winning bucket 12/12 (Shenzhen,
Singapore, Shanghai, Tokyo, Seoul; high+low). WU returns nothing for these now.
Re-run that comparison per NEW station before it goes live (script idea: closed
events via Gamma `closed=true`, compare `station_truth.daily_value(..., unit="C")`
rounded vs the market with outcomePrices [1,0]).

## What the model lacks at a new station (it degrades silently)

No `historical_skill.json` entry (HistoricalSkillCorrector._nearest_city is a 100 km
nearest-CITY search — a new station may match a far-away city's skill or none),
no CityBias/EMOS per-cell dispersion history, no MOS bias (`mosmix_bias.json`), no
calibrator history. Expect raw-ensemble biases (cf. min-temp grid-vs-station
1-2 °C leak, [[mintemp_structural_leak]]). Forecast itself runs at the station's
coords (registry lat/lon), which helps.

## Plan (do in order; each phase ships + is verified separately)

**Phase 0 — decisions to confirm with Ludo before coding**
- New stations in the gate population? The pre-registered gates
  (`scripts/*_gate.py`) read `paper_trades.csv`; adding 34 unvalidated stations
  mid-experiment changes the population. Recommended: tag new-station rows (e.g.
  `exclude_reason="expansion_unvalidated"`, same mechanism as the live
  `exclude_reason` column from 45bd230) or write a separate
  `paper_trades_expansion.csv`, and keep gates on the 14. Decide which.
- Live promotion rule per station (suggested: ≥30 resolved paper trades at that
  station, PF > 1, no obvious sigma bias; min-temp stays excluded).

**Phase 1 — discovery (no behavior change for the 14)**
1. Add a second source in `WeatherMarketScanner`: page
   `gamma-api.polymarket.com/events?tag_slug=weather&closed=false` (limit 100, offset
   until empty), dedupe with the keyword results by conditionId. Keep keyword search as
   fallback. Watch the scan time budget (scan outran 900 s once — #55; books are
   fetched in a thread pool only for <48 h markets; expect ~2-3× more tradeable).
2. Fix `scripts/discover_stations.py`: use the tag feed, drop the WU probe (no longer
   the truth source) in favour of an AWC METAR probe, replace the `{"L":"IL"}` country
   guess with an ICAO-prefix → IEM network map or IEM's `/api/1/station/{sid}.json`
   (works for all 34, see snippet in session transcript / this doc).
3. Tests: scanner returns tag-feed-only markets (mock Gamma), dedupe, offset
   termination.

**Phase 2 — registry + paper-only guard**
1. Add the 34 lines above to `_STATION_REGISTRY`.
2. Add `LIVE_PAPER_ONLY_STATIONS` (config; default = the 34 new ICAOs, env
   overridable) and enforce in `LiveTrader.execute_signal` next to
   `LIVE_EXCLUDED_METRICS` with skip reason `paper_only_station:ICAO`. Paper keeps
   logging (invariant: never gate paper logging behind live exec).
3. Resolution plumbing: confirm `paper_trader` / `station_truth` resolve new
   stations (unit "C", country "" → registry fallback; `wu_client._country` returns ""
   for unknown → WU skipped → IEM peak path). Verify with settled markets.
4. Tests + dry run like the Oct 25 one (scanner + SignalGenerator, no orders):
   count signals per new station, sanity-check model_p vs market for a few.

**Phase 3 — calibration data for new stations**
- Run `build_historical_skill.py` / the weekly rebuild including the 34 (check it
  iterates the registry, not a city list; Sunday 04:2x UTC timer rebuilds
  `historical_skill.json`; the Sep-22 shrink-guard + merge behaviour must keep the
  existing 30 entries).
- `scripts/mosmix_snapshot.py` already iterates the registry; look up WMO ids for
  the European/Asian ones and fill `wmo` (verify, never guess).
- Per-station bias table (`mosmix_bias.json` / city_bias) fills itself as data accrues.

**Phase 4 — promotion**
- Weekly report per station: n resolved, win rate, PF, mean (model_p − outcome),
  sigma bias. Promote by removing the ICAO from `LIVE_PAPER_ONLY_STATIONS` (env, no
  deploy needed) once the Phase-0 rule is met. Max-temp first; min-temp markets stay in
  `LIVE_EXCLUDED_METRICS`.

## Risks / things to watch
- Scan duration and CLOB book rate limits (`Resource temporarily unavailable`
  appears already at ~270 markets; ~3× more near-term books).
- Paper bankroll/Kelly are per signal, not per station — more stations means more
  simultaneous correlated NO bets on one weather regime; the one-bin-per-event and
  40 % day-exposure caps still apply.
- Southern-hemisphere / tropical stations (Sao Paulo, Wellington, Cape Town, Buenos
  Aires, Singapore-like) have very different error structure; the model was tuned on
  NH mid-latitude.
- Edge is unproven even for the 14 (n<250). Expansion widens the sample faster, which
  is a benefit — but only if the new rows are tagged so the gates stay interpretable.
