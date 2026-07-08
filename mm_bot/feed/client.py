"""Deribit websocket feed client: subscribe, heartbeat, reconnect, gap recovery."""
import asyncio
import json
import logging
import random

from mm_bot.config import FeedConfig
from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import (
    BookChange,
    BookSnapshot,
    TestRequest,
    Ticker,
    Trade,
    parse_message,
)

log = logging.getLogger(__name__)


class DeribitFeedClient:
    """Owns one market data subscription and a local order book.

    on_event: async callable receiving BookSnapshot | BookChange | Trade.
    on_raw: optional sync callable receiving every raw message dict (recorder hook).
    connect: injected factory returning an async-context-manager websocket;
             defaults to websockets.connect(ws_url). Tests inject a fake.
    """

    def __init__(self, config: FeedConfig, on_event, on_raw=None, connect=None):
        self._cfg = config
        self._on_event = on_event
        self._on_raw = on_raw
        self._connect = connect or self._default_connect
        self._req_id = 0
        self.book = OrderBook()

    def _default_connect(self):
        import websockets

        return websockets.connect(self._cfg.ws_url)

    async def run(self) -> None:
        """Connect and process messages forever, reconnecting on any failure.

        Any exception in the session (network drop, stale-data timeout, malformed
        message) tears the connection down; the book is reset and the next
        subscription starts from a fresh snapshot.
        """
        delay = self._cfg.reconnect_initial_delay_s
        while True:
            try:
                async with self._connect() as ws:
                    delay = self._cfg.reconnect_initial_delay_s
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("feed lost (%r), reconnecting in %.1fs", exc, delay)
                self.book.reset()
                await asyncio.sleep(delay * (1 + random.random() / 2))
                delay = min(delay * 2, self._cfg.reconnect_max_delay_s)

    async def _session(self, ws) -> None:
        await self._rpc(
            ws, "public/set_heartbeat", {"interval": self._cfg.heartbeat_interval_s}
        )
        await self._subscribe(ws, unsubscribe_first=False)
        while True:
            text = await asyncio.wait_for(
                ws.recv(), timeout=self._cfg.stale_data_timeout_s
            )
            msg = json.loads(text)
            if self._on_raw is not None:
                self._on_raw(msg)
            for event in parse_message(msg):
                await self._handle(ws, event)

    async def _handle(self, ws, event) -> None:
        match event:
            case TestRequest():
                await self._rpc(ws, "public/test", {})
            case BookSnapshot():
                self.book.apply_snapshot(event)
                await self._on_event(event)
            case BookChange():
                try:
                    self.book.apply_change(event)
                except GapError as exc:
                    log.warning("book gap (%s), resubscribing", exc)
                    self.book.reset()
                    await self._subscribe(ws, unsubscribe_first=True)
                    return
                await self._on_event(event)
            case Trade():
                await self._on_event(event)
            case Ticker():
                await self._on_event(event)

    async def _subscribe(self, ws, unsubscribe_first: bool) -> None:
        book_channel = f"book.{self._cfg.instrument}.{self._cfg.book_interval}"
        trades_channel = f"trades.{self._cfg.instrument}.{self._cfg.book_interval}"
        ticker_channel = f"ticker.{self._cfg.instrument}.{self._cfg.book_interval}"
        if unsubscribe_first:
            # only the book channel carries a sequence to resync; trades and
            # ticker have no gap concept and stay subscribed.
            await self._rpc(ws, "public/unsubscribe", {"channels": [book_channel]})
            await self._rpc(ws, "public/subscribe", {"channels": [book_channel]})
        else:
            await self._rpc(
                ws, "public/subscribe",
                {"channels": [book_channel, trades_channel, ticker_channel]},
            )

    async def _rpc(self, ws, method: str, params: dict) -> None:
        self._req_id += 1
        await ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
            )
        )
