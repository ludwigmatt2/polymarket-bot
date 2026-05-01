# Weather Data Sources — Research & Recommendations

**Context:** Polymarket weather arbitrage bot. Currently consumes Open-Meteo's free `/v1/ensemble` (GFS ~30 + ICON-EPS ~40 members), 7-day horizon. Monthly aggregate markets (e.g. Seoul May precipitation) require a 30-day horizon, and the current 7-day-times-historical-scaling hack mis-prices badly (24 mm projected vs ~100 mm climatology).

**Tooling caveat:** WebSearch and WebFetch were both blocked in the research session, so figures below come from training knowledge. Items tagged **[verify]** drift and should be confirmed on the provider page before commit.

## What we actually need

1. **Subseasonal (weeks 2–4) probabilistic precipitation** — the real blocker.
2. **Seasonal (1–6 month) ensembles** — secondary, useful as climatology-adjusted prior past day ~30.
3. **Probabilistic, not deterministic** — we trade tail probabilities (P(precip > X mm)). Single-number forecasts are useless.
4. Free or cheap; Python `requests`-friendly is a plus.

The horizon, not spatial resolution, is the bottleneck.

## Candidate evaluation

### 1. ECMWF — IFS / ENS / AIFS
- **Free open-data** (`data.ecmwf.int/forecasts/`): IFS HRES (deterministic, 0.25°) + ENS (51 members), out to **15 days [verify]**, 4 cycles/day, GRIB2 over HTTPS, no auth. AIFS (ML model) also free.
- **Subseasonal ENS-extended (46d, 101 members)** and **SEAS5 seasonal (7mo, 51 members)** are *not* in open data — paid via ECMWF Web API/MARS (4-figure EUR/yr), or **free via Copernicus C3S** with an embargo on the latest cycle.
- **Skill:** consensus best global NWP, especially day 5+.
- **Integration:** GRIB2 only (`cfgrib`/`xarray`/`pygrib`).
- **Verdict:** **Switch the 0–15d backbone to ECMWF open-data ENS.** For subseasonal/seasonal, route via C3S, not ECMWF directly.

### 2. NOAA NOMADS — GFS / GEFS direct
- Free, no auth. GFS (0.25°, 16d, 4×/day) + GEFS (31 members, 16d standard + **35d long-tail product**). GRIB2; subset via NOMADS filter CGI or GrADS server.
- Latency ~3–5h post-cycle.
- Skill lags ECMWF ENS by ~1d effective lead time.
- **Verdict:** **Strong supplement.** GEFS 35-day is the cheapest real subseasonal signal. Go direct, not via Open-Meteo.

### 3. NASA GEOS-FP / GEOS-S2S
- Free via GMAO (OPeNDAP/THREDDS). GEOS-S2S has ~4 RT members — too small for clean tail probabilities. Stronger on tropical convection than midlatitude precip.
- **Verdict:** **Skip.** Research-grade, not bot-grade.

### 4. Copernicus C3S (CDS)
- **Free with registration.** `cdsapi` Python client, async/queue-based.
- Holdings: **subseasonal multi-model** (ECMWF, UKMO, NCEP, JMA, Météo-France) weeks 1–6, with **multi-week embargo** on the latest cycle for non-commercial users **[verify — historically 21 days]**; **seasonal multi-model** (SEAS5 + GloSea + others) 7-month horizon released ~13th of each month; **ERA5 reanalysis** (1940–present, ~3-month real-time delay).
- Licence permits commercial use of released data; embargoed real-time is a grey zone — stick to released.
- **Verdict:** **Highest-value addition.** Free multi-model subseasonal + seasonal + ERA5. Solves the monthly-horizon problem. **Top priority.**

### 5. IBM Weather Company API
- Enterprise pricing, $1k+/month minimum, no public self-serve, no exposed multi-member ensemble.
- **Verdict:** **Skip.** Out of budget, wrong shape.

### 6. Visual Crossing / Meteomatics / AccuWeather
- **Visual Crossing:** ~$35/mo, deterministic only, no ensemble — doesn't fix horizon. Skip.
- **Meteomatics:** ~$300/mo, exposes ECMWF-IFS/ENS via clean REST, longer horizons. **Best paid commercial fallback** if GRIB plumbing eats too much time. **[verify pricing]**
- **AccuWeather:** Limited free, no ensemble. Skip.

