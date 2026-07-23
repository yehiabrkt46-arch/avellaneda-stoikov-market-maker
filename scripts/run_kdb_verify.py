# scripts/run_kdb_verify.py
"""aj-based independent recomputation report; see q/verify.q for semantics."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.qsession import get_q  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TICK_DB = os.environ.get("MM_TICK_DB", str(REPO / "data" / "tick"))
HORIZON_MS = 5000  # matches config.yaml/config.py default store.adverse_horizon_s = 5.0

q = get_q(scripts=("verify.q",))
q(f'system "l {Path(TICK_DB).as_posix()}"')
print("mid-at-fill reproduction (stored vs aj):")
print(q("checkMidAtFill[]"))
print(f"\nadverse move at {HORIZON_MS}ms (stored next-obs vs aj last-obs):")
print(q(f"checkAdvMove[{HORIZON_MS}]"))
print("\nreference scale: median |advMoveUsd| =",
      q("exec med abs advMoveUsd from select advMoveUsd from fill where not null advMoveUsd"))
