# Milestone 8: kdb+/q Tick Store and Research Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Load the 9.66-day recorded session into a date-partitioned kdb+ tick database, reimplement the per-fill edge decomposition in q with a strict parity test against the Python oracle, run an out-of-sample order-flow-imbalance study in q, and move sweep reporting onto q.

**Architecture:** Python stays the live engine and correctness oracle. A new pure-Python extraction module (`mm_bot/research/tickdb.py`, no pykx dependency, fully unit-testable) turns raw JSONL into row batches by reusing the exact same `parse_message` + `OrderBook` code the live engine uses. A thin PyKX loader script writes those batches into a date-partitioned kdb+ db at `data/tick/`. All q analytics live in `.q` files under `q/`, executed through embedded PyKX; every q result is checked against the Python implementation before being trusted.

**Tech Stack:** Python 3.12+, PyKX 4.0 (already in venv), kdb+ (embedded in PyKX, personal license required), q, SQLite (existing), pytest.

---

## Prerequisites and gating

- PyKX 4.0.0 is installed in `.venv`. A **KX personal license** must be installed before any q code runs (`import pykx; pykx.licensed` must be `True`). Sign-up: https://kx.com/kdb-personal-edition-download/ then `python -c "import pykx; pykx.license.install('<b64>')"`.
- Tasks 1-2 need **no pykx at all** and can start immediately.
- Tasks 3+ need the license. Every pykx-dependent test must skip cleanly (not fail) when pykx is missing or unlicensed, so the existing 131-test suite stays green on any machine.
- Primary data (already local):
  - Raw feed: `data/vps-pull-20260721/raw-20260711-231533.jsonl` (3.77 GB, the clean 9.66-day window)
  - Fills/rollups/events oracle: `data/vps-pull-20260721/replay-verify-20260711-231533.sqlite`, session_id `replay-verify-20260711-231533`
  - Sweep results (Phase 4): `data/sweep-results.sqlite` exists on the VPS at `/opt/mm-bot/data/sweep-results.sqlite`; pull via `scp root@v2202606371808473697.ultrasrv.de:/opt/mm-bot/data/sweep-results.sqlite data/` when Task 10 starts.
- Repo conventions: no Claude/Co-Authored-By commit trailers. No em dashes in README prose. Match existing code style (docstrings explaining *why*, minimal comments).

## File structure (whole milestone)

```
mm_bot/research/tickdb.py        NEW  pure-Python JSONL -> row-batch extraction (no pykx)
mm_bot/research/qsession.py      NEW  thin pykx bootstrap: licensed check, load q scripts
q/edge.q                         NEW  edge decomposition in q (closed forms + by-day aggregation)
q/verify.q                       NEW  aj-based independent recomputation of mid_at_fill / adverse move
q/ofi.q                          NEW  order-flow imbalance signal + regression + walk-forward eval
q/report.q                       NEW  sweep/walk-forward report queries
scripts/load_tick.py             NEW  JSONL + SQLite -> partitioned kdb+ db at data/tick/
scripts/run_kdb_parity.py        NEW  full-data q-vs-Python parity check
scripts/run_kdb_verify.py        NEW  full-data aj recomputation report
scripts/run_ofi_study.py         NEW  OFI train/test study, writes data/ofi-results.json
scripts/run_kdb_report.py        NEW  q-generated sweep report
tests/conftest.py                NEW  requires_pykx skip marker
tests/test_tickdb.py             NEW  extraction unit tests (no pykx)
tests/test_qedge.py              NEW  q edge decomposition unit + parity vs Python (licensed only)
tests/test_ofi.py                NEW  OFI unit tests on synthetic book sequences (licensed only)
pyproject.toml                   MOD  add optional extra kdb = ["pykx>=4.0"]
README.md                        MOD  Phase 4: replace kdb section status line with measured results
```

## Math locked in (do not re-derive)

Python oracle (`mm_bot/paper/portfolio.py`): buy of `U` USD at price `p` adds `+U` to position_usd and `+U/p` to btc_cash; `equity_usd(mark) = (btc_cash - position_usd/mark) * mark`.

Closed forms for the q implementation (algebraically identical to the Python round-trip in `mm_bot/research/edge.py`, derived by expanding the two-fill sequence):

- `spread_capture_usd`: buy at `p`, flatten at mid `m`: `U*(m-p)/p`. Sell at `p`, flatten at `m`: `U*(p-m)/p`.
- `adverse_selection_usd = -U * adverse_move_usd / p` for **both** sides (expand total-to-horizon minus spread capture; the sign convention of `adverse_move_usd` from `mm_bot/paper/adverse.py` makes the side terms cancel).

Day bucketing: Python `day_bucket(ts_ms) = ts_ms // 86_400_000`. q: `dayIdx: tsMs div 86400000`. Keep `tsMs` as a long column in every kdb table so joins and bucketing are exact integer ops matching Python; the q `time` timestamp column is derived for humans and aj, never for parity keys.

Adverse-selection convention note (matters for Task 7): the engine resolves adverse move at the **first mid observation at or after** fill_ts + horizon (`AdverseSelectionTracker.on_mid`), while q `aj` picks the **last row at or before** a time. Task 7 therefore reports the convention gap as a distribution, it is not an exact-match assertion.

---

## Phase 1: extraction module + loader

### Task 1: pytest conftest with pykx skip marker

**Files:**
- Create: `tests/conftest.py`

- [ ] **Step 1: Write the conftest**

```python
# tests/conftest.py
"""Shared fixtures. pykx-dependent tests must skip, never fail, when kdb+ is
unavailable: the core 131-test suite has to stay green on machines with no
license (CI, fresh clones)."""
import os

import pytest


def _pykx_licensed() -> bool:
    os.environ.setdefault("PYKX_NOQCE", "1")
    try:
        import pykx
    except Exception:
        return False
    return bool(getattr(pykx, "licensed", False))


requires_pykx = pytest.mark.skipif(
    not _pykx_licensed(), reason="pykx not installed or not licensed"
)
```

