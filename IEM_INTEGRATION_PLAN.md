# IEM Integration Plan — align the model with Polymarket's real resolution source

**Status:** planning (2026-07-05). No code changed yet.

## Why
Polymarket resolves each daily-temperature market off **one named airport station's finalized daily max/min, via Weather Underground** — whole degrees (°F US / °C elsewhere), the station's local calendar day, revisions ignored, settled on-chain via UMA. The bot currently uses **Open-Meteo gridded ERA5** for truth, which is a ~25 km cell, never the station thermometer. Backcheck: **32.6% of booked paper outcomes disagreed with the actual on-chain resolution**; most "profit" sat on markets that resolved the opposite way. The paper PF 5.33 measured the wrong thermometer.

Fix: make **truth** (resolution, calibration labels, MOS training target) come from the **IEM station** that Polymarket actually resolves on, while keeping the Open-Meteo ensemble as the forecast engine and **retaining MOS** — retrained to correct *forecast → that station*. Winnings already verified on-chain (PR #27).

## Open-Meteo's three roles → what changes

| Role | Today | After |
|---|---|---|
| Ensemble forecast input | Open-Meteo ensemble @ **city** grid | Open-Meteo ensemble @ **station** coords — **kept** (IEM has no ensemble forecast) |
| MOS bias-training truth | `build_historical_skill.py`: forecast − **ERA5 grid** | forecast − **IEM station** — **MOS retained, retargeted** |
| Resolution / calibration labels | `get_historical_actual` → Open-Meteo archive grid | **IEM station** daily max/min (whole-degree, station-local day) |
| Location | `market_scanner` geocodes the **city** | the market's **resolving station** coords |
| Winnings | — | on-chain settlement (already done, PR #27) |

**Honest nuance:** Open-Meteo is *not* fully removed — it stays as the forecast generator; IEM replaces it as the source of *truth*. MOS is the bridge between the two. The only station-native forecast is NWS MOS, which is **US-only** — so the ensemble stays for the international cities.

## IEM facts this plan relies on (from research 2026-07-05)
- **Daily max/min:** `GET https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py?sts=YYYY-MM-DD&ets=YYYY-MM-DD&network={NET}&stations={SID}&var=max_temp_f,min_temp_f&format=json`
- **Network id:** US = `{STATE}_ASOS` (one underscore), station `sid` = ICAO **minus leading K** (`KLGA`→`LGA`). International = `{CC}__ASOS` (two underscores), `sid` = full ICAO (`RKSI`,`VHHH`,`LFPB`,`LLBG`).
- **US sites (LGA/MIA/DFW/ATL) are DSM-backed** = the exact NWS/Wunderground resolving value. **International are IEM recompute** — usually right, can differ ±1° → must be validated/flagged.
- **Rounding trap:** DSM is whole °F; do **not** double-round through °C. Convert from the finest source and replicate the market's exact rounding. (IEM's own "Wagering on ASOS Temperatures" page warns about this.)
- **Latency:** DSM stable a few hours after local midnight; CLI can revise later. No SLA → use a settle buffer (~3–6 h past station-local midnight), optionally re-read to catch a CLI correction.
- **Station metadata:** `https://mesonet.agron.iastate.edu/geojson/network/{NET}.geojson` → `sid`, lat/lon, `tzname`, `time_domain` (archive start).
- **Bulk (backtest):** loop `daily.py` per network with `stations=_ALL`; **1 req/s**, requests >1000 station-years rejected (HTTP 422).
- **NWS MOS (optional forecast signal):** `mos.py?station=K...&model=MEX&...` field `n_x` = 12 h max/min; **US-only** (internationals return empty).
- 8 traded stations all confirmed present: KLGA `LGA`/NY_ASOS, KMIA `MIA`/FL_ASOS, KDFW `DFW`/TX_ASOS, KATL `ATL`/GA_ASOS, RKSI/KR__ASOS, VHHH/HK__ASOS, LFPB/FR__ASOS, LLBG/IL__ASOS.

## Phased rollout (backtest-first — prove edge before risking more)

### Phase 0 — IEM data layer (read-only, no behavior change)
- **`weather/iem_client.py`**: `daily_maxmin(icao, date, metric) -> float|None` (whole-degree, station-local day, settle-buffer aware); `station_meta(icao) -> {network, sid, lat, lon, tz, archive_start}` (cached from network GeoJSON; US K-strip vs intl two-underscore); `bulk_daily(network, sids, start, end)` for backtest; rounding helper that replicates the market's °F/°C whole-degree rule from the finest value.
- **Station resolver**: extract the ICAO from each market description's Wunderground URL (`.../history/daily/{cc}/{city}/{ICAO}`) — most reliable signal; fall back to the "…Station" name. Map ICAO→IEM network/coords via `station_meta` (cached registry, seeded once from network GeoJSON; auto-extends for new stations).
- **Validation script**: compare IEM daily max vs (a) the 7 live on-chain outcomes and (b) a sample of resolved paper markets' actual on-chain resolutions — i.e. finish the divergence backcheck properly on station data. Records per-station agreement so we know which internationals are trustworthy.
- Tests + rate-limit/retry (1 req/s), tz handling.

### Phase 1 — Truth = IEM station (resolution + calibration labels)
- New `get_station_actual(station, date, metric)` in the weather layer; `PaperTrader.auto_resolve` and calibration labels use it (station coords, whole-degree, station-local day + settle buffer). Keep the Phase-27 `_settle_ready` gate.
- `market_scanner` attaches the resolving station (ICAO + lat/lon/tz) to each `WeatherMarket`; downstream uses station coords, not city geocode.
- **Live:** PnL already books on-chain (authoritative). Add IEM as the same-day provisional label for calibration **and** a cross-check: if IEM-derived outcome ≠ on-chain settlement, alert (catches IEM recompute error or an oracle incident).

### Phase 2 — Retarget MOS to the station (retain mechanism)
- `build_historical_skill.py`: `error = OpenMeteo forecast (Previous Runs, @ station coords) − IEM station actual`, keyed by **station**. Rebuild `historical_skill.json`. `HistoricalSkillCorrector` unchanged in shape — it now shifts members toward the station.
- `get_ensemble_forecast` queried at station lat/lon. MOS bridges grid-forecast → station.

### Phase 3 — Backtest on station truth → **GO / NO-GO gate**
- Re-run `replay_backtest` against IEM station truth + station-based MOS + market-exact whole-degree rounding. Report PF, Brier/BSS, win-rate on the **real** resolution definition.
- **Decision gate:** only if a genuine edge survives on station data do we keep trading / revisit sizing. If the edge evaporates, the strategy needs rethinking before more capital — this is the whole point of the exercise.

### Phase 4 (optional) — NWS MOS bonus signal (US cities only)
- Pull `mos.py` MEX `n_x` for KLGA/KMIA/KDFW/KATL as an extra forecast input / cross-check. Not available for the internationals.

## Risks & mitigations
- **International recompute ≠ exchange value** (no DSM): validate each intl station in Phase 0; widen the no-trade band (or pause) on intl markets until agreement is confirmed.
- **Double-rounding** (°F↔°C): single rounding from the finest source, replicate the market's rule exactly.
- **Latency / CLI revision:** settle buffer past station-local midnight; re-read before trusting; on-chain remains the money-truth backstop.
- **Intl historical depth** may be shallow (check `time_domain`) → backtest coverage varies by city; weight conclusions toward well-covered stations.
- **MOS US-only:** internationals rely on the ensemble+retargeted-MOS only.

## Decisions I need from you
1. **NWS MOS bonus (Phase 4)** — add the US-only station forecast signal, or skip for now? (Free upside, US cities only.)
2. **Backtest-first gate** — confirm we hold sizing at quarter-Kelly (no increase) until Phase 3 proves edge on station data. (Recommended.)
3. **International markets in the interim** — while intl stations are being validated (Phase 0), keep trading them with a wider margin, or pause intl and trade US-only where truth is DSM-exact? (I lean: US-only until intl agreement is confirmed.)
