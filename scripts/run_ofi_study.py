# scripts/run_ofi_study.py
"""OFI predictive study on the recorded window, walk-forward evaluated.

Fits OLS of forward mid return on bucketed OFI using train days only
(identical chronological split to the parameter sweep: train_test_split_days
with its default train_frac=0.7, the same invocation used by
scripts/run_param_sweep.py's --report path), reports out-of-sample R2 and
directional hit rate on held-out days. Writes data/ofi-results.json. A
near-zero or negative held-out R2 is a publishable negative result, per
project discipline; do not tune on test.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.qsession import get_q  # noqa: E402
from mm_bot.research.walkforward import train_test_split_days  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TICK_DB = os.environ.get("MM_TICK_DB", str(REPO / "data" / "tick"))
BUCKET_MS = 1000
HORIZONS = (1, 5, 10)  # buckets ahead = seconds at BUCKET_MS=1000


q = get_q(scripts=("ofi.q",))
q(f'system "l {Path(TICK_DB).as_posix()}"')

days = sorted(int(d) for d in q("exec distinct tsMs div 86400000 from select tsMs from top").py())
train_days, test_days = train_test_split_days(days)  # matches run_param_sweep.py --report: default train_frac=0.7
print(f"days: {len(days)} train: {train_days} test: {test_days}")
q["trainDays"] = train_days
q["testDays"] = test_days

results = {}
for h in HORIZONS:
    q(f"bk:0! withFwdRet[ofiBuckets[select tsMs, bid, bsize, ask, asize from top;{BUCKET_MS}];{h}]")
    q("bkTrain:select from bk where (bkt div 86400000) in trainDays")
    q("bkTest:select from bk where (bkt div 86400000) in testDays")
    q("mdl:fitOls[exec ofi from bkTrain; exec fwdRet from bkTrain]")
    fit = {(k.decode() if isinstance(k, bytes) else k): v for k, v in q("mdl").py().items()}
    ev = q("evalOls[mdl; exec ofi from bkTest; exec fwdRet from bkTest]").py()
    ev = {(k.decode() if isinstance(k, bytes) else k): v for k, v in ev.items()}
    results[f"h{h}s"] = {
        "bucket_ms": BUCKET_MS,
        "horizon_buckets": h,
        "train": {"alpha": float(fit["alpha"]), "beta": float(fit["beta"]), "n": int(fit["n"])},
        "test": {"r2_oos": float(ev["r2oos"]), "hit_rate": float(ev["hitRate"]), "n": int(ev["n"])},
    }
    print(f"h={h}s train n={fit['n']} beta={fit['beta']:.3e} | "
          f"test n={ev['n']} R2_oos={ev['r2oos']:.5f} hit={ev['hitRate']:.4f}")

out = REPO / "data" / "ofi-results.json"
out.write_text(json.dumps(results, indent=2), encoding="utf-8")
print("wrote", out)
