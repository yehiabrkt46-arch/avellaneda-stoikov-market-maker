# scripts/run_kdb_parity.py
"""Recompute the full 9.66-day edge decomposition in q from the tick db and in
Python from the replay SQLite, and require per-(strategy, day) agreement.
Exit code 0 = parity holds; nonzero with a printed diff = investigate q.
"""
import math
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.edge import aggregate_fill_edge_by_day  # noqa: E402
from mm_bot.research.qsession import get_q  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TICK_DB = os.environ.get("MM_TICK_DB", str(REPO / "data" / "tick"))
DB = REPO / "data/vps-pull-20260721/replay-verify-20260711-231533.sqlite"
SESSION_ID = "replay-verify-20260711-231533"
TOL_USD = 1e-6


def _as_str(value) -> str:
    """Symbols round-trip through .pd() as bytes; fields not backed by an
    encoding-sensitive type come through as plain str already."""
    if isinstance(value, bytes):
        return value.decode()
    return str(value)


q = get_q(scripts=("edge.q",))
q(f'system "l {Path(TICK_DB).as_posix()}"')
q_rows = q("0! edgeByDay select from fill").pd()

py = aggregate_fill_edge_by_day(sqlite3.connect(DB), SESSION_ID)

failures = []
if len(q_rows) != len(py):
    failures.append(f"bucket count: q={len(q_rows)} python={len(py)}")
for _, row in q_rows.iterrows():
    key = (_as_str(row["strat"]), int(row["dayIdx"]))
    if key not in py:
        failures.append(f"{key}: in q only")
        continue
    p = py[key]
    for qcol, pcol in (("scUsd", "spread_capture_usd"), ("asUsd", "adverse_selection_usd")):
        if not math.isclose(row[qcol], p[pcol], abs_tol=TOL_USD):
            failures.append(f"{key} {pcol}: q={row[qcol]!r} python={p[pcol]!r}")
    if int(row["n"]) != p["fill_count"]:
        failures.append(f"{key} fill_count: q={int(row['n'])} python={p['fill_count']}")

print(f"buckets: {len(py)}  tolerance: {TOL_USD} USD")
if failures:
    print("PARITY FAILED:")
    for f in failures:
        print(" ", f)
    sys.exit(1)
print("PARITY OK: q reproduces the Python edge decomposition on all buckets")
