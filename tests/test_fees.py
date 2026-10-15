"""weather/fees.py — measured Polymarket taker fee (0.05 * p * (1-p) per share)."""
from weather.fees import FEE_RATE, executable_cost, fee_per_share, taker_fee_usd


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


def test_executable_cost_recovers_the_ask_from_edge():
    # YES: p_win = model_p. ask = 0.60 - 0.12 = 0.48
    assert abs(executable_cost({"model_p": "0.60", "edge_pp": "0.12", "direction": "YES"}) - 0.48) < 1e-12
    # NO: p_win = 1 - model_p = 0.65; ask = 0.65 - 0.09 = 0.56
    assert abs(executable_cost({"model_p": "0.35", "edge_pp": "0.09", "direction": "NO"}) - 0.56) < 1e-12


def test_executable_cost_none_when_unrecoverable():
    assert executable_cost({"direction": "NO"}) is None
    assert executable_cost({"model_p": "", "edge_pp": "0.1", "direction": "NO"}) is None
    assert executable_cost({"model_p": "0.9", "edge_pp": "-0.5", "direction": "YES"}) is None  # ask > 1
