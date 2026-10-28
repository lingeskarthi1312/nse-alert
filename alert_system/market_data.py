"""Universe and price history for the recommendation engine.

History comes from the project's existing provider (Yahoo Finance via
yfinance), downloaded in batches into memory for each run. It is NOT written
to ClickHouse: indicators need ~1 year of bars, while ClickHouse only keeps
month-to-date data. Only the resulting recommendation snapshots are stored.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .logging_setup import log_event

log = logging.getLogger("alert_system.market_data")

CATEGORY_ORDER = {"Large-cap": 0, "Mid-cap": 1, "Small-cap": 2}


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    asset_class: str          # Equity | Gold ETF | Silver ETF
    category: str             # Large-cap | Mid-cap | Small-cap | Commodity ETF
    exchange: str = "NSE"
    currency: str = "INR"
    expiry: str = None        # futures only; ETFs have none


def load_universe(client, commodity_cfg=()):
    """Equities from nse_alerts.stock_categories (Nifty 100 / Midcap 150 /
    Smallcap 250 classification loaded by the dashboard) plus configured
    gold/silver instruments. A symbol listed in several categories keeps the
    largest one, as save_mtd_rankings.py does."""
    best = {}
    for symbol, category in client.query(
            "SELECT DISTINCT symbol, category FROM nse_alerts.stock_categories FINAL").result_rows:
        if category not in CATEGORY_ORDER:
            continue
        if symbol not in best or CATEGORY_ORDER[category] < CATEGORY_ORDER[best[symbol]]:
            best[symbol] = category
    universe = [Instrument(s, s.removesuffix(".NS"), "Equity", c) for s, c in sorted(best.items())]
    for item in commodity_cfg:
        universe.append(Instrument(item["symbol"], item["name"], item["asset_class"], "Commodity ETF",
                                   item.get("exchange", "NSE"), item.get("currency", "INR"), item.get("expiry")))
    return universe


def download_history(symbols, start, end, batch_size=100, retries=2, downloader=None):
    """{symbol: DataFrame[Open, High, Low, Close, Volume]} for [start, end)."""
    if downloader is None:
        import yfinance as yf
        downloader = yf.download
    out = {}
    symbols = list(symbols)
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i + batch_size]
        for attempt in range(1, retries + 1):
            try:
                data = downloader(batch, start=str(start), end=str(end), interval="1d", auto_adjust=False,
                                  progress=False, threads=True, group_by="ticker", timeout=30)
                break
            except Exception as exc:  # network/provider error: retry once, then skip batch
                log_event(log, logging.WARNING, "history batch download failed", component="market_data",
                          attempt=attempt, symbols=len(batch), error=f"{type(exc).__name__}: {exc}"[:200])
                if attempt == retries:
                    data = None
                else:
                    time.sleep(5 * attempt)
        if data is None or data.empty:
            continue
        multi = getattr(data.columns, "nlevels", 1) > 1
        for s in batch:
            if multi and s not in set(data.columns.get_level_values(0)):
                continue
            frame = (data[s] if multi else data)[["Open", "High", "Low", "Close", "Volume"]]
            frame = frame.dropna(subset=["Close"])
            frame = frame[frame["Close"] > 0]
            if not frame.empty:
                frame.index = frame.index.tz_localize(None) if frame.index.tz is not None else frame.index
                out[s] = frame
    return out


def completed_bars(frame, now, tz_name, close_cutoff="15:45"):
    """Drop today's bar while the NSE session may still be running, so a
    partial (delayed) bar is never treated as a completed daily close."""
    local = now.astimezone(ZoneInfo(tz_name))
    hh, mm = (int(x) for x in close_cutoff.split(":"))
    session_done = (local.hour, local.minute) >= (hh, mm)
    if not session_done and len(frame) and frame.index[-1].date() >= local.date():
        return frame.iloc[:-1]
    return frame


def freshness(last_bar_date, today, max_age_days):
    """FRESH if the last completed bar is recent enough, else STALE.
    Calendar days tolerate weekends/one holiday (same rule as daily alerts)."""
    age = (today - last_bar_date).days
    return ("FRESH" if age <= max_age_days else "STALE"), age


def history_window(today, lookback_days):
    return today - timedelta(days=lookback_days), today + timedelta(days=1)


def now_local(tz_name):
    return datetime.now(ZoneInfo(tz_name))
