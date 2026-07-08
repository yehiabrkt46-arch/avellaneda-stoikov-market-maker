from mm_bot.feed.messages import (
    BookChange,
    BookSnapshot,
    TestRequest,
    Trade,
    parse_message,
)

BOOK_SNAPSHOT_MSG = {
    "jsonrpc": "2.0",
    "method": "subscription",
    "params": {
        "channel": "book.BTC-PERPETUAL.100ms",
        "data": {
            "type": "snapshot",
            "timestamp": 1751800000000,
            "instrument_name": "BTC-PERPETUAL",
            "change_id": 1000,
            "bids": [["new", 60000.0, 5000.0], ["new", 59999.5, 300.0]],
            "asks": [["new", 60000.5, 4200.0]],
        },
    },
}

BOOK_CHANGE_MSG = {
    "jsonrpc": "2.0",
    "method": "subscription",
    "params": {
        "channel": "book.BTC-PERPETUAL.100ms",
        "data": {
            "type": "change",
            "timestamp": 1751800000100,
            "instrument_name": "BTC-PERPETUAL",
            "change_id": 1001,
            "prev_change_id": 1000,
            "bids": [["change", 60000.0, 4500.0], ["delete", 59999.5, 0.0]],
            "asks": [["new", 60001.0, 100.0]],
        },
    },
}

TRADES_MSG = {
    "jsonrpc": "2.0",
    "method": "subscription",
    "params": {
        "channel": "trades.BTC-PERPETUAL.100ms",
        "data": [
            {
                "instrument_name": "BTC-PERPETUAL",
                "trade_id": "abc-1",
                "trade_seq": 42,
                "timestamp": 1751800000150,
                "price": 60000.5,
                "amount": 250.0,
                "direction": "buy",
            },
            {
                "instrument_name": "BTC-PERPETUAL",
                "trade_id": "abc-2",
                "trade_seq": 43,
                "timestamp": 1751800000151,
                "price": 60000.0,
                "amount": 100.0,
                "direction": "sell",
            },
        ],
    },
}

HEARTBEAT_TEST_REQUEST_MSG = {
    "jsonrpc": "2.0",
    "method": "heartbeat",
    "params": {"type": "test_request"},
}

RPC_RESPONSE_MSG = {"jsonrpc": "2.0", "id": 1, "result": ["book.BTC-PERPETUAL.100ms"]}


def test_parse_book_snapshot():
    events = parse_message(BOOK_SNAPSHOT_MSG)
    assert len(events) == 1
    snap = events[0]
    assert isinstance(snap, BookSnapshot)
    assert snap.instrument == "BTC-PERPETUAL"
    assert snap.change_id == 1000
    assert snap.timestamp_ms == 1751800000000
    assert snap.bids == [(60000.0, 5000.0), (59999.5, 300.0)]
    assert snap.asks == [(60000.5, 4200.0)]


def test_parse_book_change():
    events = parse_message(BOOK_CHANGE_MSG)
    assert len(events) == 1
    change = events[0]
    assert isinstance(change, BookChange)
    assert change.change_id == 1001
    assert change.prev_change_id == 1000
    assert change.bids == [("change", 60000.0, 4500.0), ("delete", 59999.5, 0.0)]
    assert change.asks == [("new", 60001.0, 100.0)]


def test_parse_trades():
    events = parse_message(TRADES_MSG)
    assert len(events) == 2
    assert all(isinstance(e, Trade) for e in events)
    assert events[0].trade_id == "abc-1"
    assert events[0].direction == "buy"
    assert events[1].price == 60000.0
    assert events[1].trade_seq == 43


def test_parse_heartbeat_test_request():
    events = parse_message(HEARTBEAT_TEST_REQUEST_MSG)
    assert events == [TestRequest()]


def test_parse_rpc_response_is_ignored():
    assert parse_message(RPC_RESPONSE_MSG) == []
