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
