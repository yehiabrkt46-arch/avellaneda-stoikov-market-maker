# tests/test_qedge.py
"""q edge decomposition must match the Python oracle exactly on synthetic
fills; the Python implementation is trusted (tested against Deribit
inverse-perp math), q is the one on trial."""
import math
import sqlite3

from mm_bot.research.edge import aggregate_fill_edge_by_day
from tests.conftest import requires_pykx

FILLS = [
    # (ts_ms, strategy, side, price, amount_usd, mid_at_fill, adverse_move_usd)
    (1_000, "a", "buy", 100.0, 10.0, 100.5, 0.3),
    (2_000, "a", "sell", 101.0, 10.0, 100.5, -0.2),
    (86_401_000, "a", "buy", 99.0, 20.0, 99.4, 1.1),   # next UTC day
    (86_402_000, "b", "sell", 99.5, 10.0, 99.2, 0.0),
    (86_403_000, "b", "buy", 99.0, 10.0, 99.1, None),  # unresolved: excluded
]


def _sqlite_with_fills():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE fills (id INTEGER PRIMARY KEY, session_id TEXT, ts_ms INTEGER,"
        " strategy TEXT, side TEXT, price REAL, amount_usd REAL, trade_id TEXT,"
        " mid_at_fill REAL, adverse_move_usd REAL)")
    for ts, strat, side, p, u, m, adv in FILLS:
        conn.execute(
            "INSERT INTO fills (session_id, ts_ms, strategy, side, price, amount_usd,"
            " trade_id, mid_at_fill, adverse_move_usd) VALUES ('s', ?, ?, ?, ?, ?, 't', ?, ?)",
            (ts, strat, side, p, u, m, adv))
    return conn


@requires_pykx
def test_q_edge_matches_python_oracle():
    import pykx
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("edge.q",))
    python_result = aggregate_fill_edge_by_day(_sqlite_with_fills(), "s")

    # NB: the table is named "rawFills", not "fills" -- `fills` is a q
    # reserved word (forward-fill nulls) and pykx refuses `q["fills"] = ...`
    # with "Cannot assign to reserved word or overwrite q namespace."
    q["rawFills"] = pykx.Table(data={
        "tsMs": [r[0] for r in FILLS],
        "strat": [r[1] for r in FILLS],
        "side": [r[2] for r in FILLS],
        "price": [r[3] for r in FILLS],
        "amtUsd": [r[4] for r in FILLS],
        "midAtFill": [r[5] for r in FILLS],
        "advMoveUsd": [float("nan") if r[6] is None else r[6] for r in FILLS],
    })
    q_result = q("0! edgeByDay rawFills").pd()

    assert len(q_result) == len(python_result)
    for _, row in q_result.iterrows():
        key = (str(row["strat"]), int(row["dayIdx"]))
        py = python_result[key]
        assert row["n"] == py["fill_count"]
        assert math.isclose(row["scUsd"], py["spread_capture_usd"], abs_tol=1e-9)
        assert math.isclose(row["asUsd"], py["adverse_selection_usd"], abs_tol=1e-9)
