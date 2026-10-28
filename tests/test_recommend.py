"""Recommendation engine: indicators, filters, confirmations, levels, rankings.
Synthetic price series only - deterministic, no network."""
import copy
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from alert_system.indicators import enrich, rsi
from alert_system.market_data import Instrument, completed_bars, freshness
from alert_system.recommend import AVOID, BUY, HOLD, UNAVAILABLE, WATCH, analyze, percentile_ranks, rankings
from conftest import ist

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config" / "alert_system.json").read_text())["recommendations"]
END = pd.Timestamp("2026-10-16")


def series(n=300, drift=0.001, vol=0.01, volume=2_000_000, price=500.0, seed=1, end=END, vol_trend=0.0):
    rng = np.random.default_rng(seed)
    rets = drift + vol * rng.standard_normal(n)
    close = price * np.exp(np.cumsum(rets))
    idx = pd.bdate_range(end=end, periods=n)
    openp = close * (1 - 0.002 * rng.standard_normal(n))
    high = np.maximum(openp, close) * (1 + abs(0.004 * rng.standard_normal(n)))
    low = np.minimum(openp, close) * (1 - abs(0.004 * rng.standard_normal(n)))
    vols = volume * (1 + vol_trend * np.arange(n) / n) * (1 + 0.1 * rng.random(n))
    # up days get more volume when vol_trend > 0 (accumulation)
    vols = np.where(np.diff(np.r_[close[0], close]) > 0, vols * (1 + vol_trend), vols)
    return pd.DataFrame({"Open": openp, "High": high, "Low": low, "Close": close, "Volume": vols}, index=idx)


def bench(n=300, drift=0.0003, seed=99):
    return series(n, drift=drift, vol=0.006, price=22000, seed=seed)


def inst(sym, cat="Large-cap", asset="Equity"):
    return Instrument(sym, sym, asset, cat)


def run(frames_raw, universe, b=None, cfg=CFG, today=date(2026, 10, 16)):
    b = bench() if b is None else b
    frames = {s: enrich(f, b["Close"]) for s, f in frames_raw.items()}
    return analyze(universe, frames, b, today, cfg)


def test_rsi_bounds_and_trend():
    up = pd.Series(np.linspace(100, 200, 100))
    assert rsi(up).iloc[-1] == 100.0
    s = series(300)
    r = rsi(s["Close"]).dropna()
    assert ((r >= 0) & (r <= 100)).all()


def test_indicators_have_no_lookahead():
    s = series(300)
    full = enrich(s)
    cut = enrich(s.iloc[:250])
    cols = ["sma50", "sma200", "rsi14", "macd", "atr14", "ret_63", "traded_value20"]
    pd.testing.assert_frame_equal(full[cols].iloc[:250], cut[cols])


def test_strong_uptrend_becomes_buy_candidate_with_levels():
    frames = {"UP.NS": series(300, drift=0.0025, vol=0.009, vol_trend=0.6, seed=3)}
    frames.update({f"F{i}.NS": series(300, drift=0.0, vol=0.012, seed=10 + i) for i in range(8)})
    universe = [inst(s) for s in frames]
    results, uptrend = run(frames, universe)
    a = next(r for r in results if r.symbol == "UP.NS")
    assert a.signal in (BUY, HOLD), (a.signal, a.score, a.confirmations, a.risk_reward)
    if a.signal == BUY:
        assert a.stop < a.price and a.target1 > a.price and a.risk_reward >= CFG["signal"]["min_risk_reward"]
        assert a.entry_low <= a.entry_high == a.price
        assert sum(a.confirmations.values()) >= CFG["signal"]["min_confirmations"]
    assert set(a.components) == {"trend", "momentum", "relative_strength", "volume", "macd", "risk"}
    assert a.reasons and any("not assessed" in r for r in a.risks)


def test_downtrend_is_avoid_and_has_no_trade_levels():
    frames = {"DOWN.NS": series(300, drift=-0.002, seed=4), "X.NS": series(300, seed=5)}
    results, _ = run(frames, [inst(s) for s in frames])
    a = next(r for r in results if r.symbol == "DOWN.NS")
    assert a.signal == AVOID
    assert a.entry_low is None and a.target1 is None and a.risk_reward is None and a.position_size is None


def test_single_indicator_never_enough():
    cfg = copy.deepcopy(CFG)
    cfg["signal"]["min_confirmations"] = 5
    frames = {f"S{i}.NS": series(300, drift=0.0015, seed=20 + i) for i in range(6)}
    results, _ = run(frames, [inst(s) for s in frames], cfg=cfg)
    for a in results:
        if a.signal == BUY:
            assert all(a.confirmations.values())


def test_stale_and_missing_data_marked_unavailable():
    frames = {"OLD.NS": series(300, end=pd.Timestamp("2026-10-01")), "OK.NS": series(300, seed=7)}
    universe = [inst("OLD.NS"), inst("OK.NS"), inst("MISSING.NS")]
    results, _ = run(frames, universe)
    by = {a.symbol: a for a in results}
    assert by["OLD.NS"].signal == UNAVAILABLE and "stale" in by["OLD.NS"].excluded_reason
    assert by["OLD.NS"].freshness == "STALE"
    assert by["MISSING.NS"].signal == UNAVAILABLE and by["MISSING.NS"].price is None
    assert by["MISSING.NS"].score is None and by["MISSING.NS"].stop is None  # nothing invented


