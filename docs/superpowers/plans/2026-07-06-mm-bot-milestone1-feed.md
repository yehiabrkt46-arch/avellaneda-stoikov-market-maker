# mm-bot Milestone 1: Feed (WS Client, Order Book, Recorder) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A resilient asyncio client that maintains a live local L2 order book for Deribit BTC-PERPETUAL from mainnet public websocket data, records every raw message to JSONL for replay, and survives disconnects and sequence gaps.

**Architecture:** Single asyncio event loop. `DeribitFeedClient` owns the websocket session (subscribe, heartbeat, reconnect with backoff, gap-triggered resubscribe) and an `OrderBook` built from snapshot/change messages. Parsed typed events go to an async callback; every raw message goes to an append-only JSONL recorder. `run_recorder.py` wires these together and logs periodic health stats.

**Tech Stack:** Python 3.12, asyncio, `websockets`, PyYAML, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-07-06-mm-bot-design.md`. This plan covers build-order milestone 1 only. Milestones 2-6 get their own plans after this one is verified live.

**Deribit protocol facts the engineer needs (verify against https://docs.deribit.com if anything fails):**
- Endpoint: `wss://www.deribit.com/ws/api/v2`, JSON-RPC 2.0.
- Subscribe: method `public/subscribe`, params `{"channels": ["book.BTC-PERPETUAL.100ms", "trades.BTC-PERPETUAL.100ms"]}`.
- Notifications arrive as `{"method": "subscription", "params": {"channel": ..., "data": ...}}`.
- Book data: first message per subscription has `"type": "snapshot"`, later ones `"type": "change"`. Fields: `change_id`, `prev_change_id` (change only), `timestamp` (ms), `bids`/`asks` as lists of `[action, price, amount]` with action in `new`/`change`/`delete` (snapshot rows use `new`).
- Gap rule: a change's `prev_change_id` must equal the last applied `change_id`; otherwise the local book is invalid and the client must resubscribe (a fresh subscription re-sends a snapshot).
- Trades data: a list of objects with `instrument_name`, `trade_id`, `trade_seq`, `timestamp`, `price`, `amount`, `direction`.
- Heartbeat: request via `public/set_heartbeat` `{"interval": 30}`. Server then periodically sends `{"method": "heartbeat", "params": {"type": "test_request"}}` and the client must reply with a `public/test` call or Deribit drops the connection.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `config.yaml`
- Create: `mm_bot/__init__.py` (empty)
- Create: `mm_bot/feed/__init__.py` (empty)
- Create: `tests/__init__.py` (empty)

- [ ] **Step 1: Check Python version**

Run: `python --version`
Expected: `Python 3.12.x` or newer. If older, stop and report; do not work around it.

- [ ] **Step 2: Write pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "mm-bot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "websockets>=12.0",
    "PyYAML>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[tool.setuptools]
