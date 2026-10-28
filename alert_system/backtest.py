"""Walk-forward backtest of the recommendation engine.

Method (no look-ahead):
  * every `rebalance_every` sessions, run the SAME analyze() on bars up to and
    including that day's close (indicators are causal);
  * buy the top `top_n` cross-asset BUY CANDIDATEs at the NEXT session's open;
  * exit on the stop (or the open if it gaps below), on target1 (or the open
    if it gaps above), else at the close after `max_hold_days`. If stop and
    target are both inside one bar, the stop is assumed (conservative);
  * subtract `round_trip_cost_pct` (brokerage, STT, exchange/SEBI fees, stamp
    duty, GST, DP charge, slippage) from every trade;
  * skip a signal while the same symbol already has an open trade.

Trades are split chronologically by signal date into in-sample (60%),
validation (20%) and out-of-sample (20%). Weights are NOT tuned on this data;
the split shows whether results hold up over time. The strategy is labelled
VALIDATED only if the out-of-sample segment meets every criterion in config.

Known bias: the universe is TODAY's index constituents, so stocks that were
dropped/delisted are missing (survivorship bias - results are optimistic).
"""
import math
from datetime import datetime, timezone

from .recommend import BUY, analyze

SEGMENTS = (("in_sample", 0.0, 0.6), ("validation", 0.6, 0.8), ("out_of_sample", 0.8, 1.0))


def simulate_trade(frame, signal_date, stop, target, max_hold, cost_pct):
    """Trade from the bar after signal_date. Returns dict or None."""
    after = frame.loc[frame.index > signal_date]
    if len(after) < 2:
        return None
    entry_bar = after.iloc[0]
    entry = float(entry_bar["Open"])
    if not entry > 0 or stop is None or target is None:
        return None
    risk = entry - stop
    if risk <= 0:  # gapped below the stop at the open: setup void, no trade
        return None
    exit_price, exit_date, reason = None, None, None
    window = after.iloc[:max_hold]
    for i, (ts, bar) in enumerate(window.iterrows()):
        o, h, l, c = (float(bar[k]) for k in ("Open", "High", "Low", "Close"))
        if i > 0 and o <= stop:
            exit_price, reason = o, "stop_gap"
        elif l <= stop:
            exit_price, reason = stop, "stop"
        elif i > 0 and o >= target:
            exit_price, reason = o, "target_gap"
        elif h >= target:
            exit_price, reason = target, "target"
        if exit_price is not None:
            exit_date = ts
            break
    if exit_price is None:
        if len(window) < max_hold:
            return None  # not enough future data yet: trade still open, excluded
        exit_price, exit_date, reason = float(window.iloc[-1]["Close"]), window.index[-1], "time"
    gross = exit_price / entry - 1
    net = gross - cost_pct / 100
    return {"entry_date": after.index[0], "exit_date": exit_date, "entry": entry, "exit": exit_price,
            "stop": stop, "target": target, "reason": reason, "gross_pct": gross * 100, "net_pct": net * 100,
            "r_multiple": (exit_price - entry - entry * cost_pct / 100) / risk}


