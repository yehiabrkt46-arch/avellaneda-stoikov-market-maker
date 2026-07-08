# mm-bot Milestone 5 (code) + Milestone 4 (prep): Testnet Connector and Deploy Files

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deribit testnet execution connector (auth, place/cancel/amend, rate-limit handling, fill tracking) fully coded and unit-tested offline, plus a supervised demo script (run needs API keys, later), plus pm2/VPS deploy files for milestone 4.

**Architecture:** `RpcClient` adds request/response matching over the websocket (JSON-RPC ids -> futures), unlike the feed client's fire-and-forget. `TestnetExecClient` layers auth (client-credentials, re-auth before expiry), order operations with rate-limit retry, and order/trade update channels on top. Credentials come from environment variables only, never config files or git.

**Deribit testnet facts:**
- Endpoint `wss://test.deribit.com/ws/api/v2`, same JSON-RPC 2.0 protocol as mainnet.
- Auth: `public/auth` with `{"grant_type": "client_credentials", "client_id": ..., "client_secret": ...}` -> result has `access_token`, `expires_in` (seconds). After auth, the CONNECTION is authenticated; private methods work without re-passing the token. Re-auth when close to expiry.
- Orders: `private/buy` / `private/sell` with `{"instrument_name", "amount" (USD, multiple of 10), "type": "limit", "price", "post_only": true}` -> result `{"order": {"order_id", "order_state", ...}}`. `private/cancel` with `{"order_id"}`. `private/edit` with `{"order_id", "amount", "price"}`.
- Private subscriptions: channels `user.orders.BTC-PERPETUAL.raw` (order state changes) and `user.trades.BTC-PERPETUAL.raw` (own fills, each with `amount`, `price`, `order_id`) via `private/subscribe`.
- Errors: response contains `"error": {"code", "message"}` instead of `"result"`. Code 10028 = too_many_requests -> retry with backoff. Code 13009 = unauthorized -> re-auth once, retry.

---

### Task 1: RPC layer with response matching + auth

**Files:**
- Create: `mm_bot/exec_testnet/__init__.py` (empty)
- Create: `mm_bot/exec_testnet/rpc.py`
- Test: `tests/test_exec_rpc.py`
- Modify: `pyproject.toml` (add `"mm_bot.exec_testnet"` to packages)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_exec_rpc.py
import asyncio
import json

import pytest

from mm_bot.exec_testnet.rpc import DeribitRpcError, RpcClient


class FakeWS:
    """Scripted responder: maps method -> result or error for each request."""

    def __init__(self, script):
        self.script = script  # dict: method -> list of responses (popped in order)
        self.sent = []
        self._queue = asyncio.Queue()

    async def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        responses = self.script.get(msg["method"])
        if responses:
            body = responses.pop(0)
            reply = {"jsonrpc": "2.0", "id": msg["id"]}
            reply.update(body)
            await self._queue.put(json.dumps(reply))

    async def recv(self):
        return await self._queue.get()

    def push_notification(self, msg):
        self._queue.put_nowait(json.dumps(msg))


async def run_with_pump(client, coro):
    """Run coro while the client's reader loop pumps the fake socket."""
    pump = asyncio.create_task(client.read_loop())
    try:
        return await asyncio.wait_for(coro, timeout=2)
    finally:
        pump.cancel()


async def test_call_matches_response_by_id():
    ws = FakeWS({"public/test": [{"result": {"version": "1.2.3"}}]})
    client = RpcClient(ws)
    result = await run_with_pump(client, client.call("public/test", {}))
    assert result == {"version": "1.2.3"}
    assert ws.sent[0]["method"] == "public/test"


async def test_error_response_raises():
    ws = FakeWS({"private/buy": [{"error": {"code": 10009, "message": "not_enough_funds"}}]})
    client = RpcClient(ws)
    with pytest.raises(DeribitRpcError) as e:
        await run_with_pump(client, client.call("private/buy", {}))
    assert e.value.code == 10009


async def test_concurrent_calls_do_not_cross_wires():
    ws = FakeWS({
        "a": [{"result": "ra"}],
        "b": [{"result": "rb"}],
    })
    client = RpcClient(ws)

    async def both():
        return await asyncio.gather(client.call("a", {}), client.call("b", {}))

    ra, rb = await run_with_pump(client, both())
    assert (ra, rb) == ("ra", "rb")


