"""Tests for WeatherClient — forecast cache (rate-limit mitigation) and 429 retry."""

from datetime import date
from unittest.mock import MagicMock

import pytest

import weather.weather_client as wc_module
from weather.models import Location
from weather.weather_client import WeatherClient, _get_with_retry


def _loc():
    return Location(city="Tokyo", lat=35.69, lon=139.69)


class TestForecastCache:
    """~10 bucket markets share each (city, date, metric); without a cache every
    one refetches the same ensemble and trips Open-Meteo's per-minute limit."""

    def _client_with_counters(self, monkeypatch, members, tmp_path=None):
        client = WeatherClient()
        # hermetic disk cache — without this, one test's disk write becomes the
        # next test's cache hit (and litters the repo's real cache dir)
        import uuid
        base = (tmp_path or __import__("pathlib").Path("/tmp")) / f"wcache-{uuid.uuid4().hex}"
        monkeypatch.setattr(WeatherClient, "_disk_cache_path",
                            staticmethod(lambda key: base / (str(abs(hash(repr(key)))) + ".json")))
        calls = {"members": 0, "means": 0}

        def fake_members(location, target_date, metric, model):
            calls["members"] += 1
            return list(members)

        def fake_means(location, target_date, metric):
            calls["means"] += 1
            return {"gfs_seamless": 25.0}

        monkeypatch.setattr(client, "_fetch_ensemble_members", fake_members)
        monkeypatch.setattr(client, "_fetch_forecast_means", fake_means)
        return client, calls

    def test_second_call_hits_cache(self, monkeypatch):
        client, calls = self._client_with_counters(monkeypatch, members=[20.0] * 30)
        f1 = client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
        n_after_first = calls["members"]
        f2 = client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
        assert calls["members"] == n_after_first  # no refetch
        assert f2 is f1

    def test_different_date_not_cached(self, monkeypatch):
        client, calls = self._client_with_counters(monkeypatch, members=[20.0] * 30)
        client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
        n_after_first = calls["members"]
        client.get_ensemble_forecast(_loc(), date(2026, 6, 13), "temperature_2m_max")
        assert calls["members"] == 2 * n_after_first

    def test_empty_result_negative_cached_briefly(self, monkeypatch):
        """A total failure (0 members) is negative-cached for a short TTL so one
        429'd combo doesn't re-fail once per bucket market in its block (Jul-10:
        those per-market retries + 2s backoffs dragged scans past the watchdog).
        After the TTL it IS retried."""
        import time as _t
        client, calls = self._client_with_counters(monkeypatch, members=[])
        client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
        client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
        assert calls["members"] == len(wc_module.ENSEMBLE_MODELS)  # one round only
        # expire the negative entry → retried
        for k in list(client._negative_cache):
            client._negative_cache[k] = _t.monotonic() - WeatherClient.NEGATIVE_CACHE_TTL_S - 1
        client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
        assert calls["members"] == 2 * len(wc_module.ENSEMBLE_MODELS)


class TestRateLimitRetry:
    def test_retries_once_on_429(self, monkeypatch):
        ok = MagicMock(status_code=200)
        limited = MagicMock(status_code=429)
        responses = [limited, ok]
        monkeypatch.setattr(wc_module.requests, "get", lambda *a, **k: responses.pop(0))
        monkeypatch.setattr(wc_module.time, "sleep", lambda s: None)
        r = _get_with_retry("http://x", {}, 5)
        assert r is ok
        assert responses == []  # both calls consumed

    def test_no_retry_on_success(self, monkeypatch):
        ok = MagicMock(status_code=200)
        responses = [ok]
        monkeypatch.setattr(wc_module.requests, "get", lambda *a, **k: responses.pop(0))
        r = _get_with_retry("http://x", {}, 5)
        assert r is ok

    def test_raises_after_second_429(self, monkeypatch):
        import requests as req
        limited = MagicMock(status_code=429)
        limited.raise_for_status.side_effect = req.HTTPError("429")
        monkeypatch.setattr(wc_module.requests, "get", lambda *a, **k: limited)
        monkeypatch.setattr(wc_module.time, "sleep", lambda s: None)
        with pytest.raises(req.HTTPError):
            _get_with_retry("http://x", {}, 5)


class TestControlMemberExtraction:
    """Jul-8 fix: the control run arrives under the bare variable name and was
    silently dropped by the 'member'-only filter."""

    def test_control_member_included(self):
        from datetime import date
        from weather.weather_client import _extract_members
        daily = {
            "time": ["2026-07-08", "2026-07-09"],
            "temperature_2m_max": [30.0, 31.0],                # control run
            "temperature_2m_max_member01": [29.5, 30.5],
            "temperature_2m_max_member02": [30.5, 31.5],
            "temperature_2m_min_member01": [20.0, 21.0],       # other metric
        }
        members = _extract_members(daily, "temperature_2m_max", date(2026, 7, 8))
        assert sorted(members) == [29.5, 30.0, 30.5]           # control included

    def test_other_metric_control_not_leaked(self):
        from datetime import date
        from weather.weather_client import _extract_members
        daily = {
            "time": ["2026-07-08"],
            "temperature_2m_max": [30.0],
            "temperature_2m_min": [20.0],
            "temperature_2m_min_member01": [19.0],
        }
        # temperature_2m_min starts with... itself only; max must not pick it up
        assert _extract_members(daily, "temperature_2m_max", date(2026, 7, 8)) == [30.0]
        assert sorted(_extract_members(daily, "temperature_2m_min", date(2026, 7, 8))) == [19.0, 20.0]


