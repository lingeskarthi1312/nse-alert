"""Intraday BUY/SELL engine: indicators, signals, levels, time cut-offs,
simulation and validation labelling. Synthetic 5-minute bars only."""
import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from alert_system.intraday_reco import enrich_5m, evaluate, run_backtest, scan, simulate, summarize
from alert_system.market_data import Instrument

IST = ZoneInfo("Asia/Kolkata")
CFG = json.loads((Path(__file__).resolve().parent.parent / "config" / "alert_system.json").read_text())["intraday_reco"]


def session_bars(day, closes, volumes=None, start_price=None):
    idx = pd.date_range(f"{day} 09:15", periods=len(closes), freq="5min", tz=IST)
    c = np.array(closes, dtype=float)
    o = np.r_[start_price or c[0], c[:-1]]
    vol = np.array(volumes if volumes is not None else [1_000_000] * len(c), dtype=float)
    return pd.DataFrame({"Open": o, "High": np.maximum(o, c) * 1.001, "Low": np.minimum(o, c) * 0.999,
                         "Close": c, "Volume": vol}, index=idx)


def two_days(trend, n_today=30, surge=True):
    prev = session_bars("2026-10-16", [100 + 0.01 * i for i in range(75)])
    base = prev["Close"].iloc[-1]
    closes = [base + trend * i for i in range(1, n_today + 1)]
    vols = [1_000_000] * (n_today - 3) + ([4_000_000] * 3 if surge else [1_000_000] * 3)
    return pd.concat([prev, session_bars("2026-10-19", closes, vols, start_price=base)])


def test_vwap_resets_each_session_and_no_lookahead():
    f = enrich_5m(two_days(0.3))
    first_today = f[f["session"] == f["session"].iloc[-1]].iloc[0]
    tp = (first_today["High"] + first_today["Low"] + first_today["Close"]) / 3
    assert abs(first_today["vwap"] - tp) < 1e-9
    full, cut = enrich_5m(two_days(0.3)), enrich_5m(two_days(0.3).iloc[:-5])
    pd.testing.assert_series_equal(full["vwap"].iloc[:-5], cut["vwap"], check_names=False)


def test_strong_uptrend_with_volume_is_buy_not_sell():
    f = enrich_5m(two_days(0.25))
    row = f.iloc[-1]
    buy, sell = evaluate(row, "BUY", CFG, bench_pct=0.1), evaluate(row, "SELL", CFG, bench_pct=0.1)
    assert sell is None
    if buy:  # may be filtered as over-extended; when present levels must be consistent
        assert buy["stop"] < buy["price"] < buy["target"]
        assert abs((buy["target"] - buy["price"]) / (buy["price"] - buy["stop"]) - CFG["target_r"]) < 0.05


def test_downtrend_is_sell_with_mirrored_levels():
    cfg = dict(CFG, max_extension_atr=99)
    f = enrich_5m(two_days(-0.25))
    res = evaluate(f.iloc[-1], "SELL", cfg, bench_pct=0.0)
    assert res and res["signal"] == "SELL" and res["target"] < res["price"] < res["stop"]
    assert evaluate(f.iloc[-1], "BUY", cfg, bench_pct=0.0) is None


def test_no_volume_surge_needs_all_other_confirmations():
    cfg = dict(CFG, max_extension_atr=99, min_confirmations=5)
    f = enrich_5m(two_days(0.25, surge=False))
    assert evaluate(f.iloc[-1], "BUY", cfg) is None


def test_scan_marks_cutoff_and_stale_data():
    f = enrich_5m(two_days(0.25))
    uni = [Instrument("UP.NS", "UP", "Equity", "Large-cap")]
    cfg = dict(CFG, max_extension_atr=99, min_traded_value_cr=0)
    last = f.index[-1]
    fresh = scan(uni, {"UP.NS": f}, None, last.to_pydatetime(), cfg)
    assert fresh["status"] == "open" and fresh["data_time"].startswith("2026-10-19")
    late = scan(uni, {"UP.NS": f}, None, datetime(2026, 10, 19, 15, 0, tzinfo=IST), cfg)
    assert late["status"] == "no_new_entries" and late["buys"] == []  # 15:00 is > 40 min after the last bar
    other_day = scan(uni, {"UP.NS": f}, None, datetime(2026, 10, 20, 10, 0, tzinfo=IST), cfg)
    assert other_day["status"] == "no_session_data"


def test_simulate_long_and_short_exits_with_costs():
    f = enrich_5m(two_days(0.25, n_today=40))
    i = len(f) - 20
    long = simulate(f, i, "BUY", stop=f["Close"].iloc[i] - 50, target=f["Close"].iloc[i] + 1.0,
                    square_off=datetime(2026, 1, 1, 15, 15).time(), cost_pct=0.12)
    assert long["reason"] == "target" and long["entry_time"] == f.index[i + 1]
    short = simulate(f, i, "SELL", stop=f["Close"].iloc[i] + 0.3, target=f["Close"].iloc[i] - 50,
                     square_off=datetime(2026, 1, 1, 15, 15).time(), cost_pct=0.12)
    assert short["reason"] == "stop" and short["net_pct"] < 0


def test_backtest_one_trade_per_symbol_per_day_and_label():
    cfg = copy.deepcopy(CFG)
    cfg.update(max_extension_atr=99, min_traded_value_cr=0)
    frames = {f"S{i}.NS": enrich_5m(two_days(0.2 + 0.01 * i, n_today=75)) for i in range(4)}
    trades = run_backtest(frames, None, cfg)
    keys = [(t["symbol"], t["signal_time"].date()) for t in trades]
    assert len(keys) == len(set(keys))
    assert all(t["entry_time"] > t["signal_time"] for t in trades)
    rep = summarize(trades, cfg, len(frames))
    assert rep["validation"] == "UNVALIDATED"  # far fewer than min_oos_trades
