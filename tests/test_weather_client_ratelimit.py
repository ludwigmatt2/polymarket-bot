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


def test_budget_counts_calls_and_gates_paper_only(monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(wc, "_budget_path", lambda: tmp_path / "om_budget.json")
    monkeypatch.setattr(wc, "_rl_open_until", 0.0)

    class _OK:
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr(wc.requests, "get", lambda *a, **k: _OK())
    assert wc.budget_used() == 0 and not wc.paper_only_budget_exhausted()
    for _ in range(3):
        wc._get_with_retry("u", {}, 1)
    assert wc.budget_used() == 3
    from weather import config
    monkeypatch.setattr(config, "OPEN_METEO_DAILY_BUDGET", 6)
    monkeypatch.setattr(config, "OPEN_METEO_PAPER_ONLY_SHARE", 0.5)
    assert wc.paper_only_budget_exhausted()  # 3 >= 6 * 0.5
    # a stale (yesterday) counter reads as 0
    (tmp_path / "om_budget.json").write_text(json.dumps({"date": "2000-01-01", "n": 99999}))
    assert wc.budget_used() == 0
