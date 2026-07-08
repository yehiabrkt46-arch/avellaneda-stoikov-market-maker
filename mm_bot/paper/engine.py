# mm_bot/paper/engine.py
"""Paper trading engine: routes feed events through strategy lanes.

One lane per strategy: strategy + fill simulator + portfolio + adverse
selection tracker, all sharing the single live order book. All timing uses
exchange timestamps from the messages, never the local clock.
"""
import logging

from mm_bot.config import StrategyConfig
from mm_bot.feed.book import OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, Ticker, Trade
from mm_bot.paper.adverse import AdverseSelectionTracker
from mm_bot.paper.portfolio import Portfolio
from mm_bot.paper.sim import FillSimulator
from mm_bot.risk import RiskManager
from mm_bot.store.db import Store
from mm_bot.strategy.base import QuotePair

log = logging.getLogger(__name__)

FUNDING_PERIOD_S = 28800.0  # 8 hours, the Deribit funding_8h reference period


def funding_accrual_btc(
    position_usd: float, mark: float, funding_8h: float, elapsed_s: float
) -> float:
    """BTC funding delta for one accrual interval.

    Sign convention: positive funding_8h means longs pay shorts. A long
    (positive position_usd) with positive funding loses BTC; a short
    (negative position_usd) with positive funding gains BTC.
    """
    return -position_usd / mark * funding_8h * (elapsed_s / FUNDING_PERIOD_S)


class StrategyLane:
    def __init__(
        self, strategy, cfg: StrategyConfig, store: Store, session_id: str,
        adverse_horizon_ms: int,
    ) -> None:
        self.strategy = strategy
        self.cfg = cfg
        self.portfolio = Portfolio()
        self.sim = FillSimulator(self._handle_fill)
        self.adverse = AdverseSelectionTracker(adverse_horizon_ms, store.set_adverse)
        self.risk = RiskManager(cfg, self._record_risk_event)
        self.quote_count = 0
        self.last_quote_ms: int | None = None
        self._store = store
        self._session_id = session_id
        self._current_mid: float | None = None

    def _record_risk_event(self, kind: str, detail: str, ts_ms: int) -> None:
        self._store.record_event(self._session_id, ts_ms, self.strategy.name, kind, detail)

    def _handle_fill(self, fill) -> None:
        self.portfolio.apply_fill(fill)
        fill_id = self._store.record_fill(
            self._session_id, fill.timestamp_ms, self.strategy.name,
            side=fill.side, price=fill.price, amount_usd=fill.amount_usd,
            trade_id=fill.trade_id, mid_at_fill=self._current_mid,
        )
        self.adverse.add_fill(
            ref=fill_id, side=fill.side, mid_at_fill=self._current_mid,
            ts_ms=fill.timestamp_ms,
        )

    def on_mid(self, mid: float, ts_ms: int) -> None:
        self.strategy.observe_mid(mid, ts_ms)
        self._current_mid = mid
        self.adverse.on_mid(mid, ts_ms)
        interval_ms = int(self.cfg.requote_interval_s * 1000)
        if self.last_quote_ms is None or ts_ms - self.last_quote_ms >= interval_ms:
            if self.risk.killed:
                q = QuotePair(bid=None, ask=None)
            else:
                q = self.strategy.quotes(mid, self.portfolio.position_usd, ts_ms)
                q = self.risk.filter_quotes(
                    q, self.portfolio.position_usd,
                    self.portfolio.equity_usd(mid), ts_ms,
                )
            self.sim.set_quotes(q.bid, q.ask, self.cfg.quote_size_usd)
            self._store.record_quote(
                self._session_id, ts_ms, self.strategy.name,
                q.bid, q.ask, self.cfg.quote_size_usd,
            )
            self.quote_count += 1
            self.last_quote_ms = ts_ms

    def on_trade(self, trade: Trade) -> None:
        if self._current_mid is None:
            return  # book not ready; never fill blind
        self.strategy.observe_trade(trade.price, trade.timestamp_ms)
        self.sim.on_trade(trade)

    def pull_stale_quotes(self, ts_ms: int) -> None:
        """Clear resting sim quotes after a feed gap so a stale price can't fill."""
        self.sim.set_quotes(None, None, 0)
        self._store.record_event(self._session_id, ts_ms, self.strategy.name, "stale_pull", None)

    def accrue_funding(self, funding_8h: float, mark: float, elapsed_s: float) -> None:
        self.portfolio.funding_btc += funding_accrual_btc(
            self.portfolio.position_usd, mark, funding_8h, elapsed_s
        )

    def rollup(self, ts_ms: int, mid: float) -> None:
        self._store.record_rollup(
            self._session_id, ts_ms, self.strategy.name,
            position_usd=self.portfolio.position_usd,
            btc_cash=self.portfolio.btc_cash,
            equity_btc=self.portfolio.equity_btc(mid),
            equity_usd=self.portfolio.equity_usd(mid),
            mid=mid,
            fill_count=self.portfolio.fill_count,
            quote_count=self.quote_count,
            funding_btc=self.portfolio.funding_btc,
        )