- [ ] **Step 2: Run full suite, confirm 131 tests still pass and nothing new breaks**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 131 passed (same as before this task).

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "test: pykx availability skip marker for kdb+ research tests"
```

### Task 2: tickdb extraction module (pure Python, TDD, no pykx)

**Files:**
- Create: `mm_bot/research/tickdb.py`
- Modify: `mm_bot/feed/book.py` (two additive size accessors)
- Test: `tests/test_tickdb.py`, `tests/test_book.py` (one new test)

`extract_rows(lines)` is a generator over raw JSONL lines yielding `("top", row_dict)`, `("trade", row_dict)` tuples. It reuses `parse_message` and `OrderBook` so kdb+ sees the market through exactly the same parser as the live engine. Book gaps mirror `replay_file`: on `GapError`, reset the book and keep going until the next snapshot.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_tickdb.py
"""Extraction from raw JSONL lines to kdb-ready row dicts (no pykx needed)."""
import json

from mm_bot.research.tickdb import extract_rows


def _snap(ts, change_id, bids, asks):
    return json.dumps({
        "method": "subscription",
        "params": {"channel": "book.BTC-PERPETUAL.raw",
                   "data": {"type": "snapshot", "change_id": change_id,
                            "timestamp": ts, "bids": [["new", p, a] for p, a in bids],
                            "asks": [["new", p, a] for p, a in asks]}}})


def _chg(ts, change_id, prev, bids=(), asks=()):
    return json.dumps({
        "method": "subscription",
        "params": {"channel": "book.BTC-PERPETUAL.raw",
                   "data": {"type": "change", "change_id": change_id,
                            "prev_change_id": prev, "timestamp": ts,
                            "bids": [list(b) for b in bids],
                            "asks": [list(a) for a in asks]}}})


def _trade(ts, price, amount, direction, trade_id="t1", seq=1):
    return json.dumps({
        "method": "subscription",
        "params": {"channel": "trades.BTC-PERPETUAL.raw",
                   "data": [{"instrument_name": "BTC-PERPETUAL", "trade_id": trade_id,
                             "trade_seq": seq, "timestamp": ts, "price": price,
                             "amount": amount, "direction": direction}]}})


def test_snapshot_emits_top_row():
    lines = [_snap(1000, 1, [(100.0, 5.0), (99.0, 1.0)], [(101.0, 3.0)])]
    rows = list(extract_rows(lines))
    assert rows == [("top", {"tsMs": 1000, "bid": 100.0, "bsize": 5.0,
                             "ask": 101.0, "asize": 3.0})]


def test_change_updates_top():
    lines = [
        _snap(1000, 1, [(100.0, 5.0)], [(101.0, 3.0)]),
        _chg(2000, 2, 1, bids=[("change", 100.0, 7.0)]),
    ]
    rows = list(extract_rows(lines))
    assert rows[-1] == ("top", {"tsMs": 2000, "bid": 100.0, "bsize": 7.0,
                                "ask": 101.0, "asize": 3.0})


def test_one_sided_book_emits_no_top_row():
    lines = [_snap(1000, 1, [(100.0, 5.0)], [])]
    assert list(extract_rows(lines)) == []


def test_trade_emits_trade_row():
    lines = [_trade(1500, 100.5, 250.0, "buy", trade_id="abc", seq=7)]
    assert list(extract_rows(lines)) == [
        ("trade", {"tsMs": 1500, "side": "buy", "price": 100.5,
                   "size": 250.0, "tradeId": "abc", "tradeSeq": 7})]


def test_gap_resets_until_next_snapshot():
    lines = [
        _snap(1000, 1, [(100.0, 5.0)], [(101.0, 3.0)]),
        _chg(2000, 5, 4),                       # prev mismatch -> gap
        _chg(3000, 6, 5),                       # book uninitialized -> still gapped
        _snap(4000, 10, [(102.0, 2.0)], [(103.0, 1.0)]),
    ]
    rows = list(extract_rows(lines))
    assert [r[1]["tsMs"] for r in rows if r[0] == "top"] == [1000, 4000]


def test_non_book_non_trade_messages_are_skipped():
    ticker = json.dumps({
        "method": "subscription",
        "params": {"channel": "ticker.BTC-PERPETUAL.100ms",
                   "data": {"instrument_name": "BTC-PERPETUAL", "timestamp": 1000,
                            "funding_8h": 1e-5, "mark_price": 100.0}}})
    assert list(extract_rows([ticker])) == []
```

Fixture-shape note: the `_snap`/`_chg`/`_trade` JSON shapes above were written against what `mm_bot/feed/messages.py::parse_message` actually reads (bids/asks as `[action, price, amount]` triples in both snapshot and change; trades as a list of dicts). Before running, diff them against 2-3 real lines from the raw file if in doubt: `head -3 data/vps-pull-20260721/raw-20260711-231533.jsonl`.

- [ ] **Step 2: Run tests, verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tickdb.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'mm_bot.research.tickdb'`

- [ ] **Step 3: Add public size accessors to OrderBook**

In `mm_bot/feed/book.py`, after `best_ask()`:

```python
    def best_bid_size(self) -> float | None:
        bb = self.best_bid()
        return None if bb is None else self._bids[bb]

    def best_ask_size(self) -> float | None:
        ba = self.best_ask()
        return None if ba is None else self._asks[ba]
```

Add to `tests/test_book.py` (match the existing BookSnapshot construction style already used in that file):

```python
def test_best_size_accessors():
    book = OrderBook()
    book.apply_snapshot(BookSnapshot(instrument="BTC-PERPETUAL", change_id=1,
                                     timestamp_ms=1000,
                                     bids=[(100.0, 5.0)], asks=[(101.0, 3.0)]))
    assert book.best_bid_size() == 5.0
    assert book.best_ask_size() == 3.0
```

- [ ] **Step 4: Implement tickdb**

```python
# mm_bot/research/tickdb.py
"""Raw JSONL -> kdb-ready row dicts.

Reuses the exact parser and order book the live engine runs, so the kdb+
tick database sees the market through the same code path as the measured
results; there is no second parse implementation to drift. Gap handling
mirrors mm_bot.paper.replay.replay_file: reset and wait for the next
snapshot.
"""
import json
from collections.abc import Iterable, Iterator