packages = ["mm_bot", "mm_bot.feed"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Write .gitignore**

```gitignore
.venv/
__pycache__/
*.pyc
data/
*.egg-info/
```

- [ ] **Step 4: Write config.yaml**

```yaml
feed:
  instrument: BTC-PERPETUAL
recorder:
  data_dir: data
```

- [ ] **Step 5: Create empty package files**

Create `mm_bot/__init__.py`, `mm_bot/feed/__init__.py`, `tests/__init__.py`, all empty.

- [ ] **Step 6: Create venv and install**

Run (Windows PowerShell):
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```
Expected: install succeeds, no errors.

- [ ] **Step 7: Verify pytest runs**

Run: `.venv\Scripts\python -m pytest`
Expected: `no tests ran` (exit code 5). That is success at this stage.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .gitignore config.yaml mm_bot tests
git commit -m "chore: project scaffold (package layout, deps, pytest)"
```

---

### Task 2: Config loader

**Files:**
- Create: `mm_bot/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config.py
from mm_bot.config import load_config


def test_load_config_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg.feed.ws_url == "wss://www.deribit.com/ws/api/v2"
    assert cfg.feed.instrument == "BTC-PERPETUAL"
    assert cfg.feed.book_interval == "100ms"
    assert cfg.feed.heartbeat_interval_s == 30
    assert cfg.feed.stale_data_timeout_s == 10.0
    assert cfg.feed.reconnect_initial_delay_s == 1.0
    assert cfg.feed.reconnect_max_delay_s == 60.0
    assert cfg.recorder.data_dir == "data"


def test_load_config_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "feed:\n  instrument: ETH-PERPETUAL\n  stale_data_timeout_s: 5.5\n"
        "recorder:\n  data_dir: otherdir\n"
    )
    cfg = load_config(p)
    assert cfg.feed.instrument == "ETH-PERPETUAL"
    assert cfg.feed.stale_data_timeout_s == 5.5
    assert cfg.recorder.data_dir == "otherdir"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.config'`

- [ ] **Step 3: Write the implementation**

```python
# mm_bot/config.py
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class FeedConfig:
    ws_url: str = "wss://www.deribit.com/ws/api/v2"
    instrument: str = "BTC-PERPETUAL"
    book_interval: str = "100ms"
    heartbeat_interval_s: int = 30
    stale_data_timeout_s: float = 10.0
    reconnect_initial_delay_s: float = 1.0
    reconnect_max_delay_s: float = 60.0


@dataclass(frozen=True)
class RecorderConfig:
    data_dir: str = "data"


@dataclass(frozen=True)
class Config:
    feed: FeedConfig
    recorder: RecorderConfig


def load_config(path: str | Path) -> Config:
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return Config(
        feed=FeedConfig(**raw.get("feed", {})),
        recorder=RecorderConfig(**raw.get("recorder", {})),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_config.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add mm_bot/config.py tests/test_config.py
git commit -m "feat: YAML config loader with typed defaults"
```

---

### Task 3: Message parsing

**Files:**
- Create: `mm_bot/feed/messages.py`
- Test: `tests/test_messages.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_messages.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_messages.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.feed.messages'`

- [ ] **Step 3: Write the implementation**

```python
# mm_bot/feed/messages.py
"""Typed events parsed from raw Deribit websocket JSON messages."""
from dataclasses import dataclass


@dataclass(frozen=True)
class BookSnapshot:
    instrument: str
    change_id: int
    timestamp_ms: int
    bids: list[tuple[float, float]]  # (price, amount)
    asks: list[tuple[float, float]]


@dataclass(frozen=True)
class BookChange:
    instrument: str
    change_id: int
    prev_change_id: int
    timestamp_ms: int
    bids: list[tuple[str, float, float]]  # (action, price, amount)
    asks: list[tuple[str, float, float]]


@dataclass(frozen=True)
class Trade:
    instrument: str
    trade_id: str
    trade_seq: int
    timestamp_ms: int
    price: float
    amount: float
    direction: str  # "buy" or "sell"


@dataclass(frozen=True)
class TestRequest:
    """Server heartbeat challenge; client must answer with public/test."""


def parse_message(msg: dict) -> list:
    """Parse one raw Deribit WS message into zero or more typed events.

    RPC responses and unknown methods parse to []. Raising on malformed
    subscription data is intentional: the session handler treats it as a
    connection-level failure and reconnects.
    """
    method = msg.get("method")
    if method == "heartbeat":
        if msg["params"]["type"] == "test_request":
            return [TestRequest()]
        return []
    if method != "subscription":
        return []
    channel = msg["params"]["channel"]
    data = msg["params"]["data"]
    if channel.startswith("book."):
        instrument = channel.split(".")[1]
        if data["type"] == "snapshot":
            return [
                BookSnapshot(
                    instrument=instrument,
                    change_id=data["change_id"],
                    timestamp_ms=data["timestamp"],
                    bids=[(p, a) for _, p, a in data["bids"]],
                    asks=[(p, a) for _, p, a in data["asks"]],
                )
            ]
        return [
            BookChange(
                instrument=instrument,
                change_id=data["change_id"],
                prev_change_id=data["prev_change_id"],
                timestamp_ms=data["timestamp"],
                bids=[(a, p, s) for a, p, s in data["bids"]],
                asks=[(a, p, s) for a, p, s in data["asks"]],
            )
        ]
    if channel.startswith("trades."):
        return [
            Trade(
                instrument=t["instrument_name"],
                trade_id=t["trade_id"],
                trade_seq=t["trade_seq"],
                timestamp_ms=t["timestamp"],
                price=t["price"],
                amount=t["amount"],
                direction=t["direction"],
            )
            for t in data
        ]
    return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_messages.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add mm_bot/feed/messages.py tests/test_messages.py
git commit -m "feat: parse Deribit WS messages into typed events"
```

---

### Task 4: Order book

**Files:**
- Create: `mm_bot/feed/book.py`
- Test: `tests/test_book.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_book.py
import pytest

from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot


def snapshot():
    return BookSnapshot(
        instrument="BTC-PERPETUAL",
        change_id=1000,
        timestamp_ms=1751800000000,
        bids=[(60000.0, 5000.0), (59999.5, 300.0)],
        asks=[(60000.5, 4200.0), (60001.0, 900.0)],
    )


def change(change_id, prev_change_id, bids=(), asks=()):
    return BookChange(
        instrument="BTC-PERPETUAL",
        change_id=change_id,
        prev_change_id=prev_change_id,
        timestamp_ms=1751800000100,
        bids=list(bids),
        asks=list(asks),
    )


def test_snapshot_initializes_book():
    book = OrderBook()
    assert not book.initialized
    book.apply_snapshot(snapshot())
    assert book.initialized
    assert book.best_bid() == 60000.0
    assert book.best_ask() == 60000.5
    assert book.mid() == 60000.25
    assert book.change_id == 1000


def test_change_updates_levels():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.apply_change(
        change(
            1001,
            1000,
            bids=[("change", 60000.0, 4500.0), ("delete", 59999.5, 0.0)],
            asks=[("new", 60000.4, 50.0)],
        )
    )
    assert book.best_bid() == 60000.0
    assert book.best_ask() == 60000.4
    assert book.change_id == 1001


def test_delete_best_bid_promotes_next_level():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.apply_change(change(1001, 1000, bids=[("delete", 60000.0, 0.0)]))
    assert book.best_bid() == 59999.5


def test_gap_raises():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    with pytest.raises(GapError):
        book.apply_change(change(1002, 1001))  # skips change_id 1001


def test_change_before_snapshot_raises():
    book = OrderBook()
    with pytest.raises(GapError):
        book.apply_change(change(1001, 1000))


def test_reset_clears_book():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.reset()
    assert not book.initialized
    assert book.best_bid() is None
    assert book.best_ask() is None
    assert book.mid() is None


def test_snapshot_after_reset_reinitializes():
    book = OrderBook()
    book.apply_snapshot(snapshot())
    book.reset()
    book.apply_snapshot(snapshot())
    assert book.best_bid() == 60000.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_book.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.feed.book'`

- [ ] **Step 3: Write the implementation**

```python
# mm_bot/feed/book.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_book.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add mm_bot/feed/book.py tests/test_book.py
git commit -m "feat: L2 order book with gap detection"
```

---

### Task 5: JSONL recorder

**Files:**
- Create: `mm_bot/feed/recorder.py`
- Test: `tests/test_recorder.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_recorder.py
import json

from mm_bot.feed.recorder import JsonlRecorder


def test_records_messages_as_jsonl_lines(tmp_path):
    rec = JsonlRecorder(tmp_path, "20260706-120000")
    rec.record({"a": 1})
    rec.record({"b": [1, 2]})
    rec.close()
    lines = rec.path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(l) for l in lines] == [{"a": 1}, {"b": [1, 2]}]


def test_filename_contains_session_id(tmp_path):
    rec = JsonlRecorder(tmp_path, "20260706-120000")
    rec.close()
    assert rec.path.name == "raw-20260706-120000.jsonl"


def test_creates_data_dir(tmp_path):
    rec = JsonlRecorder(tmp_path / "nested" / "dir", "s1")
    rec.record({"x": 1})
    rec.close()
    assert rec.path.exists()


def test_flush_makes_lines_visible_before_close(tmp_path):
    rec = JsonlRecorder(tmp_path, "s2")
    rec.record({"x": 1})
    rec.flush()
    assert rec.path.read_text(encoding="utf-8").strip() == '{"x":1}'
    rec.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.feed.recorder'`

- [ ] **Step 3: Write the implementation**

```python
# mm_bot/feed/recorder.py
"""Append-only raw message recorder, one JSONL file per session."""
import json
from pathlib import Path


class JsonlRecorder:
    def __init__(self, data_dir: str | Path, session_id: str) -> None:
        self.path = Path(data_dir) / f"raw-{session_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8")

    def record(self, msg: dict) -> None:
        self._fh.write(json.dumps(msg, separators=(",", ":")) + "\n")

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_recorder.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add mm_bot/feed/recorder.py tests/test_recorder.py
git commit -m "feat: append-only JSONL raw message recorder"
```

---

### Task 6: Websocket feed client

**Files:**
- Create: `mm_bot/feed/client.py`
- Test: `tests/test_client.py`

The client is built for testability: the websocket connection factory is injected, so
tests drive it with a fake connection and never touch the network.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_client.py
import asyncio
import json

import pytest

from mm_bot.config import FeedConfig
from mm_bot.feed.client import DeribitFeedClient
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python -m pytest tests/test_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.feed.client'`

- [ ] **Step 3: Write the implementation**

```python
# mm_bot/feed/client.py
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

    async def _subscribe(self, ws, unsubscribe_first: bool) -> None:
        book_channel = f"book.{self._cfg.instrument}.{self._cfg.book_interval}"
        trades_channel = f"trades.{self._cfg.instrument}.{self._cfg.book_interval}"
        if unsubscribe_first:
            await self._rpc(ws, "public/unsubscribe", {"channels": [book_channel]})
            await self._rpc(ws, "public/subscribe", {"channels": [book_channel]})
        else:
            await self._rpc(
                ws, "public/subscribe", {"channels": [book_channel, trades_channel]}
            )

    async def _rpc(self, ws, method: str, params: dict) -> None:
        self._req_id += 1
        await ws.send(
            json.dumps(
                {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
            )
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python -m pytest tests/test_client.py -v`
Expected: 5 passed

- [ ] **Step 5: Run the full suite**

Run: `.venv\Scripts\python -m pytest`
Expected: all tests pass (config 2, messages 5, book 7, recorder 4, client 5 = 23)

- [ ] **Step 6: Commit**

```bash
git add mm_bot/feed/client.py tests/test_client.py
git commit -m "feat: websocket feed client with reconnect, heartbeat, gap recovery"
```

---

### Task 7: Recorder runner entrypoint

**Files:**
- Create: `run_recorder.py`

No unit test for the wiring script itself; it is verified by the live smoke run in
Task 8. Keep it thin: everything with logic already has tests.

- [ ] **Step 1: Write the runner**

```python
# run_recorder.py
"""Milestone 1 entrypoint: maintain a live book and record raw messages.

Usage: python run_recorder.py [config.yaml]
"""
import asyncio
import logging
import sys
import time

from mm_bot.config import load_config
from mm_bot.feed.client import DeribitFeedClient
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade
from mm_bot.feed.recorder import JsonlRecorder

log = logging.getLogger("run_recorder")

REPORT_INTERVAL_S = 60


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = load_config(sys.argv[1] if len(sys.argv) > 1 else "config.yaml")
    session_id = time.strftime("%Y%m%d-%H%M%S")
    recorder = JsonlRecorder(cfg.recorder.data_dir, session_id)
    stats = {"snapshots": 0, "changes": 0, "trades": 0}

    async def on_event(event) -> None:
        match event:
            case BookSnapshot():
                stats["snapshots"] += 1
            case BookChange():
                stats["changes"] += 1
            case Trade():
                stats["trades"] += 1

    client = DeribitFeedClient(cfg.feed, on_event, on_raw=recorder.record)

    async def report() -> None:
        while True:
            await asyncio.sleep(REPORT_INTERVAL_S)
            book = client.book
            log.info(
                "stats=%s best_bid=%s best_ask=%s mid=%s book_ts=%s",
                stats,
                book.best_bid(),
                book.best_ask(),
                book.mid(),
                book.timestamp_ms,
            )
            recorder.flush()

    log.info("recording session %s to %s", session_id, recorder.path)
    try:
        await asyncio.gather(client.run(), report())
    finally:
        recorder.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `.venv\Scripts\python -c "import run_recorder"`
Expected: no output, exit 0.

- [ ] **Step 3: Commit**

```bash
git add run_recorder.py
git commit -m "feat: recorder runner entrypoint with periodic health stats"
```

---

### Task 8: Live verification (Fable, not a subagent)

This task is performed by the main session (Fable) as final verification, per the
project's orchestration rule. Subagents stop after Task 7.

- [ ] **Step 1: 90-second live smoke run**

Run: `.venv\Scripts\python run_recorder.py` for ~90 seconds, then stop it.
Expected:
- At least one `stats=` log line with non-zero snapshots and changes, plausible
  best_bid/best_ask (bid below ask, both near the current BTC price), non-null mid.
- `data/raw-<session>.jsonl` exists and every line parses as JSON.
- No unhandled tracebacks (reconnect warnings are acceptable).

- [ ] **Step 2: Sanity-check the recording**

Run a check that the recorded book messages chain correctly:
```powershell
.venv\Scripts\python -c "
import glob, json
counts = {}
prev = None
gaps = 0
path = sorted(glob.glob('data/raw-*.jsonl'))[-1]
for line in open(path, encoding='utf-8'):
    m = json.loads(line)
    if m.get('method') != 'subscription':
        continue
    ch = m['params']['channel']
    kind = ch.split('.')[0]
    counts[kind] = counts.get(kind, 0) + 1
    if ch.startswith('book.'):
        d = m['params']['data']
        if d['type'] == 'change' and prev is not None and d['prev_change_id'] != prev:
            gaps += 1
        prev = d['change_id']
print('counts:', counts, 'gaps in raw stream:', gaps)
"
```
Expected: non-zero book and trades counts; `gaps in raw stream: 0` for an
uninterrupted run. Gaps equal to the number of reconnects are acceptable and must
match reconnect warnings in the log.

- [ ] **Step 3: Overnight run**

Start `run_recorder.py` before end of day, leave running overnight (local PC fine for
this milestone; VPS deploy is milestone 4). Next session: rerun the Step 2 check on
the overnight file, confirm stats lines appear every minute throughout, count
reconnects.

- [ ] **Step 4: Tag milestone after overnight check passes**

```bash
git tag milestone-1-feed
```

---

## Execution notes

- Sonnet 5 subagents implement Tasks 1-7 (per-task dispatch, code plus tests). Fable
  reviews each diff between tasks and performs Task 8 itself.
- Windows dev commands shown (`.venv\Scripts\python`); on the VPS (Linux, milestone 4)
  the equivalent is `.venv/bin/python`.
- If live Deribit message shapes differ from the samples in Task 3 tests, trust the
  live data: fix the parser and tests, and note the discrepancy in the commit message.
