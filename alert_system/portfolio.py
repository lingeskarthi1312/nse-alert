"""Portfolio analytics for the dashboard (pure functions, unit-tested).

Performance is TIME-WEIGHTED: each day's return excludes the cash you added
(buys) or took out (sales) that day, so it is comparable with an index.
Buys are treated as invested at the start of the day, sale proceeds as
taken out at the end of it:
    r_t = (V_t + S_t) / (V_{t-1} + B_t) - 1
(B_t, S_t at trade prices). This also handles a buy and sale on the same
day: the round trip's realised gain or loss counts in that day's return.
"""
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Trade:
    symbol: str
    qty: float
    buy_date: pd.Timestamp
    buy_price: float
    sell_date: pd.Timestamp = None
    sell_price: float = None


def trades_from_rows(rows):
    out = []
    for r in rows:
        sold = r.get("status") == "SOLD" and r.get("sell_date") and r.get("sell_price")
        out.append(Trade(r["symbol"], float(r["quantity"]), pd.Timestamp(r["buy_date"]), float(r["buy_price"]),
                         pd.Timestamp(r["sell_date"]) if sold else None,
                         float(r["sell_price"]) if sold else None))
    return out


def _session_on_or_after(index, day):
    pos = index.searchsorted(day)
    return index[pos] if pos < len(index) else None


def portfolio_series(trades, closes):
    """closes: DataFrame (index=trading dates, columns=symbols, forward-filled).
    Returns DataFrame[value, flow, ret, index] from the first buy session."""
    if not trades or closes.empty:
        return pd.DataFrame(columns=["value", "flow", "ret", "index"])
    idx = closes.index
    qty = pd.DataFrame(0.0, index=idx, columns=closes.columns)
    buys = pd.Series(0.0, index=idx)
    sells = pd.Series(0.0, index=idx)
    for t in trades:
        if t.symbol not in closes.columns:
            continue
        start = _session_on_or_after(idx, t.buy_date)
        if start is None:
            continue
        end = _session_on_or_after(idx, t.sell_date) if t.sell_date is not None else None
        held = (idx >= start) & ((idx < end) if end is not None else True)
        qty.loc[held, t.symbol] += t.qty
        buys.loc[start] += t.qty * t.buy_price
        if end is not None:
            sells.loc[end] += t.qty * t.sell_price
    value = (qty * closes.fillna(0.0)).sum(axis=1)
    if not (buys > 0).any():
        return pd.DataFrame(columns=["value", "flow", "ret", "index"])
    first = buys[buys > 0].index.min()
    value, buys, sells = value.loc[first:], buys.loc[first:], sells.loc[first:]
    start_capital = value.shift(1).fillna(0.0) + buys
    ret = pd.Series(0.0, index=value.index)
    active = start_capital > 0
    ret[active] = (value[active] + sells[active]) / start_capital[active] - 1
    return pd.DataFrame({"value": value, "flow": buys - sells, "ret": ret, "index": (1 + ret).cumprod()})


def cumulative_pct(index_series, start=None, baseline=None):
    """% change from the value just BEFORE `start` (so the first day's move
    counts). If there is no earlier value, use `baseline` (1.0 for a
    portfolio index = the invested capital) or else the first value."""
    s = index_series.dropna()
    base = None
    if start is not None:
        prior = s.loc[s.index < start]
        base = prior.iloc[-1] if len(prior) else None
        s = s.loc[s.index >= start]
    if s.empty:
        return s
    if base is None:
        base = baseline if baseline is not None else s.iloc[0]
    return (s / base - 1) * 100


def with_baseline(index_series):
    """Prepend the starting capital (1.0) the day before the first session,
    so drawdowns include losses on the very first day."""
    s = index_series.dropna()
    if s.empty:
        return s
    return pd.concat([pd.Series([1.0], index=[s.index[0] - pd.Timedelta(days=1)]), s])


def drawdown(index_series):
    """Returns (drawdown_pct_series, max_dd_pct, current_dd_pct, recovery_days).
    recovery_days: sessions from the max-drawdown peak until the previous high
    was regained, or until today if not yet recovered."""
    s = index_series.dropna()
    if s.empty:
        return s, 0.0, 0.0, 0
    peak = s.cummax()
    dd = (s / peak - 1) * 100
    trough = dd.idxmin()
    peak_value = peak.loc[trough]
    peak_date = s.loc[:trough][s.loc[:trough] >= peak_value].index[-1]
    after = s.loc[trough:]
    recovered = after[after >= peak_value]
    end = recovered.index[0] if not recovered.empty else s.index[-1]
    recovery = len(s.loc[peak_date:end]) - 1
    return dd, float(dd.min()), float(dd.iloc[-1]), int(recovery)


def monthly_returns(index_series):
    s = index_series.dropna()
    if s.empty:
        return s
    month_end = s.resample("ME").last()
    base = pd.concat([pd.Series([1.0], index=[month_end.index[0] - pd.offsets.MonthEnd(1)]), month_end])
    return (base.pct_change().dropna() * 100).rename("return_pct")
