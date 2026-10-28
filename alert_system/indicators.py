"""Technical indicators on daily OHLCV frames (pandas). All functions use
only data up to each row (no look-ahead): rolling/ewm windows end at the row."""
import numpy as np
import pandas as pd


def sma(s, n):
    return s.rolling(n, min_periods=n).mean()


def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def rsi(close, n=14):
    """Wilder's RSI."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(loss != 0, 100.0)


def macd(close, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return line, sig, line - sig


def atr(high, low, close, n=14):
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def enrich(df, benchmark_close=None):
    """Add indicator columns to a frame with Open/High/Low/Close/Volume."""
    out = df.copy()
    c, h, l, v = out["Close"], out["High"], out["Low"], out["Volume"]
    out["sma20"], out["sma50"], out["sma200"] = sma(c, 20), sma(c, 50), sma(c, 200)
    out["sma50_slope"] = out["sma50"] / out["sma50"].shift(10) - 1
    out["rsi14"] = rsi(c)
    out["macd"], out["macd_signal"], out["macd_hist"] = macd(c)
    out["atr14"] = atr(h, l, c)
    out["atr_pct"] = out["atr14"] / c * 100
    out["ret_21"] = c / c.shift(21) - 1
    out["ret_63"] = c / c.shift(63) - 1
    out["vol20"] = sma(v, 20)
    out["vol50"] = sma(v, 50)
    out["traded_value20"] = (c * v).rolling(20, min_periods=20).median()
    out["support20"] = l.rolling(20, min_periods=20).min()
    out["resistance60"] = h.rolling(60, min_periods=60).max()
    out["high252"] = h.rolling(252, min_periods=120).max()
    # Volume confirmation: up-day volume vs down-day volume over 20 sessions
    up = (c.diff() > 0).astype(float)
    out["updown_vol_ratio"] = (v * up).rolling(20, min_periods=20).sum() / (
        (v * (1 - up)).rolling(20, min_periods=20).sum().replace(0, np.nan))
    if benchmark_close is not None:
        b = benchmark_close.reindex(out.index).ffill()
        out["rs_63"] = out["ret_63"] - (b / b.shift(63) - 1)
    return out