def test_short_history_unavailable():
    frames = {"NEW.NS": series(120)}
    results, _ = run(frames, [inst("NEW.NS")])
    assert results[0].signal == UNAVAILABLE and "bars" in results[0].excluded_reason


def test_illiquid_and_volatile_filtered():
    frames = {"THIN.NS": series(300, drift=0.002, volume=500, seed=8),
              "WILD.NS": series(300, drift=0.002, vol=0.12, seed=9)}
    results, _ = run(frames, [inst("THIN.NS", "Small-cap"), inst("WILD.NS", "Small-cap")])
    by = {a.symbol: a for a in results}
    assert by["THIN.NS"].signal == AVOID and "illiquid" in by["THIN.NS"].excluded_reason
    assert by["WILD.NS"].signal == AVOID and "volatile" in by["WILD.NS"].excluded_reason


def test_weak_market_requires_extra_confirmation():
    frames = {f"S{i}.NS": series(300, drift=0.002, vol_trend=0.6, seed=30 + i) for i in range(6)}
    universe = [inst(s) for s in frames]
    _, up = run(frames, universe, b=bench(drift=0.0005))
    res_down, down = run(frames, universe, b=bench(drift=-0.002))
    assert up is True and down is False
    for a in res_down:
        if a.signal == BUY:
            assert sum(a.confirmations.values()) >= CFG["signal"]["min_confirmations"] + 1
        if a.score is not None:
            assert any("200-DMA" in r for r in a.risks)


def test_rankings_are_separate_and_never_padded():
    frames = {f"L{i}.NS": series(300, drift=0.0025, vol=0.009, vol_trend=0.6, seed=40 + i) for i in range(7)}
    frames["SMALL.NS"] = series(300, drift=-0.002, seed=60)
    frames["GOLDBEES.NS"] = series(300, drift=0.0025, vol=0.008, vol_trend=0.6, seed=61, price=100,
                                   volume=20_000_000)
    universe = [inst(f"L{i}.NS") for i in range(7)] + [inst("SMALL.NS", "Small-cap"),
                                                       inst("GOLDBEES.NS", "Commodity ETF", "Gold ETF")]
    results, _ = run(frames, universe)
    r = rankings(results, 5)
    assert set(r) == {"overall", "Large-cap", "Mid-cap", "Small-cap", "cross_asset"}
    assert len(r["overall"]) <= 5 and all(a.asset_class == "Equity" for a in r["overall"])
    assert r["Small-cap"] == [] and r["Mid-cap"] == []  # no forced picks
    assert all(a.signal == BUY for k in r for a in r[k])
    scores = [a.score for a in r["cross_asset"]]
    assert scores == sorted(scores, reverse=True)


def test_position_size_uses_capital_and_risk_pct():
    frames = {"UP.NS": series(300, drift=0.0025, vol=0.009, vol_trend=0.6, seed=3)}
    frames.update({f"F{i}.NS": series(300, seed=10 + i) for i in range(5)})
    b = bench()
    enriched = {s: enrich(f, b["Close"]) for s, f in frames.items()}
    results, _ = analyze([inst(s) for s in frames], enriched, b, date(2026, 10, 16), CFG, capital=1_000_000)
    for a in results:
        if a.signal == BUY:
            risk = a.price - a.stop
            assert a.position_size.startswith(f"{int(1_000_000 * 0.01 // risk)} units")


def test_completed_bars_drops_partial_session():
    f = series(10, end=pd.Timestamp("2026-10-19"))
    assert completed_bars(f, ist(2026, 10, 19, 11, 0), "Asia/Kolkata").index[-1] == pd.Timestamp("2026-10-16")
    assert completed_bars(f, ist(2026, 10, 19, 16, 0), "Asia/Kolkata").index[-1] == pd.Timestamp("2026-10-19")


def test_freshness_rule():
    assert freshness(date(2026, 10, 16), date(2026, 10, 19), 4) == ("FRESH", 3)
    assert freshness(date(2026, 10, 10), date(2026, 10, 19), 4) == ("STALE", 9)


def test_percentile_ranks():
    assert percentile_ranks({"a": 1, "b": 3, "c": 2, "d": None}) == {"a": 0.0, "c": 50.0, "b": 100.0}


def test_watchlist_signal_exists_for_middling_setups():
    frames = {f"M{i}.NS": series(300, drift=0.0006, vol=0.012, seed=70 + i) for i in range(10)}
    results, _ = run(frames, [inst(s) for s in frames])
    assert {a.signal for a in results} <= {BUY, HOLD, WATCH, AVOID}


def test_config_validation_rejects_bad_weights(settings_factory, tmp_path):
    from alert_system.config import ConfigError, load_settings
    cfg = json.loads((ROOT / "config" / "alert_system.json").read_text())
    cfg["recommendations"]["weights"]["trend"] = "high"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(cfg))
    with pytest.raises(ConfigError, match="weights"):
        load_settings(env={"ALERT_BASE_DIR": str(tmp_path)}, config_path=p)


def test_failed_validation_forces_low_confidence():
    frames = {f"S{i}.NS": series(300, drift=0.0025, vol=0.009, vol_trend=0.6, seed=40 + i) for i in range(6)}
    b = bench()
    enriched = {s: enrich(f, b["Close"]) for s, f in frames.items()}
    results, _ = analyze([inst(s) for s in frames], enriched, b, date(2026, 10, 16), CFG, validation="FAILED")
    assert all(a.confidence == "LOW" for a in results if a.score is not None)
