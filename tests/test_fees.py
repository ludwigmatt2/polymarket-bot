"""weather/fees.py — measured Polymarket taker fee (0.05 * p * (1-p) per share)."""
from weather.fees import FEE_RATE, fee_per_share, taker_fee_usd


def test_fee_peaks_at_half_and_vanishes_at_extremes():
    assert abs(fee_per_share(0.5) - 0.0125) < 1e-12
    assert fee_per_share(0.5) > fee_per_share(0.2) > fee_per_share(0.05)
    assert fee_per_share(0.01) < 0.0005


def test_usd_fee_matches_share_formula():
    stake, price = 10.0, 0.65
    shares = stake / price
    assert abs(taker_fee_usd(stake, price) - shares * fee_per_share(price)) < 1e-12
    assert abs(taker_fee_usd(stake, price) - stake * FEE_RATE * (1 - price)) < 1e-12


def test_degenerate_inputs_are_free():
    assert taker_fee_usd(0.0, 0.5) == 0.0
    assert taker_fee_usd(5.0, 0.0) == 0.0
    assert taker_fee_usd(5.0, 1.0) == 0.0
