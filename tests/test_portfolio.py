import pandas as pd
import pytest

from alert_system.portfolio import Trade, cumulative_pct, drawdown, monthly_returns, portfolio_series, trades_from_rows


def closes(values, start="2026-01-05"):
    idx = pd.bdate_range(start, periods=len(next(iter(values.values()))))
    return pd.DataFrame(values, index=idx)


def test_twr_ignores_cash_added_later():
    c = closes({"A.NS": [100, 110, 110, 121]})
    trades = [Trade("A.NS", 1, pd.Timestamp("2026-01-05"), 100.0),
              Trade("A.NS", 9, pd.Timestamp("2026-01-07"), 110.0)]  # big top-up after +10%
    s = portfolio_series(trades, c)
    assert s["value"].tolist() == [100, 110, 1100, 1210]
    # +10% then 0% then +10%: the top-up does not count as performance
    assert s["index"].round(4).tolist() == [1.0, 1.1, 1.1, 1.21]


def test_sale_proceeds_are_withdrawals_not_losses():
    c = closes({"A.NS": [100, 100, 100], "B.NS": [50, 55, 55]})
    trades = trades_from_rows([
        {"symbol": "A.NS", "quantity": 1, "buy_date": "2026-01-05", "buy_price": 100, "status": "SOLD",
         "sell_date": "2026-01-06", "sell_price": 100},
        {"symbol": "B.NS", "quantity": 2, "buy_date": "2026-01-05", "buy_price": 50, "status": "ACTIVE"},
    ])
    s = portfolio_series(trades, c)
    assert s["value"].tolist() == [200, 110, 110]
    assert s["flow"].tolist() == [200, -100, 0]
    # day 2: (110 + 100) / 200 - 1 = +5% (B gained 10% on half the money)
    assert round(s["ret"].iloc[1], 4) == 0.05


def test_buy_on_weekend_starts_next_session_and_buy_price_vs_close():
    c = closes({"A.NS": [102, 104]})  # Mon, Tue
    s = portfolio_series([Trade("A.NS", 1, pd.Timestamp("2026-01-03"), 100.0)], c)  # Saturday
    assert s.index[0] == pd.Timestamp("2026-01-05") and round(s["ret"].iloc[0], 4) == 0.02


def test_unknown_symbol_and_empty_inputs():
    assert portfolio_series([], closes({"A.NS": [1]})).empty
    s = portfolio_series([Trade("X.NS", 1, pd.Timestamp("2026-01-05"), 1.0)], closes({"A.NS": [1, 2]}))
    assert s.empty or s["value"].sum() == 0


def test_drawdown_metrics():
    idx = pd.bdate_range("2026-01-05", periods=7)
    s = pd.Series([100, 120, 90, 96, 120, 125, 110], index=idx)
    dd, max_dd, cur, rec = drawdown(s)
    assert max_dd == pytest.approx(-25.0)
    assert cur == pytest.approx((110 / 125 - 1) * 100)
    assert rec == 3  # peak 120 (day 2) -> back to 120 on day 5


def test_drawdown_not_recovered_counts_to_today():
    s = pd.Series([100, 80, 85], index=pd.bdate_range("2026-01-05", periods=3))
    _, max_dd, cur, rec = drawdown(s)
    assert max_dd == pytest.approx(-20.0) and rec == 2


def test_monthly_returns_and_cumulative():
    idx = pd.to_datetime(["2026-01-30", "2026-02-27", "2026-03-31"])
    s = pd.Series([1.10, 1.21, 1.089], index=idx)
    m = monthly_returns(s)
    assert [round(x, 1) for x in m.tolist()] == [10.0, 10.0, -10.0]
    assert cumulative_pct(s).round(1).tolist() == [0.0, 10.0, -1.0]


def test_same_day_buy_and_sell_counts_realised_result():
    c = closes({"A.NS": [354, 360], "B.NS": [636, 640]})
    trades = trades_from_rows([
        {"symbol": "A.NS", "quantity": 10, "buy_date": "2026-01-05", "buy_price": 354, "status": "SOLD",
         "sell_date": "2026-01-05", "sell_price": 100},
        {"symbol": "B.NS", "quantity": 1, "buy_date": "2026-01-05", "buy_price": 627.85, "status": "ACTIVE"},
    ])
    s = portfolio_series(trades, c)
    # (636 value + 1000 proceeds) / (3540 + 627.85 invested) - 1
    assert round(s["ret"].iloc[0], 4) == round((636 + 1000) / (3540 + 627.85) - 1, 4)
    assert round(s["ret"].iloc[1], 4) == round(640 / 636 - 1, 4)


def test_first_day_loss_counts_in_cumulative_and_drawdown():
    from alert_system.portfolio import with_baseline
    idx = pd.bdate_range("2026-01-05", periods=3)
    s = pd.Series([0.4, 0.41, 0.42], index=idx)  # -60% on day one, then small gains
    assert round(cumulative_pct(s, idx[0], baseline=1.0).iloc[-1], 1) == -58.0
    _, max_dd, cur, _ = drawdown(with_baseline(s))
    assert round(max_dd, 1) == -60.0 and round(cur, 1) == -58.0
    bench = pd.Series([100, 101, 102, 103], index=pd.bdate_range("2026-01-02", periods=4))
    assert round(cumulative_pct(bench, idx[0]).iloc[0], 2) == 1.0  # vs previous close, not same day