async def test_notifications_go_to_handler():
    ws = FakeWS({"public/test": [{"result": {}}]})
    seen = []
    client = RpcClient(ws, on_notification=seen.append)
    ws.push_notification({"jsonrpc": "2.0", "method": "subscription",
                          "params": {"channel": "user.trades.BTC-PERPETUAL.raw", "data": []}})
    await run_with_pump(client, client.call("public/test", {}))
    assert len(seen) == 1
    assert seen[0]["params"]["channel"] == "user.trades.BTC-PERPETUAL.raw"


async def test_auth_sends_credentials_and_tracks_expiry():
    ws = FakeWS({"public/auth": [
        {"result": {"access_token": "tok", "expires_in": 900}},
    ]})
    client = RpcClient(ws)
    await run_with_pump(client, client.authenticate("cid", "csecret", now_s=1000.0))
    auth_req = ws.sent[0]
    assert auth_req["method"] == "public/auth"
    assert auth_req["params"] == {
        "grant_type": "client_credentials",
        "client_id": "cid",
        "client_secret": "csecret",
    }
    assert client.auth_expires_at_s == 1900.0
    assert client.authenticated(now_s=1000.0)
    assert not client.authenticated(now_s=1850.0)  # within 60s safety margin
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_exec_rpc.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# mm_bot/exec_testnet/rpc.py
"""JSON-RPC layer for Deribit private API: response matching and auth.

Unlike the feed client (fire-and-forget subscriptions), order operations need
request/response pairing: each call() awaits the response with the matching id.
Credentials are passed in by the caller (from environment variables); this
module never reads config files.
"""
import asyncio
import json


class DeribitRpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"deribit rpc error {code}: {message}")
        self.code = code
        self.message = message


AUTH_SAFETY_MARGIN_S = 60.0


class RpcClient:
    def __init__(self, ws, on_notification=None) -> None:
        self._ws = ws
        self._on_notification = on_notification  # sync callable(raw dict)
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self.auth_expires_at_s: float | None = None

    async def call(self, method: str, params: dict):
        self._req_id += 1
        req_id = self._req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        ))
        try:
            return await fut
        finally:
            self._pending.pop(req_id, None)

    async def read_loop(self) -> None:
        """Pump incoming messages: resolve pending calls, route notifications."""
        while True:
            msg = json.loads(await self._ws.recv())
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._pending:
                fut = self._pending[msg_id]
                if fut.done():
                    continue
                if "error" in msg:
                    err = msg["error"]
                    fut.set_exception(DeribitRpcError(err["code"], err["message"]))
                else:
                    fut.set_result(msg.get("result"))
            elif self._on_notification is not None:
                self._on_notification(msg)

    async def authenticate(self, client_id: str, client_secret: str, now_s: float) -> None:
        result = await self.call("public/auth", {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        })
        self.auth_expires_at_s = now_s + result["expires_in"]

    def authenticated(self, now_s: float) -> bool:
        if self.auth_expires_at_s is None:
            return False
        return now_s < self.auth_expires_at_s - AUTH_SAFETY_MARGIN_S
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_exec_rpc.py -v` — expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add mm_bot/exec_testnet tests/test_exec_rpc.py pyproject.toml
git commit -m "feat: testnet RPC layer with response matching and auth"
```

---

### Task 2: Order operations with rate-limit retry and fill tracking

