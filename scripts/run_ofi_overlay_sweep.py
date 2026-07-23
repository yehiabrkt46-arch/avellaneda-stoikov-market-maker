import sys
import time
from dataclasses import replace
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
OUT_DB = REPO_ROOT / "data" / "ofi-overlay-results.sqlite"

# Train-day OLS fits from M8 (data/ofi-results.json, produced by
# scripts/run_ofi_study.py, train days 20645-20652 only). Alpha is dropped
# per the milestone-9 plan (a constant shift is a bias term, negligible).
OFI_BETA_5S = 1.783e-10
OFI_BETA_1S = 6.863e-11

# M7 published control numbers (sweep-avellaneda_stoikov-g0.002-h120.0),
# reproduced here as an end-to-end determinism check for the ofi-c0 candidate.
M7_CONTROL_TRAIN = -68.65578
M7_CONTROL_TEST = -19.42465
DETERMINISM_TOL = 1e-3


def build_candidates(base_as_cfg: StrategyConfig) -> list[tuple[str, StrategyConfig]]:
    """The 5 OFI-overlay candidates, all over the M7 A-S winner (gamma=0.002,
    horizon_s=120.0), varying only ofi_beta/ofi_scale.
    """
    overlay_cfg = replace(base_as_cfg, gamma=0.002, horizon_s=120.0)
    specs = [
        ("ofi-c0", 0.0, 0.0),
        ("ofi-b5-s0.5", OFI_BETA_5S, 0.5),
        ("ofi-b5-s1.0", OFI_BETA_5S, 1.0),
        ("ofi-b5-s2.0", OFI_BETA_5S, 2.0),
        ("ofi-b1-s1.0", OFI_BETA_1S, 1.0),
    ]
    candidates = []
    for name, ofi_beta, ofi_scale in specs:
        cfg = replace(overlay_cfg, name=name, ofi_beta=ofi_beta, ofi_scale=ofi_scale)
        candidates.append(("avellaneda_stoikov", cfg))
    return candidates


def _base_as_config(cfg_path: Path) -> StrategyConfig:
    cfg = load_config(cfg_path)
    for strategy_cfg in cfg.strategies:
        if strategy_cfg.kind == "avellaneda_stoikov":
            return strategy_cfg
    raise ValueError(f"no avellaneda_stoikov strategy entry found in {cfg_path}")


def run_sweep(raw_path: Path, db_path: Path, only: str | None) -> None:
    cfg = load_config(CFG)
    base_as_cfg = _base_as_config(CFG)
    store = Store(db_path)
    adverse_horizon_ms = int(cfg.store.adverse_horizon_s * 1000)
    stale_quote_pull_ms = int(cfg.store.stale_quote_pull_s * 1000)

    for kind, candidate_cfg in build_candidates(base_as_cfg):
        session_id = candidate_cfg.name
        if only is not None and session_id != only:
            continue

        # resumability matters here: a multi-hour sweep can be interrupted
        # mid-run, so every candidate must be safely skippable on a re-run
        # instead of redone from scratch.
        already_done = store.connection.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if already_done:
            print("skip", session_id, "(already done)", flush=True)
            continue

        print("starting candidate:", session_id, flush=True)
        t0 = time.time()
        summary = replay_file(
            raw_path,
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


def run_report(db_path: Path, train_frac: float = 0.7) -> None:
    store = Store(db_path)
    conn = store.connection
    base_as_cfg = _base_as_config(CFG)
    candidates = build_candidates(base_as_cfg)

    per_candidate_agg = {}
    all_days = set()
    for kind, candidate_cfg in candidates:
        session_id = candidate_cfg.name
        exists = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not exists:
            continue
        agg = aggregate_fill_edge_by_day(conn, session_id)
        per_candidate_agg[session_id] = agg
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

    scores = {}
    for name, agg in per_candidate_agg.items():
        train_score = score(agg, name, train_days)
        test_score = score(agg, name, test_days)
        scores[name] = (train_score, test_score)

    store.close()

    print("per-candidate scores (selected/reported on train days; test reported only):", flush=True)
    for name, (train_score, test_score) in scores.items():
        print(f"{name}: train_score_usd={train_score:.4f} test_score_usd={test_score:.4f}", flush=True)

    if "ofi-c0" not in scores:
        print("comparison: skipped, ofi-c0 control candidate not present in db", flush=True)
        return

    control_train, control_test = scores["ofi-c0"]
    overlay_candidates = {name: s for name, s in scores.items() if name != "ofi-c0"}
    if overlay_candidates:
        best_name, (best_train, best_test) = max(
            overlay_candidates.items(), key=lambda item: item[1][0]
        )
        print("comparison: control=ofi-c0 vs best-by-train overlay candidate", flush=True)
        print(f"  control ofi-c0: train_score_usd={control_train:.4f} test_score_usd={control_test:.4f}", flush=True)
        print(f"  best overlay {best_name}: train_score_usd={best_train:.4f} test_score_usd={best_test:.4f}", flush=True)
    else:
        print("comparison: skipped, no overlay candidates present in db", flush=True)

    train_ok = abs(control_train - M7_CONTROL_TRAIN) <= DETERMINISM_TOL
    test_ok = abs(control_test - M7_CONTROL_TEST) <= DETERMINISM_TOL
    if train_ok and test_ok:
        print(
            f"determinism check: PASS (ofi-c0 train={control_train:.5f} test={control_test:.5f} "
            f"match M7 train={M7_CONTROL_TRAIN} test={M7_CONTROL_TEST} within {DETERMINISM_TOL})",
            flush=True,
        )
    else:
        print(
            f"determinism check: FAIL (ofi-c0 train={control_train:.5f} test={control_test:.5f} "
            f"vs M7 train={M7_CONTROL_TRAIN} test={M7_CONTROL_TEST}, tolerance {DETERMINISM_TOL})",
            flush=True,
        )


def _parse_args(argv: list[str]) -> dict:
    args = {"raw": RAW, "db": OUT_DB, "only": None, "report": False}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--report":
            args["report"] = True
            i += 1
        elif arg == "--raw":
            args["raw"] = Path(argv[i + 1])
            i += 2
        elif arg == "--db":
            args["db"] = Path(argv[i + 1])
            i += 2
        elif arg == "--only":
            args["only"] = argv[i + 1]
            i += 2
        else:
            raise SystemExit(f"unknown argument: {arg}")
    return args


if __name__ == "__main__":
    parsed = _parse_args(sys.argv[1:])
    if parsed["report"]:
        run_report(parsed["db"])
    else:
        run_sweep(parsed["raw"], parsed["db"], parsed["only"])