class PaperEngine:
    def __init__(
        self, book: OrderBook, lanes: list[StrategyLane], store: Store,
        session_id: str, rollup_interval_ms: int = 60_000,
        stale_quote_pull_ms: int = 10_000,
    ) -> None:
        self._book = book
        self.lanes = lanes
        self._store = store
        self._session_id = session_id
        self._rollup_interval_ms = rollup_interval_ms
        self._stale_quote_pull_ms = stale_quote_pull_ms
        self._last_rollup_ms: int | None = None
        self.last_event_ts: int | None = None
        self._latest_funding_8h: float | None = None
        self._latest_mark: float | None = None
        self.skipped_funding_intervals = 0

    async def on_event(self, event) -> None:
        ts_ms = getattr(event, "timestamp_ms", None)
        if ts_ms is not None:
            self._maybe_pull_stale_quotes(ts_ms)
            self.last_event_ts = ts_ms
        match event:
            case BookSnapshot() | BookChange():
                # in live mode the feed client has already applied the event
                # to the shared book; in replay apply_book_event did
                mid = self._book.mid()
                if mid is None:
                    return
                ts = self._book.timestamp_ms
                for lane in self.lanes:
                    lane.on_mid(mid, ts)
                self._maybe_rollup(ts, mid)
            case Trade():
                for lane in self.lanes:
                    lane.on_trade(event)
            case Ticker():
                self._latest_funding_8h = event.funding_8h
                self._latest_mark = event.mark_price

    def _maybe_pull_stale_quotes(self, ts_ms: int) -> None:
        if self.last_event_ts is None:
            return
        if ts_ms - self.last_event_ts > self._stale_quote_pull_ms:
            for lane in self.lanes:
                lane.pull_stale_quotes(ts_ms)

    def apply_book_event(self, event) -> None:
        """Replay helper: apply a book event when no feed client owns the book."""
        match event:
            case BookSnapshot():
                self._book.apply_snapshot(event)
            case BookChange():
                self._book.apply_change(event)

    def _maybe_rollup(self, ts_ms: int, mid: float) -> None:
        if self._last_rollup_ms is None:
            self._last_rollup_ms = ts_ms
            return
        elapsed_ms = ts_ms - self._last_rollup_ms
        if elapsed_ms >= self._rollup_interval_ms:
            self._accrue_funding(elapsed_ms / 1000.0)
            for lane in self.lanes:
                lane.rollup(ts_ms, mid)
            self._last_rollup_ms = ts_ms

    def _accrue_funding(self, elapsed_s: float) -> None:
        if self._latest_funding_8h is None or self._latest_mark is None:
            self.skipped_funding_intervals += 1
            log.info(
                "funding accrual skipped (%d total so far): no ticker received yet",
                self.skipped_funding_intervals,
            )
            return
        for lane in self.lanes:
            lane.accrue_funding(self._latest_funding_8h, self._latest_mark, elapsed_s)
