from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from weather import expansion_log as el


def _sig(mid, icao, model_p=0.2, market_p=0.3, rej=None, res=None):
    m = SimpleNamespace(
        market_id=mid, station_icao=icao, metric="temperature_2m_max", threshold=20.0,
        threshold_high=None, direction="above", resolve_unit="C", station_country="",
        location=SimpleNamespace(timezone="UTC"),
        resolution_date=res or datetime.now(timezone.utc) + timedelta(hours=20))
    return SimpleNamespace(market=m, model_p=model_p, market_p=market_p,
                           rejection_reason=rej, prob_result=SimpleNamespace(raw_p=0.25))


def test_logs_station_markets_once_per_day_with_control_flag(tmp_path):
    p = tmp_path / "c.csv"
    sigs = [_sig("a", "EDDM", rej="gate7_extreme_price:0.97"), _sig("b", "KLGA"), _sig("c", "")]
    assert el.log_scan(sigs, p) == 2          # station-less market skipped
    assert el.log_scan(sigs, p) == 0          # same UTC day -> deduped
    import csv
    rows = {r["market_id"]: r for r in csv.DictReader(open(p))}
    assert rows["a"]["expansion"] == "1" and rows["b"]["expansion"] == "0"
    assert rows["a"]["rejection"] == "gate7_extreme_price"


def test_resolve_prefers_chain_then_report(tmp_path, monkeypatch):
    import weather.relayer as relayer
    p = tmp_path / "c.csv"
    past = datetime.now(timezone.utc) - timedelta(days=3)
    el.log_scan([_sig("a", "EDDM", 0.9, 0.5, res=past), _sig("b", "EDDM", 0.1, 0.5, res=past)],
                p, now=past - timedelta(hours=10))
    monkeypatch.setattr(relayer, "chain_outcome", lambda cid: cid == "a")
    assert el.resolve_rows(p) == 2
    rep = el.station_report(p)
    assert rep[0]["n"] == 2 and rep[0]["onchain_frac"] == 1.0
    assert rep[0]["brier_model"] < rep[0]["brier_market"]
    assert abs(rep[0]["bias"]) < 0.11


def test_event_date_for_utc_plus_13_uses_named_day():
    from datetime import date
    from weather.paper_trader import _local_event_date
    from weather.station_obs import local_event_date
    res = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    assert local_event_date(res, "Pacific/Auckland") == date(2026, 10, 25)
    assert _local_event_date(res, "Pacific/Auckland") == date(2026, 10, 25)
    assert local_event_date(res, "America/Los_Angeles") == date(2026, 10, 25)
    # non-12:00Z deadlines still convert to local time
    odd = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)
    assert local_event_date(odd, "Pacific/Auckland") == date(2026, 10, 26)


def test_forecast_mean_and_std_logged_and_old_header_migrated(tmp_path):
    import csv
    p = tmp_path / "c.csv"
    with open(p, "w", newline="") as f:  # file created before the fc_* columns existed
        w = csv.writer(f)
        w.writerow([c for c in el.FIELDS if not c.startswith("fc_")])
        w.writerow(["2026-10-25T10:00:00+00:00", "old"] + [""] * (len(el.FIELDS) - 4))
    s = _sig("a", "EDDM")
    s.forecast = SimpleNamespace(all_members=[10.0, 12.0, 14.0, 12.0])
    assert el.log_scan([s], p) == 1
    rows = {r["market_id"]: r for r in csv.DictReader(open(p))}
    assert rows["a"]["fc_mean"] == "12.0" and float(rows["a"]["fc_member_std"]) > 0
    assert rows["old"]["fc_mean"] == ""


def test_live_ineligible_reason_and_telegram_filter(monkeypatch):
    import telegram_bot as tb
    import weather.live_trader as lt
    monkeypatch.setattr(lt, "LIVE_PAPER_ONLY_STATIONS", frozenset({"RKPK"}))
    mk = lambda metric, icao: SimpleNamespace(metric=metric, station_icao=icao)
    assert lt.live_ineligible_reason(mk("temperature_2m_max", "RKPK")) == "paper_only_station:RKPK"
    assert lt.live_ineligible_reason(mk("temperature_2m_min", "KLGA")).startswith("excluded_metric")
    assert lt.live_ineligible_reason(mk("temperature_2m_max", "KLGA")) is None

    monkeypatch.setattr(tb, "get_user_mode", lambda uid: "live")
    monkeypatch.setattr(tb, "_signals_path", lambda uid: __import__("pathlib").Path("/nonexistent"))
    sigs = [
        {"direction": "NO", "edge_pp": .18, "model_p": .3, "mkt_p": .5, "title": "Busan", "live_eligible": False},
        {"direction": "YES", "edge_pp": .12, "model_p": .4, "mkt_p": .3, "title": "Miami", "live_eligible": True},
    ]
    out = tb.fmt_signals(sigs, 1)
    assert "Miami" in out and "Busan" not in out and "1 paper-only" in out
    assert "No live-capable" in tb.fmt_signals(sigs[:1], 1)
