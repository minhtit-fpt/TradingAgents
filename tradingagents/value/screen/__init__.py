"""Numeric screening: universe, criteria, valuation, and the full-universe pass.

Everything here is pure Python over the point-in-time fact store, with the sole
exception of ``market.py`` (yfinance). Zero LLM cost, so it can be re-run for
every threshold change — which is exactly what phase 3's backtest does.
"""