def metrics(trades, top_n):
    n = len(trades)
    if not n:
        return {"trades": 0}
    rets = [t["net_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda t: t["exit_date"]):
        equity += t["net_pct"] / top_n  # each trade sized at 1/top_n of capital
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    rs = sorted(t["r_multiple"] for t in trades)
    return {
        "trades": n,
        "win_rate_pct": round(100 * len(wins) / n, 1),
        "avg_win_pct": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss_pct": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy_pct": round(sum(rets) / n, 3),
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "max_drawdown_pct": round(max_dd, 2),
        "total_return_pct": round(equity, 2),
        "r_multiple_p25_p50_p75": [round(rs[int(q * (n - 1))], 2) for q in (0.25, 0.5, 0.75)],
    }


def run_backtest(universe, frames, benchmark, cfg, progress=None):
    bt = cfg["backtest"]
    top_n, cost = cfg["top_n"], cfg["costs"]["round_trip_cost_pct"]
    dates = benchmark.index[cfg["filters"]["min_bars"]::bt["rebalance_every"]]
    trades, open_until = [], {}
    by_symbol = {i.symbol: i for i in universe}
    for k, d in enumerate(dates):
        sliced = {s: f.loc[:d] for s, f in frames.items()}
        bench = benchmark.loc[:d]
        results, uptrend = analyze(universe, sliced, bench, d.date(), cfg)
        picks = sorted((a for a in results if a.signal == BUY), key=lambda a: (-a.score, a.symbol))
        taken = 0
        for a in picks:
            if taken >= top_n:
                break
            if open_until.get(a.symbol) is not None and open_until[a.symbol] >= d:
                continue
            frame = frames[a.symbol]
            # the backtest needs levels for every pick; analyze() keeps stop/target for BUY signals
            t = simulate_trade(frame, d, a.stop, a.target1, bt["max_hold_days"], cost)
            if t is None:
                continue
            inst = by_symbol[a.symbol]
            t.update(symbol=a.symbol, signal_date=d, category=inst.category, asset_class=inst.asset_class,
                     regime="bull" if uptrend else "bear", score=a.score)
            trades.append(t)
            open_until[a.symbol] = t["exit_date"]
            taken += 1
        if progress:
            progress(k + 1, len(dates))
    return trades




def summarize(trades, cfg, universe_size, start, end):
    top_n = cfg["top_n"]
    trades = sorted(trades, key=lambda t: t["signal_date"])
    n = len(trades)
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period": [str(start), str(end)], "universe_size": universe_size,
        "assumptions": {"entry": "next session open", "stop": f"{cfg['levels']['stop_atr_mult']}x ATR(14)",
                        "target": "target1 (nearest resistance or R-multiple)",
                        "max_hold_days": cfg["backtest"]["max_hold_days"],
                        "round_trip_cost_pct": cfg["costs"]["round_trip_cost_pct"],
                        "rebalance_every_sessions": cfg["backtest"]["rebalance_every"], "positions": top_n},
        "biases": ["survivorship: universe is current index constituents",
                   "costs are a flat round-trip estimate, not broker-specific",
                   "fundamentals/news not part of the model"],
        "overall": metrics(trades, top_n), "segments": {}, "by_category": {}, "by_regime": {},
    }
    for name, lo, hi in SEGMENTS:
        report["segments"][name] = metrics(trades[int(lo * n):int(hi * n)], top_n)
    for key, field_name in (("by_category", "category"), ("by_regime", "regime")):
        groups = {}
        for t in trades:
            groups.setdefault(t[field_name], []).append(t)
        report[key] = {g: metrics(ts, top_n) for g, ts in sorted(groups.items())}
    crit = cfg["backtest"]["validation"]
    oos = report["segments"]["out_of_sample"]
    checks = {
        f"out-of-sample trades >= {crit['min_oos_trades']}": oos.get("trades", 0) >= crit["min_oos_trades"],
        "out-of-sample expectancy after costs > 0": (oos.get("expectancy_pct") or -1) > 0,
        f"out-of-sample profit factor >= {crit['min_profit_factor']}":
            (oos.get("profit_factor") or 0) >= crit["min_profit_factor"],
        f"out-of-sample max drawdown <= {crit['max_drawdown_pct']}%":
            oos.get("trades", 0) > 0 and oos.get("max_drawdown_pct", math.inf) <= crit["max_drawdown_pct"],
    }
    report["validation_checks"] = checks
    if oos.get("trades", 0) < crit["min_oos_trades"]:
        report["validation"] = "UNVALIDATED"  # not enough evidence either way
    else:
        report["validation"] = "VALIDATED" if all(checks.values()) else "FAILED"
    return report
