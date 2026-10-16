"""Polymarket taker fee on these weather markets.

Measured 2026-10-15 from on-chain BUY activity of the live deposit wallet
(215 fills): the USD actually paid is shares * price + FEE_RATE * p * (1 - p)
per share (corr with p(1-p) = 1.00, slope 0.050; ~1.2c/share, ~2.4% of stake at
typical prices). Charged on BUY only; redemptions pay exactly $1/share. Neither
live_trades.csv nor paper PnL subtracts it, which is the whole gap between the
CSV's +$12.45 and the wallet's -$6.34 on closed positions.

Pure helper, imported by the evaluation gates under scripts/. Not yet used by the
trading path.
"""
from __future__ import annotations

FEE_RATE = 0.05


def fee_per_share(price: float) -> float:
    """Fee in USD per share bought at `price` (0 < price < 1)."""
    return FEE_RATE * price * (1.0 - price)


def taker_fee_usd(stake_usd: float, price: float) -> float:
    """Fee in USD on a BUY of `stake_usd` (shares * price) at `price`.
    shares = stake/price, so fee = stake * FEE_RATE * (1 - price)."""
    if not (0.0 < price < 1.0) or stake_usd <= 0:
        return 0.0
    return stake_usd * FEE_RATE * (1.0 - price)


def row_fee_usd(row: dict, stake_usd: float, price: float) -> float:
    """Fee for a logged trade row: the recorded `fee_usd` when the bot wrote one
    (rows resolved after the fee column was added), else recomputed from stake and
    price. Lets net PnL be computed uniformly over history that predates the
    column, without rewriting it."""
    raw = row.get("fee_usd")
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return taker_fee_usd(stake_usd, price)


def executable_cost(row: dict) -> float | None:
    """The price a BUY of this paper row would actually have paid: the traded
    side's ASK, recovered as p_win - edge_pp (edge_pp is p_win minus the ask,
    see SignalGenerator). PaperTrader logs entry_price at the MID, so scoring a
    paper row at entry_price assumes half a spread better than live fills get
    (~0.7c/share measured). Returns None when the row lacks model_p/edge_pp or
    the recovered value is not a valid price; callers fall back to entry_price.
    A recorded `exec_price` (written by PaperTrader since 2026-10-16) wins over
    the reconstruction.
    """
    try:
        recorded = float(row.get("exec_price") or 0.0)
        if 0.0 < recorded < 1.0:
            return recorded
    except (TypeError, ValueError):
        pass
    try:
        model_p = float(row["model_p"])
        edge = float(row["edge_pp"])
    except (KeyError, TypeError, ValueError):
        return None
    p_win = model_p if row.get("direction") == "YES" else 1.0 - model_p
    ask = p_win - edge
    return ask if 0.0 < ask < 1.0 else None


def fill_price(row: dict) -> float:
    """The price a logged paper row was (or is to be) scored at: the recorded
    executable ask when present and valid, else the logged entry_price (the mid,
    for rows written before exec_price existed)."""
    try:
        recorded = float(row.get("exec_price") or 0.0)
        if 0.0 < recorded < 1.0:
            return recorded
    except (TypeError, ValueError):
        pass
    return float(row["entry_price"])