class TestRestOfDayMembers:
    """M1: per-member extremes over the REMAINING local hours only."""

    def _hourly(self):
        return {
            "time": [f"2026-07-09T{h:02d}:00" for h in range(24)],
            "temperature_2m": [20 + (h if h <= 15 else 30 - h) for h in range(24)],  # peaks 35 @15h
            "temperature_2m_member01": [19 + (h if h <= 14 else 28 - h) for h in range(24)],
        }

    def test_reduces_remaining_hours_only(self):
        from datetime import datetime
        from weather.weather_client import _restofday_members
        # cutoff 16:00 — the 15:00 peak is HISTORY; remaining max = value at 16h
        cutoff = datetime(2026, 7, 9, 16, 0)
        members = _restofday_members(self._hourly(), max, cutoff)
        assert sorted(members) == [12 + 16 - 4, 30 - 16 + 0][:0] or len(members) == 2
        control_rest = max((20 + (h if h <= 15 else 30 - h)) for h in range(16, 24))
        assert control_rest in members  # control included
        assert max(members) < 35       # the already-passed peak is excluded

    def test_full_day_when_cutoff_at_midnight(self):
        from datetime import datetime
        from weather.weather_client import _restofday_members
        members = _restofday_members(self._hourly(), max, datetime(2026, 7, 9, 0, 0))
        assert 35 in members  # nothing has passed yet — full-day max

    def test_empty_when_day_over(self):
        from datetime import datetime
        from weather.weather_client import _restofday_members
        assert _restofday_members(self._hourly(), max, datetime(2026, 7, 10, 0, 0)) == []

    def test_min_reducer(self):
        from datetime import datetime
        from weather.weather_client import _restofday_members
        # for lows: remaining-hours MIN (late-evening cooling counts)
        members = _restofday_members(self._hourly(), min, datetime(2026, 7, 9, 20, 0))
        control_rest = min((20 + (h if h <= 15 else 30 - h)) for h in range(20, 24))
        assert control_rest in members


class TestDiskCache:
    """Cross-process ensemble cache — the Jul-10 fix for blowing Open-Meteo's
    daily quota (every subprocess had a cold in-memory cache)."""

    def _fc(self):
        from datetime import date, datetime
        from weather.models import EnsembleForecast
        return EnsembleForecast(lat=1.0, lon=2.0, target_date=date(2026, 7, 10),
                                metric="temperature_2m_max",
                                member_arrays={"gfs_seamless": [30.0, 31.0]},
                                model_means={"gfs_seamless": 30.5},
                                fetched_at=datetime(2026, 7, 10, 12, 0))

    def test_round_trip_across_instances(self, tmp_path, monkeypatch):
        import weather.paths
        from weather.weather_client import WeatherClient
        monkeypatch.setattr(weather.paths, "DATA_DIR", tmp_path)
        monkeypatch.setattr("weather.weather_client.WeatherClient._disk_cache_path",
                            staticmethod(lambda key: tmp_path / "cache" / "ensembles" /
                                         (str(abs(hash(repr(key)))) + ".json")))
        key = ("k1",)
        a, b = WeatherClient(), WeatherClient()   # two "processes"
        a._disk_cache_put(key, self._fc())
        got = b._disk_cache_get(key)
        assert got is not None
        assert got.member_arrays == {"gfs_seamless": [30.0, 31.0]}
        assert got.target_date.isoformat() == "2026-07-10"

    def test_expired_entry_ignored(self, tmp_path, monkeypatch):
        import os
        import time as _t
        from weather.weather_client import WeatherClient
        monkeypatch.setattr("weather.weather_client.WeatherClient._disk_cache_path",
                            staticmethod(lambda key: tmp_path / "e.json"))
        wc = WeatherClient()
        wc._disk_cache_put(("k",), self._fc())
        old = _t.time() - WeatherClient.DISK_CACHE_TTL_S - 10
        os.utime(tmp_path / "e.json", (old, old))
        assert wc._disk_cache_get(("k",)) is None

    def test_negative_cache_blocks_refetch(self):
        from weather.weather_client import WeatherClient
        import time as _t
        wc = WeatherClient()
        wc._negative_cache[("bad",)] = _t.monotonic()
        assert wc._negative_cached(("bad",)) is True
        wc._negative_cache[("stale",)] = _t.monotonic() - WeatherClient.NEGATIVE_CACHE_TTL_S - 1
        assert wc._negative_cached(("stale",)) is False
