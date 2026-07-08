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

    def _client_with_counters(self, monkeypatch, members):
        client = WeatherClient()
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

    def test_empty_result_not_cached(self, monkeypatch):
        """A transient failure (0 members) must be retried by the next market,
        not pinned in the cache for the TTL."""
        client, calls = self._client_with_counters(monkeypatch, members=[])
        client.get_ensemble_forecast(_loc(), date(2026, 6, 12), "temperature_2m_max")
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
