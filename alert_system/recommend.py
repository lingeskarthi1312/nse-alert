"""Rule-based recommendation engine (transparent, configurable scoring).

Per instrument, on COMPLETED daily bars only:
  1. data checks   - enough history, recent last bar (else UNAVAILABLE/STALE);
  2. risk filters  - min price, liquidity (median 20-day traded value),
                     max volatility (ATR %); failures are excluded, with reason;
  3. components    - trend, momentum, relative strength, volume, MACD, risk,
                     each scored 0-100; weighted sum = score (weights in config);
  4. confirmations - five independent checks (trend, momentum, volume,
                     relative strength, MACD). A BUY CANDIDATE needs at least
                     `min_confirmations` (4 when the Nifty is below its 200-DMA),
                     a minimum score, risk/reward >= threshold and must not be
                     overbought/over-extended. One indicator alone never suffices;
  5. levels        - entry zone, ATR stop, structural/R-multiple targets, R:R and
                     risk-based position size, all derived from the bars - never
                     invented. Missing inputs leave a level empty.

Not assessed (no reliable source in this project): fundamentals, news,
corporate actions, sector strength. Every recommendation says so.
Horizon: daily bars -> swing trades (2-6 weeks). Intraday is not supported.
"""
import math
from dataclasses import asdict, dataclass, field

BUY, HOLD, WATCH, AVOID, UNAVAILABLE = "BUY CANDIDATE", "HOLD", "WATCHLIST", "AVOID", "UNAVAILABLE"
NOT_ASSESSED = "Fundamentals, news, corporate actions and sector strength not assessed (no reliable data source)."
COMPONENTS = ("trend", "momentum", "relative_strength", "volume", "macd", "risk")


@dataclass
class Analysis:
    symbol: str
    name: str
    asset_class: str
    category: str
    exchange: str
    currency: str
    signal: str = UNAVAILABLE
    price: float = None
    price_date: str = None
    freshness: str = None
    data_age_days: int = None
    score: float = None
    components: dict = field(default_factory=dict)
    confirmations: dict = field(default_factory=dict)
    entry_low: float = None
    entry_high: float = None
    stop: float = None
    target1: float = None
    target2: float = None
    target_basis: str = None
    risk_reward: float = None
    horizon: str = None
    position_size: str = None
    reasons: list = field(default_factory=list)
    risks: list = field(default_factory=list)
    excluded_reason: str = None
    confidence: str = None

    def to_dict(self):
        return asdict(self)