### 7. Open-Meteo paid (Pro / Business)
- Pro ~€29/mo: higher rate limits, commercial licence, **same models as free**. No new horizon, no subseasonal product **[verify]**.
- **Verdict:** **Skip the upgrade.** Pay only if rate limits become the issue (they aren't).

### 8. Tomorrow.io
- 14d hyperlocal ML blend, no real ensemble — confidence bands aren't members.
- **Verdict:** **Skip.** ML blending hides our signal source.

### 9. Microsoft Planetary Computer / Google Earth Engine
- Free hosting/mirror of GFS, ERA5, HRRR via STAC/EE.
- Useful for ERA5 archive at scale, **not a real-time forecast feed**.
- **Verdict:** **Use as ERA5 backend** if CDS volumes become painful; otherwise CDS is enough.

## Comparison table

| Source | Horizon | Ensemble | Cost @ our use | Subseasonal skill | Integration | Verdict |
|---|---|---|---|---|---|---|
| Open-Meteo free (current) | 7d | ~70 | $0 | None | Easy JSON | Keep, short-range only |
| **ECMWF open-data ENS** | **15d** | **51** | **$0** | Low (capped at 15d) | GRIB2 | **Add (medium-range)** |
| **NOAA NOMADS GEFS** | **35d (long)** | **31** | **$0** | Modest weeks 2–3 | GRIB2 | **Add (subseasonal floor)** |
| NASA GEOS-S2S | 45d | ~4 RT | $0 | Modest | OPeNDAP | Skip |
| **C3S (CDS)** | **46d sub / 7mo seas** | **multi-model 50–200** | **$0** | **Best free** | `cdsapi` async | **Add (top priority)** |
| IBM Weather Co | 15d | none | $1k+/mo | n/a | REST | Skip |
| Visual Crossing | 15d | no | ~$35/mo | n/a | REST | Skip |
| Meteomatics | 15d+ ECMWF | yes | ~$300/mo | Good (ECMWF) | REST clean | Hold (paid fallback) |
| AccuWeather | 15d | no | enterprise | n/a | REST | Skip |
| Open-Meteo Pro | 7d | same | ~€29/mo | None | JSON | Skip |
| Tomorrow.io | 14d | none real | $0–$X00 | n/a | JSON | Skip |
| MS Planetary / GEE | mirror | n/a | $0 | n/a | STAC/EE | ERA5 backend only |

## Prioritised action list

**Hands-on this week (free, high-value):**
1. **Sign up for Copernicus CDS** (`cds.climate.copernicus.eu`), install `cdsapi`. Pull one seasonal forecast for Seoul May precip, one subseasonal forecast for next 46d, and ERA5 May climatology (30y). Does multi-model subseasonal beat our scaling hack? **This single experiment decides the architecture.**
2. **Wire ERA5 climatology priors** regardless. Pure climatology (30-yr mean ± std) beats a 7-day extrapolation past day ~10. Cheap insurance.
3. **NOAA NOMADS GEFS-35d direct** as second subseasonal opinion. Free, no signup. Triangulate against C3S — disagreement = distrust both.

**Next, if 1–3 work:**
4. **ECMWF open-data ENS direct** for 0–15d. Replace Open-Meteo as the medium-range backbone. Keep Open-Meteo as fallback.

**Defer / monitor:**
5. **Meteomatics (~$300/mo)** — only if GRIB plumbing eats >10h/mo.
6. **MS Planetary Computer** — only if CDS volumes hurt.

**Skip outright:** IBM, AccuWeather, Tomorrow.io, Visual Crossing, Open-Meteo Pro, NASA GEOS.

## Bottom line

The fix isn't a fancier short-range API; it's a **real subseasonal/seasonal ensemble**, which **Copernicus CDS provides for free**. Stack: **C3S (subseasonal + seasonal + ERA5) + NOMADS GEFS-35d direct + ECMWF open-data ENS for 0–15d**, keep Open-Meteo as fast fallback. The Seoul-May-style mispricing collapses. Total cost **$0/month**, ~1 weekend of `cdsapi` + GRIB2 plumbing. Reserve the $50–200/mo budget for Meteomatics only if GRIB becomes a tax.
