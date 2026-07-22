import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.config import load_config
from mm_bot.paper.replay import replay_file
from mm_bot.store.db import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATE_DATA_DIRS = [
    REPO_ROOT / "data" / "vps-pull-20260721",
    REPO_ROOT / "data",
]
RAW = next(d / "raw-20260711-231533.jsonl" for d in CANDIDATE_DATA_DIRS if (d / "raw-20260711-231533.jsonl").exists())
OUT_DIR = RAW.parent
OUT_DB = OUT_DIR / "replay-verify-20260711-231533.sqlite"
CFG = REPO_ROOT / "config.yaml"

cfg = load_config(CFG)

for ext in ("", "-shm", "-wal"):
    Path(str(OUT_DB) + ext).unlink(missing_ok=True)
store = Store(OUT_DB)

t0 = time.time()
print("starting replay of", RAW, flush=True)
summary = replay_file(
    RAW,
    list(cfg.strategies),
    store,
    session_id="replay-verify-20260711-231533",
    adverse_horizon_ms=int(cfg.store.adverse_horizon_s * 1000),
    stale_quote_pull_ms=int(cfg.store.stale_quote_pull_s * 1000),
)
elapsed = time.time() - t0

print("elapsed_s", round(elapsed, 1), flush=True)
for name, stats in summary.items():
    print(name, stats, flush=True)

store.close()
print("done, db at", OUT_DB, flush=True)
