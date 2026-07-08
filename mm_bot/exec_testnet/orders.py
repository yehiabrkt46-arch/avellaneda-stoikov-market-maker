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
