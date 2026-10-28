import pytest
import requests

from weather import weather_client as wc


class _R:
    status_code = 429

    def raise_for_status(self):
        raise requests.exceptions.HTTPError("429")


def test_429_ladder_opens_breaker_then_fails_fast(monkeypatch):
    calls = []
    monkeypatch.setattr(wc.requests, "get", lambda *a, **k: calls.append(1) or _R())
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)
    monkeypatch.setattr(wc, "_rl_open_until", 0.0)
    with pytest.raises(requests.exceptions.HTTPError):
        wc._get_with_retry("u", {}, 1)
    assert len(calls) == 1 + len(wc._RETRY_DELAYS_S)
    with pytest.raises(requests.exceptions.HTTPError):
        wc._get_with_retry("u", {}, 1)
    assert len(calls) == 1 + len(wc._RETRY_DELAYS_S)  # no new request while open
    monkeypatch.setattr(wc, "_rl_open_until", 0.0)
