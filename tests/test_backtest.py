"""Backtest mechanics: next-open entry (no look-ahead), stop/target/time exits,
costs, metrics and the validation label. Synthetic data only."""
import copy

import pandas as pd

from alert_system.backtest import metrics, run_backtest, simulate_trade, summarize
from alert_system.indicators import enrich
from test_recommend import CFG, bench, inst, series


def bars(rows, start="2026-01-01"):
    idx = pd.bdate_range(start=start, periods=len(rows))
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close"], index=idx)


def test_entry_is_next_open_and_target_exit_with_costs():
    f = bars([(100, 101, 99, 100), (102, 103, 101, 102), (103, 112, 102, 111)])
    t = simulate_trade(f, f.index[0], stop=95, target=110, max_hold=5, cost_pct=0.5)
    assert t["entry_date"] == f.index[1] and t["entry"] == 102  # signal-day close is NOT used
    assert t["exit"] == 110 and t["reason"] == "target"
    assert round(t["net_pct"], 4) == round((110 / 102 - 1) * 100 - 0.5, 4)


def test_stop_assumed_first_when_both_hit_same_bar():
    f = bars([(100, 100, 100, 100), (100, 101, 99, 100), (100, 120, 80, 100)])
    t = simulate_trade(f, f.index[0], stop=90, target=110, max_hold=5, cost_pct=0)
    assert t["reason"] == "stop" and t["exit"] == 90


def test_gap_below_stop_exits_at_open():
    f = bars([(100, 100, 100, 100), (100, 101, 99, 100), (85, 86, 80, 82)])
    t = simulate_trade(f, f.index[0], stop=90, target=110, max_hold=5, cost_pct=0)
    assert t["reason"] == "stop_gap" and t["exit"] == 85


def test_time_exit_and_open_trades_excluded():
    rows = [(100, 101, 99, 100)] * 8
    f = bars(rows)
    t = simulate_trade(f, f.index[0], stop=90, target=120, max_hold=5, cost_pct=0)
    assert t["reason"] == "time" and t["exit_date"] == f.index[5]
    assert simulate_trade(f, f.index[4], stop=90, target=120, max_hold=5, cost_pct=0) is None  # still open


def test_entry_gap_below_stop_skips_trade():
    f = bars([(100, 100, 100, 100), (80, 81, 79, 80), (80, 81, 79, 80)])
    assert simulate_trade(f, f.index[0], stop=90, target=110, max_hold=5, cost_pct=0) is None


def test_metrics():
    ts = pd.Timestamp
    trades = [{"net_pct": 10, "r_multiple": 2, "exit_date": ts("2026-01-02")},
              {"net_pct": -5, "r_multiple": -1, "exit_date": ts("2026-01-03")},
              {"net_pct": -5, "r_multiple": -1, "exit_date": ts("2026-01-04")},
              {"net_pct": 20, "r_multiple": 3, "exit_date": ts("2026-01-05")}]
    m = metrics(trades, top_n=1)
    assert m["win_rate_pct"] == 50.0 and m["expectancy_pct"] == 5.0
    assert m["profit_factor"] == 3.0 and m["max_drawdown_pct"] == 10.0
    assert metrics([], 5) == {"trades": 0}


def _small_universe():
    raw = {f"S{i}.NS": series(420, drift=0.0015 if i % 2 else -0.0005, vol_trend=0.5, seed=80 + i)
           for i in range(8)}
    b = bench(420)
    frames = {s: enrich(f, b["Close"]) for s, f in raw.items()}
    return [inst(s) for s in raw], frames, b


def test_backtest_runs_and_labels_unvalidated_without_enough_trades():
    cfg = copy.deepcopy(CFG)
    cfg["backtest"]["rebalance_every"] = 10
    universe, frames, b = _small_universe()
    trades = run_backtest(universe, frames, b, cfg)
    for t in trades:
        assert t["entry_date"] > t["signal_date"]  # no same-bar look-ahead
    rep = summarize(trades, cfg, len(universe), b.index[0].date(), b.index[-1].date())
    assert set(rep["segments"]) == {"in_sample", "validation", "out_of_sample"}
    assert rep["validation"] in ("UNVALIDATED", "FAILED", "VALIDATED")
    if rep["segments"]["out_of_sample"].get("trades", 0) < cfg["backtest"]["validation"]["min_oos_trades"]:
        assert rep["validation"] == "UNVALIDATED"
    assert any("survivorship" in x for x in rep["biases"])


def test_no_overlapping_trades_per_symbol():
    cfg = copy.deepcopy(CFG)
    cfg["backtest"]["rebalance_every"] = 3
    universe, frames, b = _small_universe()
    trades = sorted(run_backtest(universe, frames, b, cfg), key=lambda t: t["entry_date"])
    last_exit = {}
    for t in trades:
        if t["symbol"] in last_exit:
            assert t["signal_date"] > last_exit[t["symbol"]]
        last_exit[t["symbol"]] = t["exit_date"]