from mm_bot.feed.book import GapError, OrderBook
from mm_bot.feed.messages import BookChange, BookSnapshot, Trade, parse_message


def extract_rows(lines: Iterable[str]) -> Iterator[tuple[str, dict]]:
    book = OrderBook()
    for line in lines:
        for event in parse_message(json.loads(line)):
            if isinstance(event, (BookSnapshot, BookChange)):
                try:
                    if isinstance(event, BookSnapshot):
                        book.apply_snapshot(event)
                    else:
                        book.apply_change(event)
                except GapError:
                    book.reset()
                    continue
                bb, ba = book.best_bid(), book.best_ask()
                if bb is None or ba is None:
                    continue
                yield ("top", {
                    "tsMs": event.timestamp_ms,
                    "bid": bb, "bsize": book.best_bid_size(),
                    "ask": ba, "asize": book.best_ask_size(),
                })
            elif isinstance(event, Trade):
                yield ("trade", {
                    "tsMs": event.timestamp_ms, "side": event.direction,
                    "price": event.price, "size": event.amount,
                    "tradeId": event.trade_id, "tradeSeq": event.trade_seq,
                })
```

- [ ] **Step 5: Run tests, verify green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_tickdb.py tests/test_book.py -q`
Expected: all pass.

- [ ] **Step 6: Full suite + commit**

Run: `.venv/Scripts/python.exe -m pytest -q` — expected: previous count + new tests, all green.

```bash
git add mm_bot/research/tickdb.py tests/test_tickdb.py mm_bot/feed/book.py tests/test_book.py
git commit -m "feat: tick-row extraction from raw JSONL via live-engine parser"
```

### Task 3: qsession bootstrap + pyproject extra

**Files:**
- Create: `mm_bot/research/qsession.py`
- Modify: `pyproject.toml` (add optional extra)

- [ ] **Step 1: Implement qsession**

```python
# mm_bot/research/qsession.py
"""Embedded q bootstrap for the research layer.

Central place for the licensed-pykx check and for loading the repo's .q
scripts, so every script and test gets the same failure message instead of
a pykx stack trace.
"""
import os
from pathlib import Path

Q_DIR = Path(__file__).resolve().parent.parent.parent / "q"


def get_q(scripts: tuple[str, ...] = ()):
    """Return the embedded pykx q instance with the given q/ scripts loaded.

    Raises RuntimeError with an actionable message if pykx is missing or
    unlicensed.
    """
    os.environ.setdefault("PYKX_NOQCE", "1")
    try:
        import pykx
    except ImportError as exc:
        raise RuntimeError(
            "pykx is not installed; run .venv/Scripts/pip install pykx"
        ) from exc
    if not getattr(pykx, "licensed", False):
        raise RuntimeError(
            "pykx is unlicensed; embedded q needs a (free) KX personal "
            "license: https://kx.com/kdb-personal-edition-download/"
        )
    for name in scripts:
        pykx.q((Q_DIR / name).read_text(encoding="utf-8"))
    return pykx.q
```

- [ ] **Step 2: Add pyproject extra**

In `pyproject.toml`, alongside the existing optional-dependencies section (read the file first for its exact shape), add:

```toml
kdb = ["pykx>=4.0"]
```

- [ ] **Step 3: Smoke test (only meaningful once licensed)**

Run: `.venv/Scripts/python.exe -c "from mm_bot.research.qsession import get_q; q = get_q(); print(q('2+2'))"`
Expected once licensed: `4`. If unlicensed: RuntimeError with the sign-up message (that is correct behavior; note it and move on).

- [ ] **Step 4: Commit**

```bash
git add mm_bot/research/qsession.py pyproject.toml
git commit -m "feat: embedded q session bootstrap and kdb optional extra"
```

### Task 4: partitioned-db loader script

**Files:**
- Create: `scripts/load_tick.py`
- Modify: `.gitignore` (add `data/tick/` if `data/` is not already ignored)

Loads (a) `top`/`trade` from the raw JSONL via `extract_rows`, buffering rows per UTC date and flushing each date once with `.Q.dpft`, and (b) `fill`/`quote`/`rollup`/`event` from the replay-verify SQLite. Instrument is constant so `sym` is always `` `BTCPERP `` (kept for canonical partitioned-db layout and the parted attribute). Chronological input means date rollover triggers the flush. Roughly 1.5M top rows per day is comfortably in memory.

- [ ] **Step 1: Write the loader**

```python
# scripts/load_tick.py
"""Build the date-partitioned kdb+ tick db at data/tick/ from the recorded
session. Usage:

    .venv/Scripts/python.exe scripts/load_tick.py [--raw PATH] [--db PATH] [--out PATH] [--skip-raw]

Flushes one date partition at a time via .Q.dpft (sym-parted), so reruns
overwrite partitions idempotently. Prints per-table row counts at the end;
verify them against SQLite/JSONL before trusting the db.
"""
import argparse
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.qsession import get_q  # noqa: E402
from mm_bot.research.tickdb import extract_rows  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DAY_MS = 86_400_000
SESSION_ID = "replay-verify-20260711-231533"

COLNAMES = {
    "top": ("tsMs", "bid", "bsize", "ask", "asize"),
    "trade": ("tsMs", "side", "price", "size", "tradeId", "tradeSeq"),
}


def flush(q, out_root: Path, day_idx: int, table: str, cols: dict) -> int:
    """Write one date partition for one table via .Q.dpft."""
    import pykx
    n = len(next(iter(cols.values())))
    if n == 0:
        return 0
    tbl = pykx.Table(data={"sym": ["BTCPERP"] * n, **cols})
    q["_stage"] = tbl
    q("_dt", pykx.q("{`date$x}", pykx.LongAtom(day_idx)))  # epoch-day -> q date
    q(f'.Q.dpft[`:{out_root.as_posix()}; _dt; `sym; `_stage]')
    q("delete _stage from `.")
    return n