**Files:**
- Create: `mm_bot/exec_testnet/orders.py`
- Test: `tests/test_exec_orders.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_exec_orders.py
import asyncio

import pytest

from mm_bot.exec_testnet.orders import OrderTracker, TestnetExecClient
from mm_bot.exec_testnet.rpc import DeribitRpcError


class FakeRpc:
    """Scripted RpcClient stand-in: method -> list of results/exceptions."""

    def __init__(self, script):
        self.script = script
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        responses = self.script[method]
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def order_result(order_id="o1", state="open"):
    return {"order": {"order_id": order_id, "order_state": state}}


async def test_place_limit_sends_post_only_and_returns_order_id():
    rpc = FakeRpc({"private/buy": [order_result("o42")]})
    ex = TestnetExecClient(rpc, instrument="BTC-PERPETUAL")
    order_id = await ex.place_limit("buy", price=50000.0, amount_usd=10.0)
    assert order_id == "o42"
    method, params = rpc.calls[0]
    assert method == "private/buy"
    assert params == {
        "instrument_name": "BTC-PERPETUAL",
        "amount": 10.0,
        "type": "limit",
        "price": 50000.0,
        "post_only": True,
    }


async def test_sell_uses_private_sell():
    rpc = FakeRpc({"private/sell": [order_result("o7")]})
    ex = TestnetExecClient(rpc, instrument="BTC-PERPETUAL")
    assert await ex.place_limit("sell", price=70000.0, amount_usd=20.0) == "o7"


async def test_cancel_and_edit_pass_order_id():
    rpc = FakeRpc({
        "private/cancel": [order_result("o1", "cancelled")],
        "private/edit": [order_result("o1", "open")],
    })
    ex = TestnetExecClient(rpc, instrument="BTC-PERPETUAL")
    await ex.cancel("o1")
    await ex.edit("o1", price=50001.0, amount_usd=10.0)
    assert rpc.calls[0] == ("private/cancel", {"order_id": "o1"})
    assert rpc.calls[1] == ("private/edit", {"order_id": "o1", "amount": 10.0, "price": 50001.0})


async def test_rate_limit_retries_then_succeeds():
    rpc = FakeRpc({"private/buy": [
        DeribitRpcError(10028, "too_many_requests"),
        DeribitRpcError(10028, "too_many_requests"),
        order_result("o9"),
    ]})
    ex = TestnetExecClient(rpc, instrument="BTC-PERPETUAL", retry_base_delay_s=0.01)
    assert await ex.place_limit("buy", price=50000.0, amount_usd=10.0) == "o9"
    assert len(rpc.calls) == 3


async def test_non_rate_limit_error_propagates():
    rpc = FakeRpc({"private/buy": [DeribitRpcError(10009, "not_enough_funds")]})
    ex = TestnetExecClient(rpc, instrument="BTC-PERPETUAL")
    with pytest.raises(DeribitRpcError):
        await ex.place_limit("buy", price=50000.0, amount_usd=10.0)


async def test_subscribe_private_channels():
    rpc = FakeRpc({"private/subscribe": [["user.orders.BTC-PERPETUAL.raw",
                                          "user.trades.BTC-PERPETUAL.raw"]]})
    ex = TestnetExecClient(rpc, instrument="BTC-PERPETUAL")
    await ex.subscribe_updates()
    method, params = rpc.calls[0]
    assert method == "private/subscribe"
    assert params == {"channels": [
        "user.orders.BTC-PERPETUAL.raw",
        "user.trades.BTC-PERPETUAL.raw",
    ]}


def test_order_tracker_accumulates_partial_fills():
    t = OrderTracker()
    t.on_notification({"method": "subscription", "params": {
        "channel": "user.orders.BTC-PERPETUAL.raw",
        "data": {"order_id": "o1", "order_state": "open"},
    }})
    t.on_notification({"method": "subscription", "params": {
        "channel": "user.trades.BTC-PERPETUAL.raw",
        "data": [{"order_id": "o1", "amount": 10.0, "price": 50000.0},
                 {"order_id": "o1", "amount": 20.0, "price": 50000.0}],
    }})
    t.on_notification({"method": "subscription", "params": {
        "channel": "user.orders.BTC-PERPETUAL.raw",
        "data": {"order_id": "o1", "order_state": "filled"},
    }})
    assert t.state("o1") == "filled"
    assert t.filled_usd("o1") == 30.0


def test_order_tracker_ignores_unrelated_notifications():
    t = OrderTracker()
    t.on_notification({"method": "heartbeat", "params": {"type": "test_request"}})
    t.on_notification({"method": "subscription", "params": {
        "channel": "book.BTC-PERPETUAL.100ms", "data": {}}})
    assert t.state("nope") is None
    assert t.filled_usd("nope") == 0.0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv\Scripts\python -m pytest tests/test_exec_orders.py -v`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

