# scripts/load_tick.py
"""Build the date-partitioned kdb+ tick db from the recorded session.

    python scripts/load_tick.py [--raw PATH] [--db PATH] [--out PATH] [--skip-raw]

Flushes one date partition per table via .Q.dpft (sym-parted), so reruns
overwrite partitions idempotently. Prints per-table row counts at the end.
Requires licensed pykx (run inside WSL on this machine).
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
Q_EPOCH_OFFSET = 10957  # days between 1970-01-01 and 2000-01-01
SESSION_ID = "replay-verify-20260711-231533"

COLNAMES = {
    "top": ("tsMs", "bid", "bsize", "ask", "asize"),
    "trade": ("tsMs", "side", "price", "size", "tradeId", "tradeSeq"),
}
SYM_COLS = {"side", "strat", "kind", "tradeId"}  # must land as q symbols
# High-cardinality free text: must stay real strings, not symbols, or every
# distinct detail message permanently bloats the process-wide sym file.
STRING_COLS = {"detail"}


def flush(q, out_root: str, day_idx: int, table: str, cols: dict) -> int:
    """Write one date partition for one table via .Q.dpft.

    .Q.dpft's 4th arg is the NAME of a global table, and it writes that
    global into out_root/date/table/. So the staged data must live in a
    global whose name IS the target table name (not a scratch var).
    """
    import pykx
    n = len(next(iter(cols.values())))
    if n == 0:
        return 0
    data = {"sym": ["BTCPERP"] * n}
    data.update(cols)
    q[table] = pykx.Table(data=data)
    # coerce string columns to symbols if pykx delivered them as char lists
    for c in cols:
        if c in SYM_COLS:
            q(f'if[10h=type first {table}`{c}; {table}:@[{table};`{c};`$]]')
    # pykx.Table auto-symbolizes any Python str column; free-text columns
    # (e.g. event detail) must be forced back to real q strings to avoid
    # unbounded symbol-table growth from near-unique text.
    for c in cols:
        if c in STRING_COLS:
            q(f'if[-11h=type first {table}`{c}; {table}:@[{table};`{c};{{string each x}}]]')
    date_val = day_idx - Q_EPOCH_OFFSET
    try:
        q(f'.Q.dpft[`$":{out_root}"; `date${date_val}; `sym; `{table}]')
    except Exception:
        # .Q.dpft requires the table sorted by the parted field (sym here).
        q(f'{table}:`sym xasc {table}')
        q(f'.Q.dpft[`$":{out_root}"; `date${date_val}; `sym; `{table}]')
    q(f'delete {table} from `.')
    return n


def load_raw(q, raw: Path, out_root: str) -> dict:
    counts = defaultdict(int)
    bufs = {t: {c: [] for c in cs} for t, cs in COLNAMES.items()}
    current_day = None
    t0 = time.time()

    def flush_day(day_idx):
        for t in bufs:
            counts[t] += flush(q, out_root, day_idx, t, bufs[t])
            bufs[t] = {c: [] for c in COLNAMES[t]}
        print(f"[raw] flushed day {day_idx} elapsed {time.time()-t0:,.0f}s", flush=True)

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


SQLITE_SPECS = {
    "fill": (
        "SELECT ts_ms, strategy, side, price, amount_usd, mid_at_fill,"
        " adverse_move_usd FROM fills WHERE session_id = ? ORDER BY ts_ms",
        ("tsMs", "strat", "side", "price", "amtUsd", "midAtFill", "advMoveUsd"),
    ),
    "quote": (
        "SELECT ts_ms, strategy, bid, ask, size_usd FROM quotes"
        " WHERE session_id = ? ORDER BY ts_ms",
        ("tsMs", "strat", "bid", "ask", "sizeUsd"),
    ),
    "rollup": (
        "SELECT ts_ms, strategy, position_usd, btc_cash, equity_btc,"
        " equity_usd, mid, fill_count, quote_count, funding_btc FROM rollups"
        " WHERE session_id = ? ORDER BY ts_ms",
        ("tsMs", "strat", "positionUsd", "btcCash", "equityBtc",
         "equityUsd", "mid", "fillCount", "quoteCount", "fundingBtc"),
    ),
    "event": (
        "SELECT ts_ms, strategy, kind, detail FROM events"
        " WHERE session_id = ? ORDER BY ts_ms",
        ("tsMs", "strat", "kind", "detail"),
    ),
}


def load_sqlite(q, db: Path, out_root: str) -> dict:
    conn = sqlite3.connect(db)
    counts = {}
    for table, (sql, cols) in SQLITE_SPECS.items():
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
            print(f"[sqlite] flushed day {day} table {table}", flush=True)
    conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=str(REPO / "data/vps-pull-20260721/raw-20260711-231533.jsonl"))
    ap.add_argument("--db", default=str(REPO / "data/vps-pull-20260721/replay-verify-20260711-231533.sqlite"))
    ap.add_argument("--out", default="/root/tickdb")
    ap.add_argument("--skip-raw", action="store_true", help="only load SQLite tables")
    args = ap.parse_args()
    q = get_q()
    out_root = args.out
    Path(out_root).mkdir(parents=True, exist_ok=True)
    counts = {}
    if not args.skip_raw:
        counts.update(load_raw(q, Path(args.raw), out_root))
    counts.update(load_sqlite(q, Path(args.db), out_root))
    print("row counts:", dict(counts), flush=True)


if __name__ == "__main__":
    main()
