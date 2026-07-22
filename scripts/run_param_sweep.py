import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.config import StrategyConfig, load_config
from mm_bot.paper.replay import replay_file
from mm_bot.research.edge import aggregate_fill_edge_by_day
from mm_bot.research.walkforward import train_test_split_days
from mm_bot.store.db import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATE_DATA_DIRS = [
    REPO_ROOT / "data" / "vps-pull-20260721",
    REPO_ROOT / "data",
]
RAW = next(d / "raw-20260711-231533.jsonl" for d in CANDIDATE_DATA_DIRS if (d / "raw-20260711-231533.jsonl").exists())
CFG = REPO_ROOT / "config.yaml"
OUT_DB = REPO_ROOT / "data" / "sweep-results.sqlite"


def build_candidates() -> list[tuple[str, StrategyConfig]]:
    """The grid of (strategy_kind, StrategyConfig) candidates to replay.

    Fixed_spread only varies half_spread_usd. Avellaneda_stoikov crosses
    gamma and horizon_s (9 combos), the two levers confirmed to actually
    change behavior post-warmup; half_spread_usd only matters during its
    warmup fallback so it is left at the default for every AS candidate.
    """
    candidates = []
    for half_spread_usd in (3.0, 5.0, 8.0):
        name = f"fixed_spread-hs{half_spread_usd}"
        cfg = StrategyConfig(kind="fixed_spread", name=name, half_spread_usd=half_spread_usd)
        candidates.append(("fixed_spread", cfg))
    for gamma in (0.0005, 0.001, 0.002):
        for horizon_s in (30.0, 60.0, 120.0):
            name = f"avellaneda_stoikov-g{gamma}-h{horizon_s}"
            cfg = StrategyConfig(kind="avellaneda_stoikov", name=name, gamma=gamma, horizon_s=horizon_s)
            candidates.append(("avellaneda_stoikov", cfg))
    return candidates


def run_sweep() -> None:
    cfg = load_config(CFG)
    store = Store(OUT_DB)
    adverse_horizon_ms = int(cfg.store.adverse_horizon_s * 1000)
    stale_quote_pull_ms = int(cfg.store.stale_quote_pull_s * 1000)

    for kind, candidate_cfg in build_candidates():
        session_id = f"sweep-{candidate_cfg.name}"
        # resumability matters here: a prior multi-hour sweep was killed by a
        # session restart mid-run, so every candidate must be safely skippable
        # on a re-run instead of redone from scratch.
        already_done = store.connection.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if already_done:
            print("skipping, already done:", session_id, flush=True)
            continue

        print("starting candidate:", session_id, flush=True)
        t0 = time.time()
        summary = replay_file(
            RAW,
            [candidate_cfg],
            store,
            session_id=session_id,
            adverse_horizon_ms=adverse_horizon_ms,
            stale_quote_pull_ms=stale_quote_pull_ms,
        )
        elapsed = time.time() - t0
        print("elapsed_s", round(elapsed, 1), flush=True)
        print(session_id, summary, flush=True)

    store.close()


def run_report(train_frac: float = 0.7) -> None:
    store = Store(OUT_DB)
    conn = store.connection
    candidates = build_candidates()

    per_candidate_agg = {}
    all_days = set()
    for kind, candidate_cfg in candidates:
        session_id = f"sweep-{candidate_cfg.name}"
        exists = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not exists:
            continue
        agg = aggregate_fill_edge_by_day(conn, session_id)
        per_candidate_agg[candidate_cfg.name] = (kind, agg)
        for strategy, day in agg:
            all_days.add(day)

    train_days, test_days = train_test_split_days(sorted(all_days), train_frac=train_frac)
    print("train_days", train_days, flush=True)
    print("test_days", test_days, flush=True)

    def score(agg, name, days):
        total = 0.0
        for day in days:
            bucket = agg.get((name, day))
            if bucket is not None:
                total += bucket["spread_capture_usd"] + bucket["adverse_selection_usd"]
        return total

    best_by_kind = {}
    for name, (kind, agg) in per_candidate_agg.items():
        train_score = score(agg, name, train_days)
        test_score = score(agg, name, test_days)
        current_best = best_by_kind.get(kind)
        if current_best is None or train_score > current_best[1]:
            best_by_kind[kind] = (name, train_score, test_score)

    store.close()

    print("walk-forward winners (selected on train days only):", flush=True)
    for kind, (name, train_score, test_score) in best_by_kind.items():
        print(
            f"{kind}: winner={name} train_score_usd={train_score:.4f} test_score_usd={test_score:.4f}",
            flush=True,
        )


if __name__ == "__main__":
    if "--report" in sys.argv:
        run_report()
    else:
        run_sweep()