def load_raw(q, raw: Path, out_root: Path) -> dict:
    counts = defaultdict(int)
    bufs = {t: {c: [] for c in cs} for t, cs in COLNAMES.items()}
    current_day = None
    t0 = time.time()

    def flush_day(day_idx):
        for t in bufs:
            counts[t] += flush(q, out_root, day_idx, t, bufs[t])
            bufs[t] = {c: [] for c in COLNAMES[t]}
        print(f"flushed day {day_idx} elapsed {time.time()-t0:,.0f}s", flush=True)

    with open(raw, encoding="utf-8") as fh:
        for table, row in extract_rows(fh):
            day = row["tsMs"] // DAY_MS
            if current_day is None:
                current_day = day
            elif day != current_day:
                flush_day(current_day)
                current_day = day
            b = bufs[table]
            for c in COLNAMES[table]:
                b[c].append(row[c])
    if current_day is not None:
        flush_day(current_day)
    return counts


def load_sqlite(q, db: Path, out_root: Path) -> dict:
    conn = sqlite3.connect(db)
    counts = {}
    specs = {
        "fill": ("SELECT ts_ms, strategy, side, price, amount_usd, mid_at_fill,"
                 " adverse_move_usd FROM fills WHERE session_id = ? ORDER BY ts_ms",
                 ("tsMs", "strat", "side", "price", "amtUsd", "midAtFill", "advMoveUsd")),
        "quote": ("SELECT ts_ms, strategy, bid, ask, size_usd FROM quotes"
                  " WHERE session_id = ? ORDER BY ts_ms",
                  ("tsMs", "strat", "bid", "ask", "sizeUsd")),
        "rollup": ("SELECT ts_ms, strategy, position_usd, btc_cash, equity_btc,"
                   " equity_usd, mid, fill_count, quote_count, funding_btc FROM rollups"
                   " WHERE session_id = ? ORDER BY ts_ms",
                   ("tsMs", "strat", "positionUsd", "btcCash", "equityBtc",
                    "equityUsd", "mid", "fillCount", "quoteCount", "fundingBtc")),
        "event": ("SELECT ts_ms, strategy, kind, detail FROM events"
                  " WHERE session_id = ? ORDER BY ts_ms",
                  ("tsMs", "strat", "kind", "detail")),
    }
    for table, (sql, cols) in specs.items():
        rows = conn.execute(sql, (SESSION_ID,)).fetchall()
        counts[table] = len(rows)
        by_day = defaultdict(lambda: {c: [] for c in cols})
        for r in rows:
            day = r[0] // DAY_MS
            for c, v in zip(cols, r):
                if v is None:
                    v = "" if c == "detail" else float("nan")
                by_day[day][c].append(v)
        for day, colmap in sorted(by_day.items()):
            flush(q, out_root, day, table, colmap)
    conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(REPO / "data/vps-pull-20260721/raw-20260711-231533.jsonl"))
    ap.add_argument("--db", default=str(REPO / "data/vps-pull-20260721/replay-verify-20260711-231533.sqlite"))
    ap.add_argument("--out", default=str(REPO / "data/tick"))
    ap.add_argument("--skip-raw", action="store_true", help="only load SQLite tables")
    args = ap.parse_args()
    q = get_q()
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    counts = {}
    if not args.skip_raw:
        counts.update(load_raw(q, Path(args.raw), out_root))
    counts.update(load_sqlite(q, Path(args.db), out_root))
    print("row counts:", dict(counts), flush=True)


if __name__ == "__main__":
    main()
```

Implementation warnings for the engineer (resolve these against real pykx behavior, they are the likely failure points):
- The `_dt` assignment shape (`q("_dt", ...)`) is illustrative; the working pattern may be `q['_dt'] = pykx.q('`date$', day_idx)` or simply inlining the date literal: compute `date_str = (epoch date 2000.01.01 offset)`; simplest robust form is `q(f'_dt:`date$ {day_idx} - 10957')` since q dates count from 2000.01.01 and epoch-day 10957 = 2000.01.01. Verify: epoch-day for 2026-07-11 is 20645; `20645 - 10957 = 9688`; `q(') `date$ 9688'` must print `2026.07.11`. Pick one form, test it in the smoke run, keep it.
- `.Q.dpft` requires the table sorted by the parted field; `sym` is constant so any order works, but if it signals `'s-fail`, sort first: `q('`_stage set `sym xasc _stage')`.
- One date's SQLite tables may flush after `top`/`trade` for the same date; different table names in one partition dir are fine. Each (date, table) pair is written exactly once (SQLite tables grouped fully in memory first), so `.Q.dpft` overwrite semantics are safe.
- `advMoveUsd` None -> NaN matters: q reads NaN as float null (`0n`), which is what edge.q filters on. `detail` None -> `""` (string column, not float).
- String columns (`side`, `strat`, `kind`, `tradeId`) should land as symbols. Check after the smoke run with `meta` on a loaded partition; coerce in the flush if they landed as char lists.
- After the full load, run one compaction pass to set the sorted attribute on tsMs per partition if aj performance in later tasks is poor: not required up front, `aj` works without it, just slower. Note it as an option, do not gold-plate now.

- [ ] **Step 2: Dry-run on a small slice (licensed machine)**

PowerShell:
```powershell
Get-Content data\vps-pull-20260721\raw-20260711-231533.jsonl -TotalCount 200000 | Set-Content -Encoding utf8 $env:TEMP\raw-head.jsonl
.venv\Scripts\python.exe scripts\load_tick.py --raw $env:TEMP\raw-head.jsonl --out data\tick-smoke
.venv\Scripts\python.exe -c "from mm_bot.research.qsession import get_q; q = get_q(); q('system\"l data/tick-smoke\"'); print(q('select n:count i by date from top')); print(q('meta select from top where date=first date'))"
```
Expected: nonzero top counts, symbol `sym` column, float bid/ask columns, long tsMs. Delete `data/tick-smoke` afterward.

- [ ] **Step 3: Full load**

Run: `.venv/Scripts/python.exe scripts/load_tick.py` (expect roughly 10-25 min; progress prints per day flush).
Then verify counts:
```bash
.venv/Scripts/python.exe -c "import sqlite3; c = sqlite3.connect('data/vps-pull-20260721/replay-verify-20260711-231533.sqlite'); print(c.execute('SELECT strategy, COUNT(*) FROM fills GROUP BY strategy').fetchall())"
.venv/Scripts/python.exe -c "from mm_bot.research.qsession import get_q; q = get_q(); q('system\"l data/tick\"'); print(q('select n:count i by strat from fill')); print(q('count select from trade'))"
```
Expected: fill counts match SQLite exactly (fixed_spread 27198, avellaneda_stoikov 33538).

- [ ] **Step 4: Gitignore + commit**

Check `.gitignore`; if `data/` is not already ignored, add `data/tick/`.

```bash
git add scripts/load_tick.py .gitignore
git commit -m "feat: kdb+ partitioned tick db loader (raw JSONL + replay SQLite)"
```

## Phase 2: edge decomposition in q + parity

### Task 5: q/edge.q + unit parity test

**Files:**
- Create: `q/edge.q`
- Test: `tests/test_qedge.py`

- [ ] **Step 1: Write q/edge.q**

```q
/ q/edge.q
/ Per-fill edge decomposition, closed forms proven equal to the Python
/ Portfolio round-trip (see plan 2026-07-23, "Math locked in"):
/   spread capture: buy U*(m-p)%p, sell U*(p-m)%p
/   adverse selection: neg U*adv%p (both sides)
/ Python (mm_bot/research/edge.py) stays the oracle; tests require agreement.

decompose:{[f]
  f:select tsMs, strat, side, p:price, U:amtUsd, m:midAtFill, adv:advMoveUsd
    from f where not null advMoveUsd;
  update scUsd:U*?[side=`buy;(m-p)%p;(p-m)%p], asUsd:neg U*adv%p from f}

