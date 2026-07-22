# mm_bot/research/edge.py
"""Per-fill P&L decomposition into spread capture and adverse selection.

A fill's realized edge is not one number, it is the sum of two effects that
pull in opposite directions: the spread earned if the position were flattened
immediately at the fill-time mid, and the extra P&L (usually negative) from
the mid drifting between the fill and the adverse-selection horizon. Reusing
the existing Portfolio inverse-perpetual accounting to compute both, rather
than re-deriving the P&L formula here, is deliberate: Portfolio is already
tested against the exact Deribit contract math, so building tiny synthetic
round-trip fills and reading off equity_usd cannot introduce a new sign or
rounding bug independent of that formula.
"""
import sqlite3

from mm_bot.paper.portfolio import Fill, Portfolio


def spread_capture_usd(side: str, price: float, amount_usd: float, mid_at_fill: float) -> float:
    """Realized P&L if this fill were immediately flattened at mid_at_fill."""
    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(timestamp_ms=0, side=side, price=price, amount_usd=amount_usd, trade_id="synthetic")
    )
    opposite_side = "sell" if side == "buy" else "buy"
    portfolio.apply_fill(
        Fill(timestamp_ms=0, side=opposite_side, price=mid_at_fill, amount_usd=amount_usd, trade_id="synthetic")
    )
    return portfolio.equity_usd(mid_at_fill)


def adverse_selection_usd(
    side: str, price: float, amount_usd: float, mid_at_fill: float, adverse_move_usd: float,
) -> float:
    """Additional P&L impact of the mid drifting to the adverse-selection horizon.

    Isolated from spread_capture_usd so the two sum exactly to the total P&L
    to the horizon: adverse_selection_usd = total_usd - spread_capture_usd.
    """
    if side == "buy":
        mid_forward = mid_at_fill - adverse_move_usd
    else:
        mid_forward = mid_at_fill + adverse_move_usd
    portfolio = Portfolio()
    portfolio.apply_fill(
        Fill(timestamp_ms=0, side=side, price=price, amount_usd=amount_usd, trade_id="synthetic")
    )
    opposite_side = "sell" if side == "buy" else "buy"
    portfolio.apply_fill(
        Fill(timestamp_ms=0, side=opposite_side, price=mid_forward, amount_usd=amount_usd, trade_id="synthetic")
    )
    total_usd = portfolio.equity_usd(mid_forward)
    return total_usd - spread_capture_usd(side, price, amount_usd, mid_at_fill)


def day_bucket(ts_ms: int) -> int:
    """UTC calendar day index (days since epoch) for a timestamp, used to bucket rollups/fills for walk-forward evaluation."""
    return ts_ms // 86_400_000


def aggregate_fill_edge_by_day(conn: sqlite3.Connection, session_id: str) -> dict[tuple[str, int], dict]:
    """Sum spread capture and adverse selection per (strategy, day_bucket).

    Fixed_spread and avellaneda_stoikov are separate lanes with independent
    fill sequences, so they are kept separate rather than summed together;
    the caller decides how to compare them. Fills whose adverse-selection
    horizon never resolved (adverse_move_usd IS NULL because the session
    ended first) are skipped, matching the rest of this module.
    """
    rows = conn.execute(
        "SELECT ts_ms, strategy, side, price, amount_usd, mid_at_fill, adverse_move_usd"
        " FROM fills WHERE session_id = ? AND adverse_move_usd IS NOT NULL ORDER BY ts_ms",
        (session_id,),
    ).fetchall()
    buckets: dict[tuple[str, int], dict] = {}
    for ts_ms, strategy, side, price, amount_usd, mid_at_fill, adverse_move_usd in rows:
        key = (strategy, day_bucket(ts_ms))
        bucket = buckets.setdefault(
            key, {"spread_capture_usd": 0.0, "adverse_selection_usd": 0.0, "fill_count": 0}
        )
        bucket["spread_capture_usd"] += spread_capture_usd(side, price, amount_usd, mid_at_fill)
        bucket["adverse_selection_usd"] += adverse_selection_usd(
            side, price, amount_usd, mid_at_fill, adverse_move_usd
        )
        bucket["fill_count"] += 1
    return buckets
