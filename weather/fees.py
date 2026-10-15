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
