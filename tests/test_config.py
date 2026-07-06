from mm_bot.config import load_config


def test_load_config_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert cfg.feed.ws_url == "wss://www.deribit.com/ws/api/v2"
    assert cfg.feed.instrument == "BTC-PERPETUAL"
    assert cfg.feed.book_interval == "100ms"
    assert cfg.feed.heartbeat_interval_s == 30
    assert cfg.feed.stale_data_timeout_s == 10.0
    assert cfg.feed.reconnect_initial_delay_s == 1.0
    assert cfg.feed.reconnect_max_delay_s == 60.0
    assert cfg.recorder.data_dir == "data"


def test_load_config_overrides(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "feed:\n  instrument: ETH-PERPETUAL\n  stale_data_timeout_s: 5.5\n"
        "recorder:\n  data_dir: otherdir\n"
    )
    cfg = load_config(p)
    assert cfg.feed.instrument == "ETH-PERPETUAL"
    assert cfg.feed.stale_data_timeout_s == 5.5
    assert cfg.recorder.data_dir == "otherdir"


def test_load_config_strategy_and_store_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("")
    cfg = load_config(p)
    assert len(cfg.strategies) == 1
    s = cfg.strategies[0]
    assert s.kind == "fixed_spread"
    assert s.name == "fixed_spread"
    assert s.half_spread_usd == 5.0
    assert s.quote_size_usd == 100.0
    assert s.tick_size == 0.5
    assert s.requote_interval_s == 1.0
    assert cfg.store.db_path == "data/mm.sqlite"
    assert cfg.store.rollup_interval_s == 60
    assert cfg.store.adverse_horizon_s == 5.0


def test_load_config_multiple_strategies(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "strategies:\n"
        "  - name: base\n    half_spread_usd: 4.0\n"
        "  - name: wide\n    half_spread_usd: 12.0\n"
        "store:\n  db_path: other.sqlite\n"
    )
    cfg = load_config(p)
    assert [s.name for s in cfg.strategies] == ["base", "wide"]
    assert cfg.strategies[1].half_spread_usd == 12.0
    assert cfg.store.db_path == "other.sqlite"
