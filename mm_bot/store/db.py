# mm_bot/store/db.py
"""Append-only SQLite persistence (WAL mode) for quotes, fills, rollups."""
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    started_ts_ms INTEGER NOT NULL,
    git_commit TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    bid REAL,
    ask REAL,
    size_usd REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    side TEXT NOT NULL,
    price REAL NOT NULL,
    amount_usd REAL NOT NULL,
    trade_id TEXT NOT NULL,
    mid_at_fill REAL NOT NULL,
    adverse_move_usd REAL
);
CREATE TABLE IF NOT EXISTS rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    strategy TEXT NOT NULL,
    position_usd REAL NOT NULL,
    btc_cash REAL NOT NULL,
    equity_btc REAL NOT NULL,
    equity_usd REAL NOT NULL,
    mid REAL NOT NULL,
    fill_count INTEGER NOT NULL,
    quote_count INTEGER NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    def start_session(
        self, session_id: str, started_ts_ms: int, git_commit: str, config_json: str
    ) -> None:
        self.connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)",
            (session_id, started_ts_ms, git_commit, config_json),
        )
        self.connection.commit()

    def record_quote(
        self, session_id: str, ts_ms: int, strategy: str,
        bid: float | None, ask: float | None, size_usd: float,
    ) -> None:
        self.connection.execute(
            "INSERT INTO quotes (session_id, ts_ms, strategy, bid, ask, size_usd)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, ts_ms, strategy, bid, ask, size_usd),
        )
        self.connection.commit()

    def record_fill(
        self, session_id: str, ts_ms: int, strategy: str, *,
        side: str, price: float, amount_usd: float, trade_id: str, mid_at_fill: float,
    ) -> int:
        cur = self.connection.execute(
            "INSERT INTO fills (session_id, ts_ms, strategy, side, price,"
            " amount_usd, trade_id, mid_at_fill) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts_ms, strategy, side, price, amount_usd, trade_id, mid_at_fill),
        )
        self.connection.commit()
        return cur.lastrowid

    def set_adverse(self, fill_id: int, adverse_move_usd: float) -> None:
        self.connection.execute(
            "UPDATE fills SET adverse_move_usd = ? WHERE id = ?",
            (adverse_move_usd, fill_id),
        )
        self.connection.commit()

    def record_rollup(
        self, session_id: str, ts_ms: int, strategy: str, *,
        position_usd: float, btc_cash: float, equity_btc: float,
        equity_usd: float, mid: float, fill_count: int, quote_count: int,
    ) -> None:
        self.connection.execute(
            "INSERT INTO rollups (session_id, ts_ms, strategy, position_usd,"
            " btc_cash, equity_btc, equity_usd, mid, fill_count, quote_count)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, ts_ms, strategy, position_usd, btc_cash, equity_btc,
             equity_usd, mid, fill_count, quote_count),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()
