"""Local L2 order book maintained from Deribit snapshot/change messages."""
from mm_bot.feed.messages import BookChange, BookSnapshot


class GapError(Exception):
    """Sequence break: the local book no longer matches the exchange."""


class OrderBook:
    def __init__(self) -> None:
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self.change_id: int | None = None
        self.timestamp_ms: int | None = None

    @property
    def initialized(self) -> bool:
        return self.change_id is not None

    def apply_snapshot(self, snap: BookSnapshot) -> None:
        self._bids = dict(snap.bids)
        self._asks = dict(snap.asks)
        self.change_id = snap.change_id
        self.timestamp_ms = snap.timestamp_ms

    def apply_change(self, chg: BookChange) -> None:
        if not self.initialized:
            raise GapError("change received before snapshot")
        if chg.prev_change_id != self.change_id:
            raise GapError(
                f"expected prev_change_id {self.change_id}, got {chg.prev_change_id}"
            )
        for side, levels in ((self._bids, chg.bids), (self._asks, chg.asks)):
            for action, price, amount in levels:
                if action == "delete":
                    side.pop(price, None)
                else:  # "new" and "change" both set the level to the given amount
                    side[price] = amount
        self.change_id = chg.change_id
        self.timestamp_ms = chg.timestamp_ms

    def best_bid(self) -> float | None:
        return max(self._bids) if self._bids else None

    def best_ask(self) -> float | None:
        return min(self._asks) if self._asks else None

    def best_bid_size(self) -> float | None:
        bb = self.best_bid()
        return None if bb is None else self._bids[bb]

    def best_ask_size(self) -> float | None:
        ba = self.best_ask()
        return None if ba is None else self._asks[ba]

    def mid(self) -> float | None:
        bb, ba = self.best_bid(), self.best_ask()
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2

    def reset(self) -> None:
        self._bids.clear()
        self._asks.clear()
        self.change_id = None
        self.timestamp_ms = None
