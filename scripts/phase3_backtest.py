"""Phase 3 GO/NO-GO backtest: re-score the bot's resolved TEMPERATURE paper trades
against the ACTUAL on-chain resolution (Polymarket's settled outcome — the same
value WU reproduces 100%), and compare the real edge to what the bot reported off
Open-Meteo. Uses the bot's actual historical bets (direction/entry/size/model_p);
it does NOT re-run the new station-MOS model (Open-Meteo ensemble history is <4d),
so this is a lower bound — the honest question "did these bets have edge on REAL
resolutions?". Run: python scripts/phase3_backtest.py [N]
"""
import csv, json, sys, time, urllib.request

N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
rows = [r for r in csv.DictReader(open("logs/paper_trades.csv"))
        if r.get("resolved_at") and r.get("actual_outcome") in ("0", "1")
        and r.get("metric", "").startswith("temperature")
        and r.get("model_p") and r.get("entry_price") and r.get("size_usd")]
seen, sample = set(), []
step = max(1, len(rows) // N)
for r in rows[::step]:
    if r["market_id"] in seen: continue
    seen.add(r["market_id"]); sample.append(r)
    if len(sample) >= N: break

def gamma(mid):
    if mid.startswith("0x"):
        url = f"https://gamma-api.polymarket.com/markets?condition_ids={mid}&closed=true"
    else:
        url = f"https://gamma-api.polymarket.com/markets/{mid}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=20).read())
    return (d[0] if isinstance(d, list) and d else {}) if mid.startswith("0x") else d

def stats(label, n, wins, gw, gl, pnl, size, brier):
    if not n: print(f"{label}: no data"); return
    pf = gw / gl if gl else float("inf")
    print(f"{label:9} n={n:3}  win%={100*wins/n:4.1f}  PF={pf:4.2f}  "
          f"ROI={100*pnl/size:+5.1f}%  meanBrier={brier/n:.3f}  BSS={1-(brier/n)/0.25:+.2f}")

R = dict(n=0, wins=0, gw=0.0, gl=0.0, pnl=0.0, size=0.0, brier=0.0)   # real (on-chain)
O = dict(n=0, wins=0, gw=0.0, gl=0.0, pnl=0.0, size=0.0, brier=0.0)   # reported (Open-Meteo)
flips = 0; skipped = 0
for r in sample:
    try:
        m = gamma(r["market_id"]); time.sleep(1.1)
        if m.get("umaResolutionStatus") != "resolved" or not m.get("outcomePrices"):
            skipped += 1; continue
        pm_yes = json.loads(m["outcomePrices"])[0] == "1"
        mp = float(r["model_p"]); entry = float(r["entry_price"]); size = float(r["size_usd"])
        d = r["direction"]; old_yes = r["actual_outcome"] == "1"
        if pm_yes != old_yes: flips += 1
        for truth, A in ((pm_yes, R), (old_yes, O)):
            win = (truth and d == "YES") or ((not truth) and d == "NO")
            pnl = size * (1.0/entry - 1.0) if win else -size
            A["n"] += 1; A["wins"] += win; A["size"] += size; A["pnl"] += pnl
            A["gw"] += pnl if pnl > 0 else 0.0; A["gl"] += -pnl if pnl < 0 else 0.0
            A["brier"] += (mp - float(truth))**2
    except Exception:
        skipped += 1; continue

print(f"\nsampled {len(sample)} temp markets, {skipped} skipped, {flips} outcome-flips vs Open-Meteo\n")
stats("REAL", **R)         # scored on actual on-chain resolution
stats("REPORTED", **O)     # scored on the Open-Meteo label the bot booked
print("\nREAL = truth (what actually paid).  REPORTED = the bot's Open-Meteo view.")