edgeByDay:{[f]
  select scUsd:sum scUsd, asUsd:sum asUsd, n:count i
    by strat, dayIdx:tsMs div 86400000 from decompose f}
```

- [ ] **Step 2: Write the parity unit test (synthetic fills, licensed-only)**

```python
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

    q["fills"] = pykx.Table(data={
        "tsMs": [r[0] for r in FILLS],
        "strat": [r[1] for r in FILLS],
        "side": [r[2] for r in FILLS],
        "price": [r[3] for r in FILLS],
        "amtUsd": [r[4] for r in FILLS],
        "midAtFill": [r[5] for r in FILLS],
        "advMoveUsd": [float("nan") if r[6] is None else r[6] for r in FILLS],
    })
    q_result = q("0! edgeByDay fills").pd()

    assert len(q_result) == len(python_result)
    for _, row in q_result.iterrows():
        key = (str(row["strat"]), int(row["dayIdx"]))
        py = python_result[key]
        assert row["n"] == py["fill_count"]
        assert math.isclose(row["scUsd"], py["spread_capture_usd"], abs_tol=1e-9)
        assert math.isclose(row["asUsd"], py["adverse_selection_usd"], abs_tol=1e-9)
```

- [ ] **Step 3: Run, verify**

Run: `.venv/Scripts/python.exe -m pytest tests/test_qedge.py -v`
Expected: PASS on the licensed machine (SKIP means blocked on license; hold the commit until it has actually run green once).

- [ ] **Step 4: Commit**

```bash
git add q/edge.q tests/test_qedge.py
git commit -m "feat: edge decomposition in q with parity test against Python oracle"
```

### Task 6: full-data parity script

**Files:**
- Create: `scripts/run_kdb_parity.py`

- [ ] **Step 1: Write the script**

```python
# scripts/run_kdb_parity.py
"""Recompute the full 9.66-day edge decomposition in q from data/tick and in
Python from the replay SQLite, and require per-(strategy, day) agreement.
Exit code 0 = parity holds; nonzero with a printed diff = investigate q.
"""
import math
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.edge import aggregate_fill_edge_by_day  # noqa: E402
from mm_bot.research.qsession import get_q  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data/vps-pull-20260721/replay-verify-20260711-231533.sqlite"
SESSION_ID = "replay-verify-20260711-231533"
TOL_USD = 1e-6

q = get_q(scripts=("edge.q",))
q(f'system "l {(REPO / "data/tick").as_posix()}"')
q_rows = q("0! edgeByDay select from fill").pd()

py = aggregate_fill_edge_by_day(sqlite3.connect(DB), SESSION_ID)

failures = []
if len(q_rows) != len(py):
    failures.append(f"bucket count: q={len(q_rows)} python={len(py)}")
for _, row in q_rows.iterrows():
    key = (str(row["strat"]), int(row["dayIdx"]))
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
```

- [ ] **Step 2: Run against the real tick db**

Run: `.venv/Scripts/python.exe scripts/run_kdb_parity.py`
Expected: `PARITY OK ...`, exit 0. If it fails, debug the q side (symbol vs string strat comparison in `.pd()` output is the usual suspect). The Python side is the oracle: never widen TOL_USD to make it pass; find the actual cause.

- [ ] **Step 3: Record the numbers for the README**

Run and save output: `.venv/Scripts/python.exe -c "from mm_bot.research.qsession import get_q; q = get_q(scripts=('edge.q',)); q('system\"l data/tick\"'); print(q('select sum scUsd, sum asUsd, n:count i by strat from decompose select from fill'))"`
Report bucket count and totals in the task summary; Task 11 needs them.

- [ ] **Step 4: Commit**

```bash
git add scripts/run_kdb_parity.py
git commit -m "feat: full-window q vs Python edge decomposition parity check"
```

### Task 7: aj-based independent recomputation (q/verify.q)

**Files:**
- Create: `q/verify.q`
- Create: `scripts/run_kdb_verify.py`

This is the *stronger* check: instead of trusting the stored `midAtFill`/`advMoveUsd`, recompute both from the `top` table with asof joins and report how well the engine's stored values are reproduced. `midAtFill` should match essentially exactly (the engine's mid at a fill is the book mid from the same event stream `top` was built from). The adverse move uses a different convention (engine: first observation at or after t+horizon; aj: last at or before), so it is reported as a distribution, not asserted exactly.

- [ ] **Step 1: Write q/verify.q**

```q
/ q/verify.q
/ Independent recomputation of fill-time mid and forward mid from the top
/ table via asof joins. midAtFill should reproduce the engine's stored value
/ almost everywhere; advMove uses a next-observation convention in the
/ engine, so aj (last-at-or-before) differs at feed gaps; report, don't assert.

