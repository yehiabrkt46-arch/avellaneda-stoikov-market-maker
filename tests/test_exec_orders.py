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
