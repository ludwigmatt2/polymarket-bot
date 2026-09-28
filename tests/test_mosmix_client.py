"""Tests for weather.mosmix_client — DWD MOSMIX station-forecast client."""
import io
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from weather import mosmix_client as mx

# 10 hourly UTC steps starting 2026-09-28T00:00Z. In America/New_York (EDT,
# UTC-4) the Sep-28 local calendar day is UTC [04:00, next-day 04:00), which
# covers idx2..idx9 (idx0/idx1 fall in the prior local day).
_TIMES_UTC = [
    "2026-09-28T00:00:00.000Z", "2026-09-28T03:00:00.000Z",
    "2026-09-28T06:00:00.000Z", "2026-09-28T09:00:00.000Z",
    "2026-09-28T12:00:00.000Z", "2026-09-28T15:00:00.000Z",
    "2026-09-28T18:00:00.000Z", "2026-09-28T21:00:00.000Z",
    "2026-09-29T00:00:00.000Z", "2026-09-29T03:00:00.000Z",
]
_TTT_K = [280.0, 281.0, 285.0, 290.0, 295.0, 293.0, 288.0, 282.0, 279.0, 277.0]
_E_TTT_K = [0.5, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3]
# Sep-28 local-day subset (idx2..idx9): max TTT 21.85C (idx4), min 3.85C
# (idx9), max sigma 1.3C (idx9) regardless of kind.
_EXPECT_MAX = pytest.approx(21.85, abs=1e-9)
_EXPECT_MIN = pytest.approx(3.85, abs=1e-9)
_EXPECT_SIGMA = pytest.approx(1.3, abs=1e-9)


def _kml(wmo: str, tx_tn: str) -> str:
    ttt_vals = " ".join(str(v) for v in _TTT_K)
    e_ttt_vals = " ".join(str(v) for v in _E_TTT_K)
    steps = "".join(f"<dwd:TimeStep>{t}</dwd:TimeStep>" for t in _TIMES_UTC)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml:kml xmlns:dwd="https://opendata.dwd.de/weather/lib/pointforecast_dwd_extension_V1_0.xsd"
         xmlns:kml="http://www.opengis.net/kml/2.2">