mids:{[] select sym, tsMs, mid:0.5*bid+ask from top}

/ stored vs aj-recomputed mid at fill time
checkMidAtFill:{[]
  f:select sym, tsMs, strat, side, midAtFill from fill;
  j:aj[`sym`tsMs; f; mids[]];
  select nFills:count i,
         nExact:sum 1e-9>abs midAtFill-mid,
         maxAbsDiff:max abs midAtFill-mid
    by strat from j}

/ adverse move via aj at t+horizon (last mid at or before), vs stored
checkAdvMove:{[horizonMs]
  f:select sym, tsMs, strat, side, midAtFill, advMoveUsd from fill
    where not null advMoveUsd;
  fwd:select sym, tsMs, fwdMid:mid from mids[];
  j:aj[`sym`tsMs; update tsMs:tsMs+horizonMs from f; fwd];
  j:update ajMove:?[side=`buy; midAtFill-fwdMid; fwdMid-midAtFill] from j;
  d:select strat, ad:abs advMoveUsd-ajMove from j;
  select nFills:count i, p50AbsDiff:med ad, maxAbsDiff:max ad by strat from d}
```

(If a p99 is wanted in the report, add `p99AbsDiff:ad iasc[ad] -1+ceiling 0.99*count ad` inside a per-strat lambda; med and max are the required outputs, do not fight q for percentile syntax if it stalls the task.)

- [ ] **Step 2: Write the runner**

```python
# scripts/run_kdb_verify.py
"""aj-based independent recomputation report; see q/verify.q for semantics."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.qsession import get_q  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
HORIZON_MS = 5000  # config.yaml store.adverse_horizon_s * 1000 -- verify against config.yaml

q = get_q(scripts=("verify.q",))
q(f'system "l {(REPO / "data/tick").as_posix()}"')
print("mid-at-fill reproduction (stored vs aj):")
print(q("checkMidAtFill[]"))
print(f"\nadverse move at {HORIZON_MS}ms (stored next-obs vs aj last-obs):")
print(q(f"checkAdvMove[{HORIZON_MS}]"))
print("\nreference scale: median |advMoveUsd| =",
      q("exec med abs advMoveUsd from fill where not null advMoveUsd"))
```

- [ ] **Step 3: Run, sanity-check the report**

Run: `.venv/Scripts/python.exe scripts/run_kdb_verify.py`
Expected shape: `nExact/nFills` close to 1.0 for midAtFill per strategy. If below ~0.99, the likely cause is timestamp ties (fill's trade event and a book event at the same tsMs; aj takes the last at equal time, the engine used the book state before the trade in stream order). Report the observed rate honestly rather than chasing exactness. Adverse p50AbsDiff should be small relative to the printed median |advMoveUsd| reference scale.

- [ ] **Step 4: Commit**

```bash
git add q/verify.q scripts/run_kdb_verify.py
git commit -m "feat: aj-based independent recomputation of fill mids and adverse moves"
```

## Phase 3: OFI study

### Task 8: q/ofi.q + unit tests

**Files:**
- Create: `q/ofi.q`
- Test: `tests/test_ofi.py`

- [ ] **Step 1: Write q/ofi.q**

```q
/ q/ofi.q
/ Order-flow imbalance (Cont, Kukanov, Stoikov 2014): per book update
/   e = 1[b_t>=b_{t-1}]*q^b_t - 1[b_t<=b_{t-1}]*q^b_{t-1}
/     - 1[a_t<=a_{t-1}]*q^a_t + 1[a_t>=a_{t-1}]*q^a_{t-1}
/ summed over updates in a time bucket. Predictor of short-horizon mid moves.

ofiEvents:{[t]
  t:update pb:prev bid, pa:prev ask, pbs:prev bsize, pas:prev asize from
    `tsMs xasc select tsMs, bid, bsize, ask, asize from t;
  t:1_ t;  / first row has no prev
  update e:((?[bid>=pb;bsize;0f])-?[bid<=pb;pbs;0f])
          -((?[ask<=pa;asize;0f])-?[ask>=pa;pas;0f]) from t}

/ bucketed OFI + end-of-bucket mid, bucketMs e.g. 1000
ofiBuckets:{[t;bucketMs]
  e:ofiEvents t;
  select ofi:sum e, mid:last 0.5*bid+ask by bkt:bucketMs xbar tsMs from e}

/ forward return h buckets ahead (null-padded tail)
fwdShift:{[h;v] (h _ v),h#0n}
withFwdRet:{[b;h] update fwdRet:(fwdShift[h;mid]%mid)-1 from b}

/ OLS y = a + b*x on non-null pairs
fitOls:{[x;y]
  ok:where (not null x) and not null y; x:x ok; y:y ok;
  b:cov[x;y]%var x; a:avg[y]-b*avg x;
  `alpha`beta`n!(a;b;count ok)}

/ out-of-sample R2 and directional hit rate of a fitted model
evalOls:{[m;x;y]
  ok:where (not null x) and not null y; x:x ok; y:y ok;
  pred:m[`alpha]+m[`beta]*x;
  ss:sum d*d:y-pred; tot:sum d2*d2:y-avg y;
  `r2oos`hitRate`n!((1-ss%tot); avg (signum pred)=signum y; count ok)}
```

Note: `ofiBuckets` groups by `bkt` on the *event* table, so `mid` inside the group is well-defined (last event's mid in the bucket). `withFwdRet` assumes the bucket table is keyed and sorted ascending by bkt; `select ... by bkt` returns it keyed and sorted; `update` on a keyed table preserves keys — call `0!` before handing to fitOls if column extraction misbehaves.

- [ ] **Step 2: Unit tests on synthetic sequences**

```python
# tests/test_ofi.py
"""OFI on hand-computed book sequences; regression on a planted linear signal."""
import math

from tests.conftest import requires_pykx


@requires_pykx
def test_ofi_hand_computed():
    import pykx
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("ofi.q",))
    # Hand computation against the formula in ofi.q's header comment:
    # row2 (1100): bid 100->100 (>= and <= both true): e_b = 7 - 5 = 2
    #              ask 101->101: e_a = 3 - 3 = 0                e = 2
    # row3 (1200): bid 100->99 (down): e_b = 0 - 7 = -7
    #              ask same: e_a = 0                            e = -7
    # row4 (1300): bid same 99: e_b = 4 - 4 = 0
    #              ask 101->100 (down): e_a = 6 - 0 = 6         e = 0 - 6 = -6
    rows = [
        (1000, 100.0, 5.0, 101.0, 3.0),
        (1100, 100.0, 7.0, 101.0, 3.0),
        (1200,  99.0, 4.0, 101.0, 3.0),
        (1300,  99.0, 4.0, 100.0, 6.0),
    ]
    q["t"] = pykx.Table(data={
        "tsMs": [r[0] for r in rows], "bid": [r[1] for r in rows],
        "bsize": [r[2] for r in rows], "ask": [r[3] for r in rows],
        "asize": [r[4] for r in rows]})
    e = q("exec e from ofiEvents t").py()
    assert e == [2.0, -7.0, -6.0]


@requires_pykx
def test_ols_recovers_planted_line():
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("ofi.q",))
    q("x:0.01*til 500; y:3.0+2.0*x")
    m = q("fitOls[x;y]").py()
    assert abs(m["alpha"] - 3.0) < 1e-9
    assert abs(m["beta"] - 2.0) < 1e-9
    ev = q("evalOls[fitOls[x;y];x;y]").py()
    assert ev["r2oos"] > 0.999999


@requires_pykx
def test_fwd_shift_pads_with_null():
    from mm_bot.research.qsession import get_q

    q = get_q(scripts=("ofi.q",))
    out = q("fwdShift[2; 1.0 2.0 3.0 4.0]").py()
    assert out[:2] == [3.0, 4.0]
    assert math.isnan(out[2]) and math.isnan(out[3])
```

If `test_ofi_hand_computed` disagrees with the q output, re-derive the hand computation from the formula comment first; the formula comment is the spec, fix whichever side departs from it.

- [ ] **Step 3: Run, verify green**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ofi.py -v`
Expected: 3 passed on the licensed machine.

- [ ] **Step 4: Commit**

```bash
git add q/ofi.q tests/test_ofi.py
git commit -m "feat: order-flow imbalance signal and OLS evaluation in q"
```

### Task 9: OFI study script on real data

**Files:**
- Create: `scripts/run_ofi_study.py`

- [ ] **Step 1: Check the walkforward split call site**

Read `mm_bot/research/walkforward.py` and the `--report` section of `scripts/run_param_sweep.py`. The OFI study must call `train_test_split_days` with exactly the same arguments the sweep report used, so the 8-train/3-test day split is identical. Copy that invocation.

- [ ] **Step 2: Write the study script**

```python
# scripts/run_ofi_study.py
"""OFI predictive study on the recorded window, walk-forward evaluated.

Fits OLS of forward mid return on bucketed OFI using train days only
(identical chronological split to the parameter sweep), reports
out-of-sample R2 and directional hit rate on the held-out days.
Writes data/ofi-results.json. A near-zero or negative held-out R2 is a
publishable negative result, per project discipline; do not tune on test.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mm_bot.research.qsession import get_q  # noqa: E402
from mm_bot.research.walkforward import train_test_split_days  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BUCKET_MS = 1000
HORIZONS = (1, 5, 10)  # buckets ahead = seconds at BUCKET_MS=1000

q = get_q(scripts=("ofi.q",))
q(f'system "l {(REPO / "data/tick").as_posix()}"')

days = sorted(q("exec distinct tsMs div 86400000 from top").py())
train_days, test_days = train_test_split_days(days)  # match sweep invocation (Task 9 Step 1)
print(f"days: {len(days)} train: {train_days} test: {test_days}")
q["trainDays"] = train_days
q["testDays"] = test_days

results = {}
for h in HORIZONS:
    q(f"b:0! withFwdRet[ofiBuckets[select from top;{BUCKET_MS}];{h}]")
    q("bTrain:select from b where (bkt div 86400000) in trainDays")
    q("bTest:select from b where (bkt div 86400000) in testDays")
    q("m:fitOls[exec ofi from bTrain; exec fwdRet from bTrain]")
    fit = q("m").py()
    ev = q("evalOls[m; exec ofi from bTest; exec fwdRet from bTest]").py()
    results[f"h{h}s"] = {
        "bucket_ms": BUCKET_MS,
        "horizon_buckets": h,
        "train": {"alpha": fit["alpha"], "beta": fit["beta"], "n": int(fit["n"])},
        "test": {"r2_oos": ev["r2oos"], "hit_rate": ev["hitRate"], "n": int(ev["n"])},
    }
    print(f"h={h}s train n={fit['n']} beta={fit['beta']:.3e} | "
          f"test n={ev['n']} R2_oos={ev['r2oos']:.5f} hit={ev['hitRate']:.4f}")

out = REPO / "data" / "ofi-results.json"
out.write_text(json.dumps(results, indent=2), encoding="utf-8")
print("wrote", out)
```

Memory note: `select from top` pulls the whole window into memory (~600 MB order of magnitude). Acceptable locally. If it wedges, fall back to per-date processing (`raze` over date partitions) and disclose the one-event-per-day-boundary seam in the output.

Important honesty guard: the forward return crosses day boundaries inside `withFwdRet` (buckets are contiguous across days). At the train/test boundary this leaks up to h seconds of test data into the last train target. At h <= 10 s against multi-day blocks this is negligible, but state it in the README sentence if any horizon result is borderline; alternatively drop the last h buckets of each train day (one `where` clause) if the reviewer prefers strictness.

- [ ] **Step 3: Run the study**

Run: `.venv/Scripts/python.exe scripts/run_ofi_study.py`
Expected: per-horizon train beta and held-out R2/hit rate printed, `data/ofi-results.json` written. The numbers are whatever they are.

- [ ] **Step 4: Commit (script only; json is data)**

```bash
git add scripts/run_ofi_study.py
git commit -m "feat: out-of-sample OFI predictive study on recorded window"
```

## Phase 4: reporting + README results

### Task 10: q sweep report

**Files:**
- Create: `q/report.q`
- Create: `scripts/run_kdb_report.py`
- Data prerequisite: `scp root@v2202606371808473697.ultrasrv.de:/opt/mm-bot/data/sweep-results.sqlite data/`

- [ ] **Step 1: Pull the sweep db and inspect schema**

Run the scp above, then:
`.venv/Scripts/python.exe -c "import sqlite3; c = sqlite3.connect('data/sweep-results.sqlite'); print(c.execute('SELECT name FROM sqlite_master WHERE type=\'table\'').fetchall()); print(c.execute('SELECT session_id, COUNT(*) FROM fills GROUP BY session_id').fetchall())"`
Expected: 12 sweep sessions named `sweep-<kind>-<params>`. Also read `scripts/run_param_sweep.py` `--report` for the exact score definition and split invocation; q/report.q must mirror it.

- [ ] **Step 2: Write q/report.q**

```q
/ q/report.q
/ Sweep report: per-candidate per-day edge score (spread capture + adverse
/ selection, same definition as scripts/run_param_sweep.py --report), then
/ walk-forward totals per candidate: train-day sum picks the winner, held-out
/ test-day sum reported alongside.

scoreByDay:{[f]
  select score:sum scUsd+asUsd by session, dayIdx:tsMs div 86400000
    from update scUsd:U*?[side=`buy;(m-p)%p;(p-m)%p], asUsd:neg U*adv%p
    from select tsMs, session, side, p:price, U:amtUsd, m:midAtFill, adv:advMoveUsd
    from f where not null advMoveUsd}

