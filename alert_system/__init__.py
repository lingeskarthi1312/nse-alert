"""NSE alert automation service: scheduler, WhatsApp outbox, MTD retention.

The existing alert scripts (load_all_stock_prices.py, scheduled_daily_alerts.py,
save_mtd_rankings.py, whatsapp_mtd_rankings.py) remain the source of the alert
logic; this package runs them on schedule and persists every run's state.
"""
__version__ = "2.0.0"
