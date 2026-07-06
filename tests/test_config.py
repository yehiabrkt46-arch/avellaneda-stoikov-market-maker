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