report:{[s;trainDays;testDays]
  t:select train:sum score by session from s where dayIdx in trainDays;
  h:select test:sum score by session from s where dayIdx in testDays;
  `train xdesc 0! t lj h}
```

- [ ] **Step 3: Write the runner**

```python
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
import pykx  # noqa: E402  (safe: get_q already verified licensed import)

conn = sqlite3.connect(DB)
rows = conn.execute(
    "SELECT session_id, ts_ms, side, price, amount_usd, mid_at_fill,"
    " adverse_move_usd FROM fills ORDER BY ts_ms").fetchall()
q["sweepFills"] = pykx.Table(data={
    "session": [r[0] for r in rows], "tsMs": [r[1] for r in rows],
    "side": [r[2] for r in rows], "price": [r[3] for r in rows],
    "amtUsd": [r[4] for r in rows], "midAtFill": [r[5] for r in rows],
    "advMoveUsd": [float("nan") if r[6] is None else r[6] for r in rows]})

days = sorted(q("exec distinct tsMs div 86400000 from sweepFills").py())
train_days, test_days = train_test_split_days(days)  # same invocation as sweep --report
q["trainDays"] = train_days
q["testDays"] = test_days
q("s:scoreByDay sweepFills")
print("per-candidate train/test scores (train desc):")
print(q("report[s;trainDays;testDays]"))
```

- [ ] **Step 4: Run and cross-check against the Python report**

Run: `.venv/Scripts/python.exe scripts/run_kdb_report.py`
Then the existing Python report (check `run_param_sweep.py` for its flags and expected db path; copy the db or pass the path as it expects).
Expected: identical winners (fixed_spread half_spread 8.0: train -23.57 / test -2.86; avellaneda_stoikov gamma 0.002 horizon 120: train -68.66 / test -19.42) and per-candidate totals matching to 1e-6. Disagreement means q/report.q drifted from the Python score definition; fix report.q.

- [ ] **Step 5: Commit**

```bash
git add q/report.q scripts/run_kdb_report.py
git commit -m "feat: sweep walk-forward report in q, cross-checked against Python report"
```

### Task 11: README results update + push

**Files:**
- Modify: `README.md` (the "kdb+/q tick store and research layer" section)

- [ ] **Step 1: Replace the Status paragraph with measured results**

Replace the final paragraph of the kdb+ section (starts `Status: the tick database loader ...`) with real numbers from Tasks 6, 7, 9, 10. Placeholders in ALL CAPS must be replaced with measured values, never committed as-is:

```markdown
Measured results from this layer:

- Parity: the q edge decomposition reproduces the Python implementation on all NBUCKETS (strategy, day) buckets of the 9.66-day window to within 1e-6 USD. The aj-based independent recomputation reproduces the engine's stored fill-time mids exactly for MIDEXACTPCT of fills; the adverse-move comparison differs only through a stated observation-convention difference, median absolute difference MEDADVDIFF USD.
- OFI study: on held-out days (same 8 train / 3 test chronological split as the parameter sweep), bucketed order-flow imbalance predicts the H-second forward mid return with out-of-sample R2 of R2VALUE and directional hit rate HITRATE. INTERPRETATION_SENTENCE_MATCHING_ACTUAL_NUMBERS.
- The sweep walk-forward report reruns in q and reproduces the Python winners and scores above.
```

Style rules: no em dashes, no double hyphens, honest framing. If R2_oos is near zero or negative, the interpretation sentence says the signal did not survive out of sample at these horizons; a disclosed negative result is consistent with the rest of the README.

- [ ] **Step 2: Run the full test suite one last time**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: all green (core suite + tickdb + qedge + ofi on the licensed machine).

- [ ] **Step 3: Commit and push**

```bash
git add README.md
git commit -m "docs: kdb+/q layer measured results (parity, aj verification, OFI study)"
git -c credential.helper= -c credential.helper='!gh auth git-credential' push origin main
```

---

## Self-review notes

- Spec coverage: Phase 1 loader (Tasks 1-4), Phase 2 parity (5-7), Phase 3 OFI (8-9), Phase 4 reporting + README (10-11). All four phases have tasks.
- The Python engine and oracle are never modified except two additive OrderBook accessors with tests.
- Every pykx-dependent test skips cleanly when unlicensed; core suite stays green everywhere.
- Judgment calls delegated with explicit instructions: q date-literal construction in the loader flush (Task 4 warning), pykx str->symbol coercion check (Task 4), `train_test_split_days` exact invocation copied from the sweep report (Tasks 9, 10).
- Deliberate scope cuts: no `depth` table / multi-level OFI (top-of-book OFI only), no A-S strategy overlay replay (22 min/pass; predictive study first, overlay is a future milestone only if OFI survives out of sample), no q reporting of live rollups.
