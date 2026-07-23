# scripts/run_kdb_report.py
"""q-generated sweep report; must reproduce run_param_sweep.py --report winners."""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.qsession import get_q  # noqa: E402
from mm_bot.research.walkforward import train_test_split_days  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "sweep-results.sqlite"

q = get_q(scripts=("report.q",))
import pykx  # noqa: E402

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT session_id, ts_ms, side, price, amount_usd, mid_at_fill,"
    " adverse_move_usd FROM fills WHERE adverse_move_usd IS NOT NULL ORDER BY ts_ms").fetchall()
print(f"loaded {len(rows)} fills rows", flush=True)
q["sweepFills"] = pykx.Table(data={
    "session": [r[0] for r in rows], "tsMs": [r[1] for r in rows],
    "side": [r[2] for r in rows], "price": [r[3] for r in rows],
    "amtUsd": [r[4] for r in rows], "midAtFill": [r[5] for r in rows],
    "advMoveUsd": [r[6] for r in rows]})

days = sorted(int(d) for d in q("exec distinct tsMs div 86400000 from sweepFills").py())
train_days, test_days = train_test_split_days(days, train_frac=0.7)  # mirrors run_param_sweep.py --report exactly
print("train_days", train_days, flush=True)
print("test_days", test_days, flush=True)
q["trainDays"] = train_days
q["testDays"] = test_days
q("scored:scoreByDay sweepFills")
result = q("report[scored;trainDays;testDays]")
print("per-candidate train/test scores (train desc):")
print(result)

df = result.pd()


def _as_str(value):
    return value.decode() if isinstance(value, bytes) else str(value)


df["session"] = df["session"].map(_as_str)
df["kind"] = df["session"].map(
    lambda s: "fixed_spread" if s.startswith("sweep-fixed_spread") else "avellaneda_stoikov")
print("\nwalk-forward winners (selected on train days only, from q report):")
for kind, group in df.groupby("kind"):
    winner = group.loc[group["train"].idxmax()]
    print(f"{kind}: winner={winner['session']} train_score_usd={winner['train']:.4f}"
          f" test_score_usd={winner['test']:.4f}")
