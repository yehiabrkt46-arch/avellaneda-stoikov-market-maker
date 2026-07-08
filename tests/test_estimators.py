# tests/test_estimators.py
import pytest

from mm_bot.strategy.estimators import EwmaVolatility, TradeIntensity


def test_vol_not_warm_before_min_samples():
    vol = EwmaVolatility(lam=0.5, min_dt_s=1.0, min_samples=3)
    vol.observe(60000.0, 1_000_000)
    vol.observe(60002.0, 1_001_000)  # 1 sample (first obs is just the anchor)
    assert not vol.warm
    assert vol.sigma2() is None


def test_vol_constant_diffs_converge_to_known_variance():
    vol = EwmaVolatility(lam=0.5, min_dt_s=1.0, min_samples=3)
    ts = 1_000_000
    vol.observe(60000.0, ts)
    for i in range(1, 6):  # +2.0 USD exactly every 1s -> var_sample = 4 every time
        vol.observe(60000.0 + 2.0 * i, ts + 1000 * i)
    assert vol.warm
    assert vol.sigma2() == pytest.approx(4.0)


def test_vol_ignores_samples_closer_than_min_dt():
    vol = EwmaVolatility(lam=0.5, min_dt_s=1.0, min_samples=2)
    vol.observe(60000.0, 1_000_000)
    vol.observe(70000.0, 1_000_100)  # 0.1s later: ignored entirely
    vol.observe(60002.0, 1_001_000)
    vol.observe(60004.0, 1_002_000)
    assert vol.sigma2() == pytest.approx(4.0)


def test_intensity_mle_is_inverse_mean_distance():
    k = TradeIntensity(window_s=3600.0, min_trades=3)
    k.observe(10.0, 1_000_000)
    k.observe(20.0, 1_001_000)
    k.observe(30.0, 1_002_000)
    assert k.warm
    assert k.k() == pytest.approx(1.0 / 20.0)


def test_intensity_not_warm_below_min_trades():
    k = TradeIntensity(window_s=3600.0, min_trades=3)
    k.observe(10.0, 1_000_000)
    k.observe(20.0, 1_001_000)
    assert not k.warm
    assert k.k() is None


def test_intensity_evicts_outside_window():
    k = TradeIntensity(window_s=10.0, min_trades=2)
    k.observe(100.0, 1_000_000)
    k.observe(10.0, 1_020_000)  # first trade now 20s old, window 10s -> evicted
    k.observe(30.0, 1_021_000)
    assert k.k() == pytest.approx(1.0 / 20.0)  # mean of (10, 30) only


def test_intensity_zero_mean_not_warm():
    k = TradeIntensity(window_s=3600.0, min_trades=2)
    k.observe(0.0, 1_000_000)
    k.observe(0.0, 1_001_000)
    assert k.k() is None
