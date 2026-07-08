# mm_bot/paper/portfolio.py
"""Inverse-perpetual position and P&L accounting (Deribit BTC-PERPETUAL).

Contracts are USD-denominated, P&L settles in BTC. Long N contracts (10 USD
each) entered at p1 and exited at p2 earns 10*N*(1/p1 - 1/p2) BTC. The
bookkeeping below reproduces that for any fill sequence:
a buy of `usd` notional at price p adds +usd to position_usd and +usd/p to
btc_cash; equity_btc(mark) = btc_cash + funding_btc - position_usd/mark.

funding_btc is the cumulative perpetual funding accrued against this
position (see mm_bot.paper.engine's accrual formula); it starts at 0.0 and
only changes once a live ticker feeds a funding rate, so equity is
unaffected for any caller that never touches it.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Fill:
    timestamp_ms: int
    side: str  # "buy" or "sell" (our side of the fill)
    price: float
    amount_usd: float
    trade_id: str


class Portfolio:
    def __init__(self) -> None:
        self.position_usd = 0.0
        self.btc_cash = 0.0
        self.funding_btc = 0.0
        self.fill_count = 0

    def apply_fill(self, fill: Fill) -> None:
        sign = 1.0 if fill.side == "buy" else -1.0
        self.position_usd += sign * fill.amount_usd
        self.btc_cash += sign * fill.amount_usd / fill.price
        self.fill_count += 1

    def equity_btc(self, mark: float) -> float:
        return self.btc_cash + self.funding_btc - self.position_usd / mark

    def equity_usd(self, mark: float) -> float:
        return self.equity_btc(mark) * mark