```python
# mm_bot/exec_testnet/orders.py
"""Order operations and own-fill tracking on the authenticated RPC client.

Rate-limit errors (10028) retry with exponential backoff; anything else
propagates: on a testnet demo a real error should be seen, not swallowed.
"""
import asyncio

from mm_bot.exec_testnet.rpc import DeribitRpcError

RATE_LIMIT_CODE = 10028
MAX_RETRIES = 5


class TestnetExecClient:
    def __init__(self, rpc, instrument: str, retry_base_delay_s: float = 0.5) -> None:
        self._rpc = rpc
        self._instrument = instrument
        self._retry_base_delay_s = retry_base_delay_s

    async def _call_with_retry(self, method: str, params: dict):
        delay = self._retry_base_delay_s
        for attempt in range(MAX_RETRIES):
            try:
                return await self._rpc.call(method, params)
            except DeribitRpcError as exc:
                if exc.code != RATE_LIMIT_CODE or attempt == MAX_RETRIES - 1:
                    raise
                await asyncio.sleep(delay)
                delay *= 2

    async def place_limit(self, side: str, price: float, amount_usd: float) -> str:
        method = "private/buy" if side == "buy" else "private/sell"
        result = await self._call_with_retry(method, {
            "instrument_name": self._instrument,
            "amount": amount_usd,
            "type": "limit",
            "price": price,
            "post_only": True,
        })
        return result["order"]["order_id"]

    async def cancel(self, order_id: str):
        return await self._call_with_retry("private/cancel", {"order_id": order_id})

    async def edit(self, order_id: str, price: float, amount_usd: float):
        return await self._call_with_retry(
            "private/edit", {"order_id": order_id, "amount": amount_usd, "price": price}
        )

    async def subscribe_updates(self):
        return await self._rpc.call("private/subscribe", {"channels": [
            f"user.orders.{self._instrument}.raw",
            f"user.trades.{self._instrument}.raw",
        ]})


class OrderTracker:
    """Tracks own order states and accumulated fill amounts from notifications."""

    def __init__(self) -> None:
        self._states: dict[str, str] = {}
        self._filled_usd: dict[str, float] = {}

    def on_notification(self, msg: dict) -> None:
        if msg.get("method") != "subscription":
            return
        channel = msg["params"]["channel"]
        data = msg["params"]["data"]
        if channel.startswith("user.orders."):
            self._states[data["order_id"]] = data["order_state"]
        elif channel.startswith("user.trades."):
            for trade in data:
                oid = trade["order_id"]
                self._filled_usd[oid] = self._filled_usd.get(oid, 0.0) + trade["amount"]

    def state(self, order_id: str) -> str | None:
        return self._states.get(order_id)

    def filled_usd(self, order_id: str) -> float:
        return self._filled_usd.get(order_id, 0.0)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv\Scripts\python -m pytest tests/test_exec_orders.py -v` — expected: 8 passed.
Full suite — expected: all green (77 + 5 + 8 = 90).

- [ ] **Step 5: Commit**

```bash
git add mm_bot/exec_testnet/orders.py tests/test_exec_orders.py
git commit -m "feat: testnet order operations with rate-limit retry and fill tracking"
```

---

### Task 3: Demo script + VPS deploy files

**Files:**
- Create: `run_testnet_demo.py`
- Create: `deploy/ecosystem.config.js`
- Create: `deploy/DEPLOY.md`

No unit tests: the demo script is verified by the supervised run once API keys
exist; deploy files are verified at deploy time.

- [ ] **Step 1: Write the demo script**