def _f(x):
    """float or None for NaN/missing (never invent a value)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _clip(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def percentile_ranks(values):
    """{key: 0-100 percentile} for non-None values."""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    if n <= 1:
        return {k: 50.0 for _, k in items}
    return {k: 100.0 * i / (n - 1) for i, (_, k) in enumerate(items)}


def check_data(inst, frame, today, cfg):
    """Return (row, error). row is the last completed bar with indicators."""
    from .market_data import freshness
    if frame is None or frame.empty:
        return None, "no price data from provider"
    if len(frame) < cfg["filters"]["min_bars"]:
        return None, f"only {len(frame)} daily bars (< {cfg['filters']['min_bars']} needed)"
    row = frame.iloc[-1]
    state, age = freshness(frame.index[-1].date(), today, cfg["max_data_age_days"])
    if state != "FRESH":
        return row, f"last completed bar {frame.index[-1].date()} is {age} days old (stale)"
    return row, None


def risk_filters(inst, row, cfg):
    f = cfg["filters"]
    price, tv, atrp = _f(row["Close"]), _f(row.get("traded_value20")), _f(row.get("atr_pct"))
    if price is None or price < f["min_price"]:
        return f"price below ₹{f['min_price']}"
    min_tv = f["min_traded_value_cr"].get(inst.category, f["min_traded_value_cr"].get("default", 5)) * 1e7
    if tv is None or tv < min_tv:
        return f"illiquid: median 20-day traded value ₹{(tv or 0) / 1e7:.1f} cr < ₹{min_tv / 1e7:.0f} cr"
    if atrp is None or atrp > f["max_atr_pct"]:
        return f"too volatile: ATR {atrp if atrp is not None else float('nan'):.1f}% > {f['max_atr_pct']}%"
    for col in ("sma50", "sma200", "rsi14", "macd", "atr14", "ret_63", "vol50"):
        if _f(row.get(col)) is None:
            return f"indicator {col} unavailable"
    return None


def components_for(row, ranks_ret63, ranks_rs63, symbol, cfg):
    c, s50, s200 = _f(row["Close"]), _f(row["sma50"]), _f(row["sma200"])
    slope, rsi_v = _f(row.get("sma50_slope")) or 0.0, _f(row["rsi14"])
    trend = 30 * (c > s50) + 30 * (s50 > s200) + 20 * (c > s200) + 20 * (slope > 0)
    rsi_zone = 100 if 50 <= rsi_v <= 70 else 50 if (40 <= rsi_v < 50 or 70 < rsi_v <= 75) else 0
    momentum = 0.6 * ranks_ret63.get(symbol, 50.0) + 0.4 * rsi_zone
    rel = ranks_rs63.get(symbol, 50.0)
    vol_ratio = (_f(row["vol20"]) or 0) / (_f(row["vol50"]) or 1)
    updown = _f(row.get("updown_vol_ratio")) or 0.0
    volume = 50 * min(vol_ratio, 1.5) / 1.5 + 50 * min(updown, 2.0) / 2.0
    m, ms = _f(row["macd"]), _f(row["macd_signal"])
    hist_now, hist_prev = _f(row.get("macd_hist")), _f(row.get("macd_hist_prev"))
    macd_s = 50 * (m is not None and ms is not None and m > ms) + 30 * (m is not None and m > 0) + \
        20 * (hist_now is not None and hist_prev is not None and hist_now > hist_prev)
    atrp, max_atr = _f(row["atr_pct"]), cfg["filters"]["max_atr_pct"]
    risk = _clip(100 * (max_atr - atrp) / (max_atr - 1.5)) if atrp > 1.5 else 100.0
    return {"trend": float(trend), "momentum": round(momentum, 1), "relative_strength": round(rel, 1),
            "volume": round(volume, 1), "macd": float(macd_s), "risk": round(risk, 1)}


def confirmations_for(row):
    c = _f(row["Close"])
    rsi_v = _f(row["rsi14"])
    rs = _f(row.get("rs_63"))
    return {
        "trend": c > _f(row["sma50"]) > _f(row["sma200"]),
        "momentum": (_f(row["ret_63"]) or 0) > 0 and 45 <= rsi_v <= 75,
        "volume": (_f(row["vol20"]) or 0) >= (_f(row["vol50"]) or math.inf) and (_f(row.get("updown_vol_ratio")) or 0) > 1.0,
        "relative_strength": rs is not None and rs > 0,
        "macd": _f(row["macd"]) > _f(row["macd_signal"]),
    }


def levels_for(row, cfg, capital=None):
    lv = cfg["levels"]
    c, a = _f(row["Close"]), _f(row["atr14"])
    if c is None or a is None or a <= 0:
        return {}
    s20 = _f(row.get("sma20"))
    entry_low = max(s20, c - lv["entry_atr_band"] * a) if s20 is not None and s20 < c else c - lv["entry_atr_band"] * a
    stop = c - lv["stop_atr_mult"] * a
    risk = c - stop
    projection = c + lv["target_r_multiple"] * risk
    structural = [x for x in (_f(row.get("resistance60")), _f(row.get("high252"))) if x is not None and x > c * 1.005]
    note = None
    nearest = min(structural) if structural else None
    if nearest is not None and nearest >= c + risk:
        t1, basis = nearest, "nearest resistance (60-day / 52-week high)"
        t2 = projection if projection > t1 else None
    else:
        t1, t2 = projection, None
        if nearest is not None:
            basis = f"{lv['target_r_multiple']:g}R projection (requires a breakout above ₹{nearest:,.2f})"
            note = f"overhead resistance at ₹{nearest:,.2f} is within 1R: needs a breakout to reach target"
        else:
            basis = f"{lv['target_r_multiple']:g}R projection (no overhead resistance; price at highs)"
    rr = (t1 - c) / risk if risk > 0 else None
    pct = cfg["position_sizing"]["risk_per_trade_pct"]
    if capital:
        qty = math.floor(capital * pct / 100 / risk)
        size = f"{qty} units (risk {pct}% of ₹{capital:,.0f} ÷ ₹{risk:.2f}/unit to stop)"
    else:
        size = f"units = (capital × {pct}%) ÷ ₹{risk:.2f} risk per unit (set PORTFOLIO_CAPITAL to compute)"
    return {"entry_low": round(entry_low, 2), "entry_high": round(c, 2), "stop": round(stop, 2),
            "target1": round(t1, 2), "target2": round(t2, 2) if t2 else None, "target_basis": basis,
            "risk_reward": round(rr, 2) if rr is not None else None, "position_size": size,
            "resistance_note": note}


def classify(a, row, cfg, market_uptrend):
    sig = cfg["signal"]
    conf = a.confirmations
    n_conf = sum(bool(v) for v in conf.values())
    needed = sig["min_confirmations"] + (0 if market_uptrend else 1)
    c, s20, atr_v, rsi_v = _f(row["Close"]), _f(row["sma20"]), _f(row["atr14"]), _f(row["rsi14"])
    extension = (c - s20) / atr_v if s20 is not None and atr_v else 0.0
    downtrend = c < _f(row["sma200"]) and _f(row["sma50"]) < _f(row["sma200"])
    if downtrend or a.score < sig["min_score_watch"]:
        return AVOID
    overheated = rsi_v > sig["overbought_rsi"] or extension > sig["max_extension_atr"]
    if conf.get("trend") and overheated:
        return HOLD
    if (n_conf >= needed and a.score >= sig["min_score_buy"] and a.risk_reward is not None
            and a.risk_reward >= sig["min_risk_reward"]):
        return BUY
    return WATCH


def explain(a, row, n_needed):
    r = []
    labels = {"trend": "price above rising 50-DMA and 50-DMA above 200-DMA",
              "momentum": f"3-month return {(_f(row['ret_63']) or 0) * 100:+.1f}% with RSI {(_f(row['rsi14'])):.0f}",
              "volume": "20-day volume above 50-day average with up-day volume dominance",
              "relative_strength": f"outperforming Nifty 50 by {(_f(row.get('rs_63')) or 0) * 100:+.1f}% over 3 months",
              "macd": "MACD above its signal line"}
    for k, ok in a.confirmations.items():
        if ok:
            r.append(labels[k])
    r.append(f"{sum(a.confirmations.values())}/5 confirmations (need {n_needed})")
    risks = [f"volatility: ATR {(_f(row['atr_pct'])):.1f}% of price"]
    if a.stop is not None:
        risks.append(f"setup invalid on a daily close below ₹{a.stop:,.2f} (2×ATR stop)")
    if not a.confirmations.get("relative_strength"):
        risks.append("not outperforming the Nifty 50")
    if (_f(row["rsi14"]) or 0) > 70:
        risks.append("RSI above 70: pullback risk")
    if a.asset_class != "Equity":
        risks.append("ETF tracks international bullion prices and USD/INR; tracking error and expense ratio apply")
    risks.append(NOT_ASSESSED)
    return r, risks


def analyze(universe, frames, benchmark_frame, today, cfg, capital=None, validation="UNVALIDATED"):
    """frames: {symbol: enriched DataFrame of completed bars}. Returns list[Analysis]."""
    from .indicators import sma
    market_uptrend = True
    if benchmark_frame is not None and len(benchmark_frame) >= 200:
        b = benchmark_frame["Close"]
        market_uptrend = bool(b.iloc[-1] > sma(b, 200).iloc[-1])
    results, rows = [], {}
    for inst in universe:
        a = Analysis(inst.symbol, inst.name, inst.asset_class, inst.category, inst.exchange, inst.currency)
        results.append(a)
        frame = frames.get(inst.symbol)
        row, err = check_data(inst, frame, today, cfg)
        if row is not None:
            a.price, a.price_date = round(float(row["Close"]), 2), str(frame.index[-1].date())
            from .market_data import freshness
            a.freshness, a.data_age_days = freshness(frame.index[-1].date(), today, cfg["max_data_age_days"])
        if err:
            a.signal, a.excluded_reason = UNAVAILABLE, err
            continue
        if len(frame) >= 2:
            row = row.copy()
            row["macd_hist_prev"] = frame["macd_hist"].iloc[-6] if len(frame) >= 6 else None
        reason = risk_filters(inst, row, cfg)
        if reason:
            a.signal, a.excluded_reason = AVOID, f"filtered: {reason}"
            continue
        rows[inst.symbol] = (a, row)
    ranks_ret = percentile_ranks({s: _f(r["ret_63"]) for s, (_, r) in rows.items()})
    ranks_rs = percentile_ranks({s: _f(r.get("rs_63")) for s, (_, r) in rows.items()})
    weights = cfg["weights"]
    total_w = sum(weights[k] for k in COMPONENTS)
    needed = cfg["signal"]["min_confirmations"] + (0 if market_uptrend else 1)
    for symbol, (a, row) in rows.items():
        a.components = components_for(row, ranks_ret, ranks_rs, symbol, cfg)
        a.score = round(sum(a.components[k] * weights[k] for k in COMPONENTS) / total_w, 1)
        a.confirmations = confirmations_for(row)
        levels = levels_for(row, cfg, capital)
        note = levels.pop("resistance_note", None)
        for k, v in levels.items():
            setattr(a, k, v)
        a.signal = classify(a, row, cfg, market_uptrend)
        a.horizon = "Swing (2-6 weeks), end-of-day signal" if a.signal in (BUY, WATCH, HOLD) else None
        a.reasons, a.risks = explain(a, row, needed)
        if note:
            a.risks.insert(0, note)
        if not market_uptrend:
            a.risks.insert(0, "broad market weak: Nifty 50 below its 200-DMA (stricter confirmation applied)")
        n_conf = sum(a.confirmations.values())
        if validation == "FAILED":
            a.confidence = "LOW"  # backtest shows no edge after costs
        elif validation == "VALIDATED":
            a.confidence = "HIGH" if n_conf == 5 else "MEDIUM" if n_conf >= 4 else "LOW"
        else:
            a.confidence = "MEDIUM" if n_conf >= 4 else "LOW"
        if a.signal != BUY:  # levels are only actionable for buy candidates
            a.entry_low = a.entry_high = a.target1 = a.target2 = a.risk_reward = None
            a.position_size = None
    return results, market_uptrend


def rankings(results, top_n=5, signal=BUY):
    """Separate, clearly labelled rankings for one signal. For BUY and
    WATCHLIST the strongest scores come first; for AVOID ("ignore") the
    weakest come first. A category can have fewer than top_n (never padded)."""
    weakest_first = signal == AVOID
    pool = [a for a in results if a.signal == signal and a.score is not None]
    picks = sorted(pool, key=lambda a: (a.score if weakest_first else -a.score, a.symbol))
    eq = [a for a in picks if a.asset_class == "Equity"]
    out = {"overall": eq[:top_n]}
    for cat in ("Large-cap", "Mid-cap", "Small-cap"):
        out[cat] = [a for a in eq if a.category == cat][:top_n]
    out["cross_asset"] = picks[:top_n]
    return out
