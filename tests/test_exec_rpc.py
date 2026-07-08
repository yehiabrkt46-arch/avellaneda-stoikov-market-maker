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