```python
# run_testnet_demo.py
"""Supervised Deribit TESTNET order-plumbing demo (milestone 5).

Proves real exchange plumbing works: auth, post-only placement, amend,
cancel, and fill tracking. Its numbers are NEVER reported as performance:
testnet books are not real markets.

Requires environment variables DERIBIT_TESTNET_CLIENT_ID and
DERIBIT_TESTNET_CLIENT_SECRET (create keys at test.deribit.com, free).

Usage: python run_testnet_demo.py
"""
import asyncio
import logging
import os
import time

import websockets

from mm_bot.exec_testnet.orders import OrderTracker, TestnetExecClient
from mm_bot.exec_testnet.rpc import RpcClient

log = logging.getLogger("testnet_demo")

TESTNET_WS = "wss://test.deribit.com/ws/api/v2"
INSTRUMENT = "BTC-PERPETUAL"
AMOUNT_USD = 10.0  # one contract


async def get_mid(rpc: RpcClient) -> float:
    ticker = await rpc.call("public/ticker", {"instrument_name": INSTRUMENT})
    return (ticker["best_bid_price"] + ticker["best_ask_price"]) / 2


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    client_id = os.environ.get("DERIBIT_TESTNET_CLIENT_ID")
    client_secret = os.environ.get("DERIBIT_TESTNET_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Set DERIBIT_TESTNET_CLIENT_ID and DERIBIT_TESTNET_CLIENT_SECRET "
            "(create API keys at test.deribit.com)"
        )

    tracker = OrderTracker()
    async with websockets.connect(TESTNET_WS) as ws:
        rpc = RpcClient(ws, on_notification=tracker.on_notification)
        pump = asyncio.create_task(rpc.read_loop())
        ex = TestnetExecClient(rpc, instrument=INSTRUMENT)

        log.info("authenticating...")
        await rpc.authenticate(client_id, client_secret, now_s=time.monotonic())
        await ex.subscribe_updates()

        mid = await get_mid(rpc)
        log.info("mid=%.1f", mid)

        # 1. passive bid far below mid: must rest, not fill
        far_price = round((mid - 2000) * 2) / 2
        oid = await ex.place_limit("buy", price=far_price, amount_usd=AMOUNT_USD)
        log.info("placed passive bid %s at %.1f", oid, far_price)
        await asyncio.sleep(2)
        log.info("state after 2s: %s (expected open)", tracker.state(oid))

        # 2. amend it 100 USD higher, still far from mid
        await ex.edit(oid, price=far_price + 100, amount_usd=AMOUNT_USD)
        log.info("amended %s to %.1f", oid, far_price + 100)
        await asyncio.sleep(2)

        # 3. cancel it
        await ex.cancel(oid)
        await asyncio.sleep(2)
        log.info("state after cancel: %s (expected cancelled)", tracker.state(oid))

        # 4. crossing bid above mid: should fill immediately (post_only False here)
        cross_price = round((mid + 50) * 2) / 2
        result = await ex._call_with_retry("private/buy", {
            "instrument_name": INSTRUMENT, "amount": AMOUNT_USD,
            "type": "limit", "price": cross_price, "post_only": False,
        })
        oid2 = result["order"]["order_id"]
        log.info("placed crossing bid %s at %.1f", oid2, cross_price)
        await asyncio.sleep(3)
        log.info("state: %s filled_usd=%.1f (expected filled, 10.0)",
                 tracker.state(oid2), tracker.filled_usd(oid2))

        # 5. flatten: sell back whatever filled
        filled = tracker.filled_usd(oid2)
        if filled > 0:
            sell_mid = await get_mid(rpc)
            result = await ex._call_with_retry("private/sell", {
                "instrument_name": INSTRUMENT, "amount": filled,
                "type": "limit", "price": round((sell_mid - 50) * 2) / 2,
                "post_only": False,
            })
            log.info("flattened with sell order %s", result["order"]["order_id"])
            await asyncio.sleep(3)

        pump.cancel()
        log.info("demo complete: auth, place, amend, cancel, fill, flatten all exercised")


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Write deploy/ecosystem.config.js**

```javascript
// pm2 process file for the 7-day paper run on the VPS (Linux).
// Adjust cwd to the clone location before `pm2 start deploy/ecosystem.config.js`.
module.exports = {
  apps: [
    {
      name: "mm-bot-paper",
      cwd: "/opt/mm-bot",
      script: ".venv/bin/python",
      args: "run_paper.py",
      interpreter: "none",
      autorestart: true,
      max_restarts: 100,
      restart_delay: 5000,
      out_file: "logs/paper.out.log",
      error_file: "logs/paper.err.log",
      merge_logs: true,
      time: true,
    },
  ],
};
```

- [ ] **Step 3: Write deploy/DEPLOY.md**

```markdown
# VPS deploy (milestone 4)

One-time setup on the VPS (Ubuntu assumed, pm2 already installed for CSAlpha):

    sudo mkdir -p /opt/mm-bot && sudo chown $USER /opt/mm-bot
    git clone <repo-or-rsync-from-dev-machine> /opt/mm-bot
    cd /opt/mm-bot
    python3.12 -m venv .venv          # python3 --version must be >= 3.12
    .venv/bin/pip install -e .
    mkdir -p logs data
    .venv/bin/python -m pytest -q     # all green before starting

Start the 7-day dual-strategy run:

    pm2 start deploy/ecosystem.config.js
    pm2 save
    pm2 logs mm-bot-paper --lines 20  # expect per-strategy stats lines each minute

Daily health check:

    sqlite3 data/mm.sqlite "SELECT strategy, MAX(ts_ms), COUNT(*) FROM rollups GROUP BY strategy;"
    pm2 status                        # restarts count = disclosed downtime events

Stop at the end of the measurement window:

    pm2 stop mm-bot-paper

Notes:
- data/mm.sqlite and data/raw-*.jsonl grow ~50-100 MB/day combined; ensure
  a few GB free.
- Every pm2 restart starts a new session row (new session_id); the write-up
  must disclose restart count and gaps (query the sessions table).
- Do NOT run the testnet demo on the VPS; it is a local supervised script.
```

- [ ] **Step 4: Verify demo script imports**

Run: `.venv\Scripts\python -c "import run_testnet_demo"` — expected exit 0 (the
credential check lives inside main(), which does not run on import).

Full suite still green: `.venv\Scripts\python -m pytest -q`.

- [ ] **Step 5: Commit**

```bash
git add run_testnet_demo.py deploy/ecosystem.config.js deploy/DEPLOY.md
git commit -m "feat: testnet demo script and VPS deploy files"
```

---

## Execution notes

- Sonnet 5 subagents implement Tasks 1-3 sequentially; Fable reviews each diff.
- The demo RUN (with real keys) is deferred until Yehia creates testnet API keys; the code is complete without them.
- Credentials: environment variables only. Never write keys into config.yaml, a committed .env, or any tracked file.