<kml:Document>
<kml:ExtendedData>
<dwd:ProductDefinition>
<dwd:IssueTime>2026-09-28T09:00:00.000Z</dwd:IssueTime>
<dwd:ForecastTimeSteps>{steps}</dwd:ForecastTimeSteps>
</dwd:ProductDefinition>
</kml:ExtendedData>
<kml:Placemark>
<kml:name>{wmo}</kml:name>
<kml:ExtendedData>
<dwd:Forecast dwd:elementName="TTT"><dwd:value>{ttt_vals}</dwd:value></dwd:Forecast>
<dwd:Forecast dwd:elementName="E_TTT"><dwd:value>{e_ttt_vals}</dwd:value></dwd:Forecast>
<dwd:Forecast dwd:elementName="TX"><dwd:value>{tx_tn}</dwd:value></dwd:Forecast>
<dwd:Forecast dwd:elementName="TN"><dwd:value>{tx_tn}</dwd:value></dwd:Forecast>
</kml:ExtendedData>
</kml:Placemark>
</kml:Document>
</kml:kml>"""


def _kmz_bytes(wmo: str, tx_tn: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"MOSMIX_L_LATEST_{wmo}.kml", _kml(wmo, tx_tn))
    return buf.getvalue()


class _Resp:
    def __init__(self, body: bytes):
        self._b = body
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


def _mock_urlopen(monkeypatch, body: bytes | None, exc: Exception | None = None, capture: dict | None = None):
    def fake(req, timeout=30):
        if capture is not None:
            capture["url"] = req.full_url
        if exc is not None:
            raise exc
        return _Resp(body)
    monkeypatch.setattr("urllib.request.urlopen", fake)


@pytest.fixture(autouse=True)
def _isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RAILWAY_VOLUME_MOUNT_PATH", str(tmp_path))
    import importlib
    from weather import paths
    importlib.reload(paths)


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    # fetch_station retries on failure with real backoff (_RETRY_DELAYS_S);
    # the outage/missing-station tests below make it exhaust all retries, so
    # without this they'd burn ~7s each on real sleeps.
    monkeypatch.setattr(mx.time, "sleep", lambda _s: None)


@pytest.mark.parametrize("tx_tn", [
    "- - - - - - - - - -",                                                    # typical US station (no DSM TX/TN)
    "290.0 290.0 291.0 291.0 292.0 292.0 293.0 293.0 294.0 294.0",             # typical international station
], ids=["empty_tx_tn", "populated_tx_tn"])
def test_parse_ignores_tx_tn_either_way(monkeypatch, tx_tn):
    """_parse_kmz only reads TTT/E_TTT (see weather/mosmix_client.py) — TX/TN
    being all '-' or fully populated must parse identically either way. This
    is NOT a US-vs-international behavior difference (there isn't one); it's
    robustness to a KMZ shape variance DWD actually ships."""
    cap = {}
    _mock_urlopen(monkeypatch, _kmz_bytes("72503", tx_tn), capture=cap)
    fc = mx.fetch_station("72503")
    assert fc is not None
    assert fc.wmo == "72503"
    assert "72503" in cap["url"]
    assert len(fc.times) == 10
    assert fc.ttt_c[0] == pytest.approx(280.0 - 273.15)


@pytest.mark.parametrize("kind,expected", [("max", _EXPECT_MAX), ("min", _EXPECT_MIN)])
def test_daily_extreme_by_kind(monkeypatch, kind, expected):
    _mock_urlopen(monkeypatch, _kmz_bytes("72503", "- - - - - - - - - -"))
    fc = mx.fetch_station("72503")
    assert fc is not None
    m = mx.daily_extreme(fc, date(2026, 9, 28), "America/New_York", kind)
    assert m == (expected, _EXPECT_SIGMA)


def test_daily_extreme_day_not_covered_returns_none(monkeypatch):
    _mock_urlopen(monkeypatch, _kmz_bytes("72503", "- - - - - - - - - -"))
    fc = mx.fetch_station("72503")
    assert mx.daily_extreme(fc, date(2099, 1, 1), "America/New_York", "max") is None


def test_missing_station_returns_none(monkeypatch):
    import urllib.error
    _mock_urlopen(monkeypatch, None, exc=urllib.error.HTTPError("url", 404, "Not Found", {}, None))
    assert mx.fetch_station("99999") is None


def test_dwd_outage_returns_none_not_raises(monkeypatch):
    _mock_urlopen(monkeypatch, None, exc=TimeoutError("connection timed out"))
    assert mx.fetch_station("72503") is None


def test_empty_wmo_returns_none():
    assert mx.fetch_station("") is None
    assert mx.fetch_station(None) is None  # type: ignore[arg-type]


def test_cache_hit_skips_network(monkeypatch):
    _mock_urlopen(monkeypatch, _kmz_bytes("72503", "- - - - - - - - - -"))
    first = mx.fetch_station("72503")
    assert first is not None

    def fail(*a, **k):
        raise AssertionError("network hit on a warm cache")
    monkeypatch.setattr("urllib.request.urlopen", fail)

    second = mx.fetch_station("72503")
    assert second is not None
    assert second.wmo == first.wmo
    assert second.ttt_c == first.ttt_c


def test_parses_real_dwd_fixture():
    """A real KMZ pulled live from DWD 2026-09-28 (KLGA/72503) — catches drift
    in the real namespace/structure that a hand-built fixture can't, since a
    hand-built fixture only proves the parser agrees with its own assumptions.
    This is exactly how the V1.0 vs V1_0 namespace typo was caught: every
    hand-built-fixture test above passed while fetch_station() returned None
    for every real station."""
    data = (Path(__file__).parent / "fixtures" / "mosmix_klga_sample.kmz").read_bytes()
    fc = mx._parse_kmz(data)
    assert fc is not None
    assert fc.wmo == "72503"
    assert len(fc.times) > 100  # MOSMIX_L ships ~240 hourly steps
    assert len(fc.times) == len(fc.ttt_c) == len(fc.e_ttt_c)
    assert any(v is not None for v in fc.ttt_c)
    assert any(v is not None for v in fc.e_ttt_c)
    # sanity range: real September NYC temps, not a unit-conversion bug
    real_vals = [v for v in fc.ttt_c if v is not None]
    assert all(-10 < v < 45 for v in real_vals)


def test_stale_cache_is_evicted(monkeypatch):
    import time as _time
    _mock_urlopen(monkeypatch, _kmz_bytes("72503", "- - - - - - - - - -"))
    fc = mx.fetch_station("72503")
    assert fc is not None

    p = mx._cache_path("72503")
    old = _time.time() - mx._CACHE_TTL_S - 60
    import os
    os.utime(p, (old, old))

    cap = {}
    _mock_urlopen(monkeypatch, _kmz_bytes("72503", "- - - - - - - - - -"), capture=cap)
    fc2 = mx.fetch_station("72503")
    assert fc2 is not None
    assert "url" in cap  # network was hit again, proving the stale cache was skipped
