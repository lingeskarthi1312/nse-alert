"""Recommendation + backtest jobs: fetch history, run the engine, persist.

Persisted:
  * ClickHouse nse_alerts.recommendations - one row per ranked instrument per
    run_date (MTD retention applies; re-runs replace the day's snapshot);
  * state/recommendations_latest.json   - full analysis incl. gold/silver
    status and excluded reasons (what the email report reads);
  * state/backtest_latest.json          - latest walk-forward validation.
Raw history is never stored (indicators need a year, ClickHouse keeps MTD).
"""
import json
import logging
from datetime import timedelta

from .backtest import run_backtest, summarize
from .indicators import enrich
from .logging_setup import log_event
from .market_data import completed_bars, download_history, load_universe
from .recommend import AVOID, BUY, WATCH, analyze, rankings
from .timeutil import local_today, to_iso

log = logging.getLogger("alert_system.reco_job")

TABLE = "nse_alerts.recommendations"
DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE}
(
    run_date Date,
    generated_at DateTime,
    ranking LowCardinality(String),
    rank UInt8,
    symbol String,
    name String,
    asset_class LowCardinality(String),
    category LowCardinality(String),
    exchange LowCardinality(String),
    price Float64,
    price_date Date,
    signal LowCardinality(String),
    score Float64,
    components String,
    entry_low Nullable(Float64),
    entry_high Nullable(Float64),
    stop Nullable(Float64),
    target1 Nullable(Float64),
    target2 Nullable(Float64),
    risk_reward Nullable(Float64),
    horizon String,
    confidence LowCardinality(String),
    validation LowCardinality(String),
    reasons String,
    risks String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (run_date, ranking, rank)
"""
COLUMNS = ["run_date", "generated_at", "ranking", "rank", "symbol", "name", "asset_class", "category", "exchange",
           "price", "price_date", "signal", "score", "components", "entry_low", "entry_high", "stop", "target1",
           "target2", "risk_reward", "horizon", "confidence", "validation", "reasons", "risks"]


def validation_status(settings):
    path = settings.state_dir / "backtest_latest.json"
    try:
        return json.loads(path.read_text()).get("validation", "UNVALIDATED")
    except (OSError, ValueError):
        return "UNVALIDATED"


def _prepare(settings, client, now, history_days):
    cfg = settings.recommendations
    today = local_today(settings.timezone, now)
    universe = load_universe(client, cfg["commodities"])
    start, end = today - timedelta(days=history_days), today + timedelta(days=1)
    raw = download_history([i.symbol for i in universe] + [cfg["benchmark"]], start, end)
    bench = raw.pop(cfg["benchmark"], None)
    if bench is not None:
        bench = completed_bars(bench, now, settings.timezone)
    frames = {s: enrich(completed_bars(f, now, settings.timezone),
                        bench["Close"] if bench is not None else None) for s, f in raw.items()}
    return today, universe, frames, bench


def run_recommendations(settings, client, now):
    cfg = settings.recommendations
    today, universe, frames, bench = _prepare(settings, client, now, cfg["history_days"])
    if bench is None:
        raise RuntimeError(f"benchmark {cfg['benchmark']} history unavailable; refusing to score without market context")
    if len(frames) < 0.5 * len(universe):
        raise RuntimeError(f"history downloaded for only {len(frames)}/{len(universe)} instruments")
    validation = validation_status(settings)
    results, uptrend = analyze(universe, frames, bench, today, cfg, settings.report.portfolio_capital, validation)
    ranked = rankings(results, cfg["top_n"])
    gen = to_iso(now)
    rows = []
    for name, items in ranked.items():
        for rank, a in enumerate(items, 1):
            rows.append([today, now.replace(tzinfo=None, microsecond=0), name, rank, a.symbol, a.name,
                         a.asset_class, a.category, a.exchange, a.price, _date(a.price_date), a.signal, a.score,
                         json.dumps(a.components), a.entry_low, a.entry_high, a.stop, a.target1, a.target2,
                         a.risk_reward, a.horizon or "", a.confidence or "", validation,
                         " | ".join(a.reasons), " | ".join(a.risks)])
    client.command(DDL)
    # Idempotent: replace this day's snapshot (lightweight delete, then insert).
    client.command(f"DELETE FROM {TABLE} WHERE run_date = toDate(%(d)s)", parameters={"d": today.isoformat()})
    if rows:
        client.insert(TABLE, rows, column_names=COLUMNS)
    commodities = [a.to_dict() for a in results if a.asset_class != "Equity"]
    snapshot = {
        "run_date": today.isoformat(), "generated_at": gen, "market_uptrend": uptrend, "validation": validation,
        "universe": len(universe), "with_history": len(frames),
        "signals": {s: sum(a.signal == s for a in results) for s in {a.signal for a in results}},
        "rankings": {k: [a.to_dict() for a in v] for k, v in ranked.items()},
        # same lists for the WATCH and IGNORE (AVOID) views in the dashboard
        "rankings_watch": {k: [a.to_dict() for a in v] for k, v in rankings(results, cfg["top_n"], WATCH).items()},
        "rankings_avoid": {k: [a.to_dict() for a in v] for k, v in rankings(results, cfg["top_n"], AVOID).items()},
        "commodities": commodities,
        "unavailable": [{"symbol": a.symbol, "reason": a.excluded_reason}
                        for a in results if a.signal == "UNAVAILABLE"][:50],
    }
    tmp = settings.state_dir / "recommendations_latest.json.tmp"
    tmp.write_text(json.dumps(snapshot, default=str, indent=1))
    tmp.replace(settings.state_dir / "recommendations_latest.json")
    log_event(log, logging.INFO, "recommendations generated", component="recommendations",
              run_date=today.isoformat(), universe=len(universe), with_history=len(frames),
              buy_candidates=sum(a.signal == BUY for a in results), market_uptrend=uptrend,
              validation=validation, ranked_rows=len(rows), signals=snapshot["signals"])
    return snapshot


def run_backtest_job(settings, client, now):
    cfg = settings.recommendations
    today, universe, frames, bench = _prepare(settings, client, now, cfg["backtest"]["history_days"])
    if bench is None:
        raise RuntimeError("benchmark history unavailable for backtest")
    trades = run_backtest(universe, frames, bench, cfg)
    report = summarize(trades, cfg, len(frames), bench.index[0].date(), bench.index[-1].date())
    (settings.state_dir / "backtest_latest.json").write_text(json.dumps(report, default=str, indent=1))
    log_event(log, logging.INFO, "backtest finished", component="backtest", validation=report["validation"],
              trades=report["overall"].get("trades"), out_of_sample=report["segments"]["out_of_sample"])
    return report


def _date(text):
    from datetime import date
    return date.fromisoformat(text) if text else None
