"""Intraday BUY (long) / SELL (short) candidates on 5-minute bars.

Data: Yahoo Finance 5-minute bars (free; NSE quotes ~15 minutes delayed;
Yahoo keeps at most ~60 days of 5-minute history). Nothing is stored in
ClickHouse; the latest snapshot goes to state/intraday_latest.json.

Per stock, on the CURRENT session's completed bars:
  VWAP (session), EMA 9/21, RSI 14, ATR 14 (5-minute), opening range = first
  15 minutes, volume surge = last 3 bars vs the 20-bar average, day change
  vs previous close, relative strength vs the NIFTY 50 intraday move.

BUY candidate needs >= min_confirmations of:
  price > VWAP, EMA9 > EMA21, price > opening-range high, RSI in [55, 75],
  volume surge >= volume_surge_min. SELL (short) is the exact mirror.
Levels: entry = last price, stop = entry -/+ stop_atr_mult x ATR(5m),
target = entry +/- target_r x risk. Positions are meant to be squared off by
the square-off time; no new signals after no_new_signals_after.

The weekly backtest (run_backtest) replays the SAME rules every 15 minutes on
up to 59 days of 5-minute history, enters at the next bar's open, exits on
stop / target / square-off with intraday costs, and labels the strategy
VALIDATED / FAILED / UNVALIDATED exactly like the swing model.
"""
import json
import logging
import math
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np

from .backtest import SEGMENTS, metrics
from .indicators import atr, ema, rsi
from .logging_setup import log_event
from .market_data import load_universe
from .timeutil import to_iso

log = logging.getLogger("alert_system.intraday_reco")
IST = ZoneInfo("Asia/Kolkata")
SESSION_START = dtime(9, 15)
CONFIRMATIONS = ("vwap", "ema", "range", "rsi", "volume")


def _hm(text):
    h, m = text.split(":")
    return dtime(int(h), int(m))


