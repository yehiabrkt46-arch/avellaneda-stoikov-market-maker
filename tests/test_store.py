# tests/test_store.py
import sqlite3

from mm_bot.store.db import Store


def test_creates_schema_and_session(tmp_path):
    db = tmp_path / "mm.sqlite"
    store = Store(db)
    store.start_session("s1", 1751800000000, "abc123", '{"x":1}')
    store.close()
    con = sqlite3.connect(db)
    row = con.execute("SELECT session_id, started_ts_ms, git_commit FROM sessions").fetchone()
    assert row == ("s1", 1751800000000, "abc123")
    for table in ("quotes", "fills", "rollups", "events"):
        con.execute(f"SELECT * FROM {table}")  # table exists
    con.close()


def test_record_quote_and_rollup(tmp_path):
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    store.record_quote("s1", 1751800000000, "fixed_spread", 59995.0, 60005.0, 100.0)
    store.record_rollup(
        "s1", 1751800060000, "fixed_spread",
        position_usd=100.0, btc_cash=0.002, equity_btc=0.0001,
        equity_usd=6.0, mid=60000.0, fill_count=3, quote_count=60,
    )
    con = store.connection
    assert con.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
    r = con.execute(
        "SELECT position_usd, equity_usd, fill_count FROM rollups"
    ).fetchone()
    assert r == (100.0, 6.0, 3)
    store.close()


def test_record_fill_and_update_adverse(tmp_path):
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    fill_id = store.record_fill(
        "s1", 1751800000000, "fixed_spread",
        side="buy", price=59995.0, amount_usd=50.0,
        trade_id="t9", mid_at_fill=60000.0,
    )
    con = store.connection
    assert con.execute("SELECT adverse_move_usd FROM fills WHERE id=?", (fill_id,)).fetchone()[0] is None
    store.set_adverse(fill_id, -1.5)
    assert con.execute("SELECT adverse_move_usd FROM fills WHERE id=?", (fill_id,)).fetchone()[0] == -1.5
    store.close()


def test_record_event_round_trips(tmp_path):
    store = Store(tmp_path / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    store.record_event("s1", 1751800000000, "fixed_spread", "cap_bind", "side=bid position_usd=500.00")
    store.record_event("s1", 1751800001000, "fixed_spread", "kill_switch", None)
    con = store.connection
    rows = con.execute(
        "SELECT session_id, ts_ms, strategy, kind, detail FROM events ORDER BY id"
    ).fetchall()
    assert rows == [
        ("s1", 1751800000000, "fixed_spread", "cap_bind", "side=bid position_usd=500.00"),
        ("s1", 1751800001000, "fixed_spread", "kill_switch", None),
    ]
    store.close()


def test_creates_parent_dir(tmp_path):
    store = Store(tmp_path / "nested" / "mm.sqlite")
    store.start_session("s1", 0, "c", "{}")
    store.close()
    assert (tmp_path / "nested" / "mm.sqlite").exists()
