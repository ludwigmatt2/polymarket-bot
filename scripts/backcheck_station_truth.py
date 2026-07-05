"""Backcheck: does Wunderground-direct reproduce Polymarket's actual resolution,
vs the Open-Meteo value the bot booked? Samples resolved temperature paper trades,
pulls each market's station (from its rules) + on-chain resolution (gamma), reads
WU for that station/date, and compares. Run: python scripts/backcheck_station_truth.py [N]
"""
import csv, json, sys, time, urllib.request
from datetime import datetime, date
from weather import station_parser, wu_client
from weather.iem_client import f_to_c
from weather.paper_trader import _evaluate_outcome

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40

rows = [r for r in csv.DictReader(open("logs/paper_trades.csv"))
        if r.get("resolved_at") and r.get("actual_outcome") in ("0", "1")
        and r.get("metric", "").startswith("temperature")]
# one trade per unique market, spread across the file for city/date variety
seen, sample = set(), []
step = max(1, len(rows) // N)
for r in rows[::step]:
    if r["market_id"] in seen:
        continue
    seen.add(r["market_id"]); sample.append(r)
    if len(sample) >= N:
        break

def gamma(mid):
    # Paper CSV mixes numeric gamma ids (path form) and 0x conditionIds (query form).
    if mid.startswith("0x"):
        url = f"https://gamma-api.polymarket.com/markets?condition_ids={mid}&closed=true"
    else:
        url = f"https://gamma-api.polymarket.com/markets/{mid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if mid.startswith("0x"):
        return d[0] if isinstance(d, list) and d else {}
    return d

us = {"wu_ok": 0, "wu_n": 0, "om_ok": 0}
intl = {"wu_ok": 0, "wu_n": 0, "om_ok": 0}
skipped = 0
for r in sample:
    try:
        m = gamma(r["market_id"]); time.sleep(1.1)
        if m.get("umaResolutionStatus") != "resolved" or not m.get("outcomePrices"):
            skipped += 1; continue
        pm_yes = json.loads(m["outcomePrices"])[0] == "1"
        st = station_parser.station_from_description(m.get("description"))
        if not st:
            skipped += 1; continue
        day = datetime.fromisoformat(r["resolution_date"]).date()
        hl = wu_client.daily_high_low(st["icao"], day, st["country"]); time.sleep(0.4)
        if not hl:
            skipped += 1; continue
        kind = "max_f" if r["metric"] == "temperature_2m_max" else "min_f"
        wu_c = f_to_c(hl[kind])
        thr = float(r["threshold"]); thr_hi = float(r["threshold_high"]) if r.get("threshold_high") else None
        wu_yes = _evaluate_outcome(wu_c, thr, r.get("weather_direction", "above"), thr_hi)
        om_yes = r["actual_outcome"] == "1"
        bucket = us if st["country"] == "US" else intl
        bucket["wu_n"] += 1
        bucket["wu_ok"] += (wu_yes == pm_yes)
        bucket["om_ok"] += (om_yes == pm_yes)
    except Exception as e:
        skipped += 1
        continue

def show(label, b):
    n = b["wu_n"]
    if not n:
        print(f"{label}: no data"); return
    print(f"{label:6} n={n:3}  WU agree={b['wu_ok']:3}/{n} ({100*b['wu_ok']/n:4.1f}%)  "
          f"OpenMeteo agree={b['om_ok']:3}/{n} ({100*b['om_ok']/n:4.1f}%)")

print(f"sampled {len(sample)} markets, {skipped} skipped (unresolved/no-station/no-WU)\n")
show("US", us); show("INTL", intl)
tot_n = us["wu_n"] + intl["wu_n"]
if tot_n:
    print(f"\nTOTAL n={tot_n}  WU={100*(us['wu_ok']+intl['wu_ok'])/tot_n:.1f}%  "
          f"OpenMeteo={100*(us['om_ok']+intl['om_ok'])/tot_n:.1f}%")