def download_5m(symbols, period="5d", batch_size=100, downloader=None):
    """{symbol: DataFrame[Open, High, Low, Close, Volume]} with IST timestamps."""
    if downloader is None:
        import yfinance as yf
        downloader = yf.download
    out = {}
    symbols = list(symbols)
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        try:
            data = downloader(batch, period=period, interval="5m", auto_adjust=False, progress=False,
                              threads=True, group_by="ticker", prepost=False, timeout=30)
        except Exception as exc:  # provider hiccup: skip this batch, report coverage instead
            log_event(log, logging.WARNING, "5m batch download failed", component="intraday_reco",
                      symbols=len(batch), error=f"{type(exc).__name__}: {exc}"[:200])
            continue
        if data is None or data.empty:
            continue
        multi = getattr(data.columns, "nlevels", 1) > 1
        present = set(data.columns.get_level_values(0)) if multi else set()
        for s in batch:
            if multi and s not in present:
                continue
            f = (data[s] if multi else data)[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
            f = f[f["Close"] > 0]
            if f.empty:
                continue
            idx = f.index.tz_convert(IST) if f.index.tz is not None else f.index.tz_localize(IST)
            f.index = idx
            out[s] = f
    return out


def enrich_5m(f):
    """Add intraday indicator columns (causal: each row uses only data up to it)."""
    f = f.copy()
    f["session"] = f.index.date
    tp = (f["High"] + f["Low"] + f["Close"]) / 3
    pv = (tp * f["Volume"]).groupby(f["session"]).cumsum()
    vol = f["Volume"].groupby(f["session"]).cumsum()
    f["vwap"] = pv / vol.replace(0, np.nan)
    f["ema9"], f["ema21"] = ema(f["Close"], 9), ema(f["Close"], 21)
    f["rsi"] = rsi(f["Close"], 14)
    f["atr"] = atr(f["High"], f["Low"], f["Close"], 14)
    f["vol_surge"] = f["Volume"].rolling(3).mean() / f["Volume"].rolling(20).mean().replace(0, np.nan)
    first3 = f.groupby("session").head(3)
    orh = first3.groupby("session")["High"].max()
    orl = first3.groupby("session")["Low"].min()
    f["or_high"], f["or_low"] = f["session"].map(orh), f["session"].map(orl)
    f["bar_no"] = f.groupby("session").cumcount()
    last_close = f.groupby("session")["Close"].last()
    prev_close = last_close.shift(1)
    f["prev_close"] = f["session"].map(prev_close)
    f["day_pct"] = (f["Close"] / f["prev_close"] - 1) * 100
    f["traded_value"] = (f["Close"] * f["Volume"]).groupby(f["session"]).cumsum()
    return f


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def evaluate(row, side, cfg, bench_pct=None):
    """Score/confirmations for one stock at one bar. Returns dict or None."""
    c, vwap, e9, e21 = _f(row["Close"]), _f(row["vwap"]), _f(row["ema9"]), _f(row["ema21"])
    r, a, vs = _f(row["rsi"]), _f(row["atr"]), _f(row["vol_surge"])
    orh, orl, day = _f(row["or_high"]), _f(row["or_low"]), _f(row["day_pct"])
    if None in (c, vwap, e9, e21, r, a, vs, orh, orl) or a <= 0 or row["bar_no"] < 3:
        return None
    long = side == "BUY"
    conf = {
        "vwap": c > vwap if long else c < vwap,
        "ema": e9 > e21 if long else e9 < e21,
        "range": c > orh if long else c < orl,
        "rsi": (55 <= r <= 75) if long else (25 <= r <= 45),
        "volume": vs >= cfg["volume_surge_min"],
    }
    n = sum(conf.values())
    if n < cfg["min_confirmations"]:
        return None
    sign = 1 if long else -1
    dist_vwap = sign * (c - vwap) / a                       # ATRs beyond VWAP (in trade direction)
    if dist_vwap > cfg["max_extension_atr"]:
        return None                                          # too stretched to chase
    rs = None if day is None or bench_pct is None else sign * (day - bench_pct)
    comp = {
        "trend": 100.0 * (conf["vwap"] + conf["ema"]) / 2,
        "breakout": 100.0 if conf["range"] else 0.0,
        "momentum": 100.0 if conf["rsi"] else 40.0,
        "volume": float(min(vs / 3.0, 1.0) * 100),
        "relative_strength": 50.0 if rs is None else float(np.clip(50 + rs * 15, 0, 100)),
    }
    w = cfg["weights"]
    score = sum(comp[k] * w[k] for k in comp) / sum(w.values())
    stop = c - sign * cfg["stop_atr_mult"] * a
    risk = abs(c - stop)
    target = c + sign * cfg["target_r"] * risk
    return {"signal": side, "score": round(score, 1), "components": {k: round(v) for k, v in comp.items()},
            "confirmations": conf, "price": round(c, 2), "stop": round(stop, 2), "target": round(target, 2),
            "risk_reward": round(cfg["target_r"], 1), "vwap": round(vwap, 2), "rsi": round(r, 1),
            "atr": round(a, 2), "day_pct": None if day is None else round(day, 2),
            "volume_surge": round(vs, 2), "n_conf": n}


def _liquid(row, cfg):
    tv = _f(row["traded_value"])
    return tv is not None and tv >= cfg["min_traded_value_cr"] * 1e7 * (int(row["bar_no"]) + 1) / 75


def scan(universe, frames, bench, now, cfg, validation="UNVALIDATED"):
    """Top BUY and SELL candidates at the latest completed bar of today's session."""
    local = now.astimezone(IST)
    today = local.date()
    cut = _hm(cfg["no_new_signals_after"])
    bench_pct = None
    if bench is not None:
        bt = bench[bench["session"] == today]
        if len(bt):
            bench_pct = _f(bt["day_pct"].iloc[-1])
    by = {i.symbol: i for i in universe}
    buys, sells, stale = [], [], 0
    newest = None
    for sym, f in frames.items():
        t = f[f["session"] == today]
        if t.empty:
            stale += 1
            continue
        row = t.iloc[-1]
        ts = t.index[-1]
        newest = ts if newest is None or ts > newest else newest
        if (local - ts).total_seconds() > cfg["max_quote_age_minutes"] * 60 or not _liquid(row, cfg):
            continue
        inst = by.get(sym)
        for side, bucket in (("BUY", buys), ("SELL", sells)):
            res = evaluate(row, side, cfg, bench_pct)
            if res:
                res.update(symbol=sym, name=inst.name if inst else sym, category=inst.category if inst else "",
                           bar_time=ts.strftime("%H:%M"), price_date=str(today), horizon="Intraday (square off by "
                           f"{cfg['square_off']})", confidence="LOW" if validation != "VALIDATED" else
                           ("HIGH" if res["n_conf"] == 5 else "MEDIUM"))
                bucket.append(res)
    top = cfg["top_n"]
    status = "open"
    if local.time() > cut:
        status = "no_new_entries"  # signals shown for reference only after the cut-off
    if newest is None:
        status = "no_session_data"
    return {
        "generated_at": to_iso(now), "session_date": str(today), "data_time": newest.isoformat() if newest else None,
        "status": status, "nifty_day_pct": bench_pct, "validation": validation,
        "buys": sorted(buys, key=lambda x: -x["score"])[:top],
        "sells": sorted(sells, key=lambda x: -x["score"])[:top],
        "counts": {"buy": len(buys), "sell": len(sells), "scanned": len(frames), "no_today_data": stale},
        "rules": {k: cfg[k] for k in ("min_confirmations", "volume_surge_min", "stop_atr_mult", "target_r",
                                      "no_new_signals_after", "square_off")},
    }


def validation_status(settings):
    try:
        return json.loads((settings.state_dir / "intraday_backtest.json").read_text()).get("validation", "UNVALIDATED")
    except (OSError, ValueError):
        return "UNVALIDATED"


def run_intraday_reco(settings, client, now):
    cfg = settings.intraday_reco
    universe = [i for i in load_universe(client, ()) if i.asset_class == "Equity"]
    raw = download_5m([i.symbol for i in universe] + [cfg["benchmark"]], period="5d")
    if len(raw) < 0.5 * len(universe):
        raise RuntimeError(f"5-minute data for only {len(raw)}/{len(universe)} symbols")
    bench = enrich_5m(raw.pop(cfg["benchmark"])) if cfg["benchmark"] in raw else None
    frames = {s: enrich_5m(f) for s, f in raw.items()}
    snap = scan(universe, frames, bench, now, cfg, validation_status(settings))
    tmp = settings.state_dir / "intraday_latest.json.tmp"
    tmp.write_text(json.dumps(snap, default=str, indent=1))
    tmp.replace(settings.state_dir / "intraday_latest.json")
    log_event(log, logging.INFO, "intraday scan done", component="intraday_reco", status=snap["status"],
              data_time=snap["data_time"], **snap["counts"])
    return snap


# ------------------------------------------------------------------ backtest ---
def simulate(f, i, side, stop, target, square_off, cost_pct):
    """Enter at bar i+1 open; exit on stop/target (stop first if both) or square-off."""
    if i + 1 >= len(f):
        return None
    sess = f["session"].iloc[i]
    entry_bar = f.iloc[i + 1]
    if entry_bar["session"] != sess:
        return None
    entry = float(entry_bar["Open"])
    sign = 1 if side == "BUY" else -1
    risk = sign * (entry - stop)
    if risk <= 0:
        return None
    exit_price, reason, j = None, None, i + 1
    while j < len(f) and f["session"].iloc[j] == sess:
        bar = f.iloc[j]
        hi, lo = float(bar["High"]), float(bar["Low"])
        if (sign > 0 and lo <= stop) or (sign < 0 and hi >= stop):
            exit_price, reason = stop, "stop"
        elif (sign > 0 and hi >= target) or (sign < 0 and lo <= target):
            exit_price, reason = target, "target"
        elif f.index[j].time() >= square_off:
            exit_price, reason = float(bar["Close"]), "square_off"
        if exit_price is not None:
            break
        j += 1
    if exit_price is None:
        exit_price, reason, j = float(f["Close"].iloc[j - 1]), "session_end", j - 1
    gross = sign * (exit_price / entry - 1)
    net = gross - cost_pct / 100
    return {"entry_time": f.index[i + 1], "exit_time": f.index[j], "exit_date": f.index[j], "entry": entry,
            "exit": exit_price, "reason": reason, "net_pct": net * 100,
            "r_multiple": (sign * (exit_price - entry) - entry * cost_pct / 100) / risk}


def run_backtest(frames, bench, cfg):
    """Every 15 minutes between start and cut-off: take the top N BUY and SELL
    candidates (one trade per symbol per day), simulate, collect trades."""
    bt = cfg["backtest"]
    cut, sq = _hm(cfg["no_new_signals_after"]), _hm(cfg["square_off"])
    first = _hm(bt["first_check"])
    checks = {}
    for sym, f in frames.items():
        mask = (f.index.minute % 15 == 0) & (f.index.time >= first) & (f.index.time <= cut)
        for i in np.flatnonzero(mask):
            checks.setdefault(f.index[i], []).append((sym, i))
    bench_day = bench["day_pct"] if bench is not None else None
    trades, used = [], set()
    for ts in sorted(checks):
        bpct = _f(bench_day.get(ts)) if bench_day is not None else None
        cands = []
        for sym, i in checks[ts]:
            f = frames[sym]
            row = f.iloc[i]
            if not _liquid(row, cfg):
                continue
            for side in ("BUY", "SELL"):
                if (sym, ts.date()) in used:
                    continue
                res = evaluate(row, side, cfg, bpct)
                if res:
                    cands.append((res["score"], side, sym, i, res))
        for side in ("BUY", "SELL"):
            picked = sorted((c for c in cands if c[1] == side), key=lambda c: -c[0])[:bt["per_check"]]
            for _, _, sym, i, res in picked:
                if (sym, ts.date()) in used:
                    continue
                t = simulate(frames[sym], i, side, res["stop"], res["target"], sq,
                             cfg["costs"]["round_trip_cost_pct"])
                if t:
                    t.update(symbol=sym, side=side, signal_time=ts)
                    trades.append(t)
                    used.add((sym, ts.date()))
    return trades


def summarize(trades, cfg, n_symbols):
    trades = sorted(trades, key=lambda t: t["signal_time"])
    n = len(trades)
    per = cfg["backtest"]["per_check"] * 2
    rep = {"generated_at": to_iso(datetime.now(IST)), "symbols": n_symbols,
           "period": [str(trades[0]["signal_time"].date()), str(trades[-1]["signal_time"].date())] if n else None,
           "overall": metrics(trades, per), "segments": {}, "by_side": {},
           "assumptions": {"entry": "next 5-minute bar open", "exit": "stop / target / square-off "
                           + cfg["square_off"], "round_trip_cost_pct": cfg["costs"]["round_trip_cost_pct"],
                           "checks": "every 15 minutes " + cfg["backtest"]["first_check"] + "-"
                           + cfg["no_new_signals_after"]},
           "biases": ["survivorship: current index constituents only",
                      "Yahoo 5-minute data is ~15 minutes delayed live; the backtest assumes undelayed fills",
                      "flat cost estimate; real slippage on fast moves can be larger"]}
    for name, lo, hi in SEGMENTS:
        rep["segments"][name] = metrics(trades[int(lo * n):int(hi * n)], per)
    for side in ("BUY", "SELL"):
        rep["by_side"][side] = metrics([t for t in trades if t["side"] == side], per)
    crit = cfg["backtest"]["validation"]
    oos = rep["segments"]["out_of_sample"]
    checks = {
        f"out-of-sample trades >= {crit['min_oos_trades']}": oos.get("trades", 0) >= crit["min_oos_trades"],
        "out-of-sample expectancy after costs > 0": (oos.get("expectancy_pct") or -1) > 0,
        f"out-of-sample profit factor >= {crit['min_profit_factor']}":
            (oos.get("profit_factor") or 0) >= crit["min_profit_factor"],
    }
    rep["validation_checks"] = checks
    rep["validation"] = ("UNVALIDATED" if oos.get("trades", 0) < crit["min_oos_trades"]
                         else "VALIDATED" if all(checks.values()) else "FAILED")
    return rep


def run_backtest_job(settings, client, now):
    cfg = settings.intraday_reco
    universe = [i for i in load_universe(client, ()) if i.asset_class == "Equity"]
    raw = download_5m([i.symbol for i in universe] + [cfg["benchmark"]], period=cfg["backtest"]["period"])
    bench = enrich_5m(raw.pop(cfg["benchmark"])) if cfg["benchmark"] in raw else None
    frames = {s: enrich_5m(f) for s, f in raw.items()}
    trades = run_backtest(frames, bench, cfg)
    rep = summarize(trades, cfg, len(frames))
    (settings.state_dir / "intraday_backtest.json").write_text(json.dumps(rep, default=str, indent=1))
    log_event(log, logging.INFO, "intraday backtest finished", component="intraday_reco",
              validation=rep["validation"], trades=rep["overall"].get("trades"))
    return rep

