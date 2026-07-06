# mm_bot/paper/sim.py
"""Conservative fill simulator.

A resting quote fills only when a real printed trade STRICTLY crosses it:
trade price below our bid fills the bid, trade price above our ask fills the
ask. Fill price is our quote price (maker). Fill size is capped by both the
printed trade amount and the quote's remaining size. No at-touch fills, no
queue-position modeling; this understates fill rate, which is the documented
conservative bias of every reported number.
"""
from mm_bot.feed.messages import Trade
from mm_bot.paper.portfolio import Fill


class FillSimulator:
    def __init__(self, on_fill) -> None:
        self._on_fill = on_fill  # sync callable(Fill)
        self._bid: tuple[float, float] | None = None  # (price, remaining_usd)
        self._ask: tuple[float, float] | None = None

    def set_quotes(
        self, bid_price: float | None, ask_price: float | None, size_usd: float
    ) -> None:
        self._bid = (bid_price, size_usd) if bid_price is not None else None
        self._ask = (ask_price, size_usd) if ask_price is not None else None

    @property
    def bid(self) -> tuple[float, float] | None:
        return self._bid

    @property
    def ask(self) -> tuple[float, float] | None:
        return self._ask

    def on_trade(self, trade: Trade) -> None:
        if self._bid is not None and trade.price < self._bid[0]:
            price, remaining = self._bid
            qty = min(remaining, trade.amount)
            remaining -= qty
            self._bid = (price, remaining) if remaining > 0 else None
            self._on_fill(
                Fill(
                    timestamp_ms=trade.timestamp_ms,
                    side="buy",
                    price=price,
                    amount_usd=qty,
                    trade_id=trade.trade_id,
                )
            )
        if self._ask is not None and trade.price > self._ask[0]:
            price, remaining = self._ask
            qty = min(remaining, trade.amount)
            remaining -= qty
            self._ask = (price, remaining) if remaining > 0 else None
            self._on_fill(
                Fill(
                    timestamp_ms=trade.timestamp_ms,
                    side="sell",
                    price=price,
                    amount_usd=qty,
                    trade_id=trade.trade_id,
                )
            )
