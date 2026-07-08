import asyncio
import json

import pytest

from mm_bot.config import FeedConfig
from mm_bot.feed.client import DeribitFeedClient
from mm_bot.feed.messages import BookChange, BookSnapshot, Ticker, Trade


class FeedClosed(Exception):
    """Raised by FakeWS when its scripted messages run out."""


class FakeWS:
    """Scripted websocket: yields queued messages, records everything sent."""

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, text):
        self.sent.append(json.loads(text))

    async def recv(self):
        if not self._incoming:
            raise FeedClosed()
        return json.dumps(self._incoming.pop(0))

    def sent_methods(self):
        return [m["method"] for m in self.sent]


def book_snapshot_msg(change_id=1000):
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "book.BTC-PERPETUAL.100ms",
            "data": {
                "type": "snapshot",
                "timestamp": 1751800000000,
                "instrument_name": "BTC-PERPETUAL",
                "change_id": change_id,
                "bids": [["new", 60000.0, 5000.0]],
                "asks": [["new", 60000.5, 4200.0]],
            },
        },
    }


def book_change_msg(change_id, prev_change_id):
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "book.BTC-PERPETUAL.100ms",
            "data": {
                "type": "change",
                "timestamp": 1751800000100,
                "instrument_name": "BTC-PERPETUAL",
                "change_id": change_id,
                "prev_change_id": prev_change_id,
                "bids": [["change", 60000.0, 4900.0]],
                "asks": [],
            },
        },
    }


def trades_msg():
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "trades.BTC-PERPETUAL.100ms",
            "data": [
                {
                    "instrument_name": "BTC-PERPETUAL",
                    "trade_id": "t1",
                    "trade_seq": 1,
                    "timestamp": 1751800000150,
                    "price": 60000.5,
                    "amount": 10.0,
                    "direction": "buy",
                }
            ],
        },
    }


def ticker_msg():
    return {
        "jsonrpc": "2.0",
        "method": "subscription",
        "params": {
            "channel": "ticker.BTC-PERPETUAL.100ms",
            "data": {
                "instrument_name": "BTC-PERPETUAL",
                "timestamp": 1751800000200,
                "funding_8h": 0.0001,
                "mark_price": 60000.3,
            },
        },
    }


def heartbeat_msg():
    return {"jsonrpc": "2.0", "method": "heartbeat", "params": {"type": "test_request"}}


def make_client(incoming, **cfg_overrides):
    cfg = FeedConfig(stale_data_timeout_s=1.0, **cfg_overrides)
    ws = FakeWS(incoming)
    events = []

    async def on_event(event):
        events.append(event)

    raw = []
    client = DeribitFeedClient(
        cfg, on_event, on_raw=raw.append, connect=lambda: ws
    )
    return client, ws, events, raw


async def run_session(client, ws):
    with pytest.raises(FeedClosed):
        async with ws:
            await client._session(ws)


async def test_session_sets_heartbeat_and_subscribes():
    client, ws, events, raw = make_client([])
    await run_session(client, ws)
    assert ws.sent_methods() == ["public/set_heartbeat", "public/subscribe"]
    assert ws.sent[0]["params"]["interval"] == 30
    assert ws.sent[1]["params"]["channels"] == [
        "book.BTC-PERPETUAL.100ms",
        "trades.BTC-PERPETUAL.100ms",
        "ticker.BTC-PERPETUAL.100ms",
    ]


async def test_events_dispatched_and_book_maintained():
    client, ws, events, raw = make_client(
        [book_snapshot_msg(), book_change_msg(1001, 1000), trades_msg()]
    )
    await run_session(client, ws)
    assert [type(e) for e in events] == [BookSnapshot, BookChange, Trade]
    assert client.book.best_bid() == 60000.0
    assert client.book.change_id == 1001
    assert len(raw) == 3  # every raw message hit the recorder hook


async def test_ticker_dispatched_to_on_event():
    client, ws, events, raw = make_client([ticker_msg()])
    await run_session(client, ws)
    assert [type(e) for e in events] == [Ticker]
    assert events[0].funding_8h == 0.0001
    assert events[0].mark_price == 60000.3


async def test_heartbeat_test_request_answered():
    client, ws, events, raw = make_client([heartbeat_msg()])
    await run_session(client, ws)
    assert "public/test" in ws.sent_methods()


async def test_gap_triggers_resubscribe_and_reset():
    client, ws, events, raw = make_client(
        [book_snapshot_msg(), book_change_msg(1005, 1004)]  # gap: prev 1004 != 1000
    )
    await run_session(client, ws)
    methods = ws.sent_methods()
    assert "public/unsubscribe" in methods
    assert methods.count("public/subscribe") == 2  # initial + after gap
    assert not client.book.initialized
    assert [type(e) for e in events] == [BookSnapshot]  # gapped change not dispatched


async def test_run_reconnects_after_failure():
    cfg = FeedConfig(
        stale_data_timeout_s=1.0,
        reconnect_initial_delay_s=0.01,
        reconnect_max_delay_s=0.02,
    )
    attempts = []

    def connect():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("refused")
        return FakeWS([book_snapshot_msg()])

    async def on_event(event):
        pass

    client = DeribitFeedClient(cfg, on_event, connect=connect)
    task = asyncio.create_task(client.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(attempts) >= 3:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(attempts) >= 3  # failed, connected, then reconnected after FeedClosed
