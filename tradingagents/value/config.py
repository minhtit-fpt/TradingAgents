"""Configuration for the value module.

Own env-var namespace: every knob here is ``VALUE_*``, never ``TRADINGAGENTS_*``.
Own state directory: ``~/.tradingagents/value/``, separate from ``results_dir`` /
``data_cache_dir`` / ``memory_log_path``.

Invalid env values raise at import rather than falling back to a default — a
misconfigured budget cap must fail loudly, not quietly run uncapped.
"""

import os
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser() if raw else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for {name}: expected an integer, got {raw!r}") from exc
    if value <= 0:
        raise ValueError(f"Invalid value for {name}: must be positive, got {value}")
    return value


def _env_nonneg_int(name: str, default: int) -> int:
    """Like ``_env_int`` but admits zero — tolerating no violation-year is valid."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for {name}: expected an integer, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"Invalid value for {name}: must not be negative, got {value}")
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid value for {name}: expected a number, got {raw!r}") from exc
    if value < 0:
        raise ValueError(f"Invalid value for {name}: must not be negative, got {value}")
    return value


VALUE_HOME = _env_path("VALUE_HOME", Path.home() / ".tradingagents" / "value")

LLM_CACHE_DIR = _env_path("VALUE_LLM_CACHE_DIR", VALUE_HOME / "llm_cache")
BUDGET_LEDGER_PATH = _env_path("VALUE_BUDGET_LEDGER", VALUE_HOME / "llm_budget.jsonl")
DB_PATH = _env_path("VALUE_DB_PATH", VALUE_HOME / "value.db")

# SEC requires a descriptive User-Agent carrying a real contact address. There is
# no default: an anonymous or fake agent gets the server's IP blocked, and EDGAR
# is the only source of statements this module has.
SEC_USER_AGENT = os.environ.get("VALUE_SEC_USER_AGENT", "")

# SEC's published ceiling is 10 requests/second. Sit under it — the cost of being
# slightly slow is nothing next to the cost of being banned.
EDGAR_REQUESTS_PER_SECOND = _env_float("VALUE_EDGAR_RPS", 8.0)
EDGAR_MAX_RETRIES = _env_int("VALUE_EDGAR_MAX_RETRIES", 5)
EDGAR_TIMEOUT_SECONDS = _env_float("VALUE_EDGAR_TIMEOUT", 30.0)

# How many fiscal years of history the screen reasons over.
HISTORY_YEARS = _env_int("VALUE_HISTORY_YEARS", 10)

# Fraction of concepts that must resolve for a ticker to be screenable. Below
# this it is excluded rather than screened on partial data (plan section 9.6):
# a ratio computed from missing inputs is a confidently wrong number.
MIN_CONCEPT_COVERAGE = _env_float("VALUE_MIN_COVERAGE", 0.8)

# --- Screening thresholds (plan section 6) -------------------------------------
#
# Every one of these is a knob, not a law: they are the levels from "Warren
# Buffett and the Interpretation of Financial Statements", and phase 3's free
# numeric backtest exists to tune them. Keep them here so tuning never edits code.

GROSS_MARGIN_MIN = _env_float("VALUE_GROSS_MARGIN_MIN", 0.40)
NET_MARGIN_MIN = _env_float("VALUE_NET_MARGIN_MIN", 0.20)
SGA_TO_GROSS_PROFIT_MAX = _env_float("VALUE_SGA_MAX", 0.30)
RND_TO_GROSS_PROFIT_MAX = _env_float("VALUE_RND_MAX", 0.30)
DA_TO_GROSS_PROFIT_MAX = _env_float("VALUE_DA_MAX", 0.10)
INTEREST_TO_OPERATING_INCOME_MAX = _env_float("VALUE_INTEREST_MAX", 0.15)
LONG_TERM_DEBT_TO_NET_INCOME_MAX = _env_float("VALUE_LTD_MAX", 4.0)
DEBT_TO_EQUITY_MAX = _env_float("VALUE_DEBT_EQUITY_MAX", 0.8)
ROE_MIN = _env_float("VALUE_ROE_MIN", 0.15)
CAPEX_TO_NET_INCOME_MAX = _env_float("VALUE_CAPEX_MAX", 0.25)

# How many of the ten years may violate a criterion and still pass it. One
# recession year should not disqualify an otherwise excellent business; five
# should. Open item #3 in the plan — settle it empirically in phase 3.
VIOLATION_TOLERANCE = _env_nonneg_int("VALUE_VIOLATION_TOLERANCE", 1)

# --- Valuation (plan section 6, "intrinsic value") -----------------------------

PROJECTION_YEARS = _env_int("VALUE_PROJECTION_YEARS", 10)
# Caps on optimism. A 40% fitted growth rate is a fluke being extrapolated for a
# decade, and a 40x terminal multiple is the market's mood, not the business.
GROWTH_RATE_CAP = _env_float("VALUE_GROWTH_CAP", 0.15)
TERMINAL_PE_CAP = _env_float("VALUE_TERMINAL_PE_CAP", 15.0)
# Discounting at a 1% Treasury yield values a stable earner at absurd multiples,
# so the discount rate is floored regardless of what the bond market is doing.
DISCOUNT_RATE_FLOOR = _env_float("VALUE_DISCOUNT_FLOOR", 0.04)

# The alert trigger: buy-side margin of safety against computed intrinsic value.
MARGIN_OF_SAFETY_MIN = _env_float("VALUE_MOS_MIN", 0.30)

# --- Backtest (plan section 10) ------------------------------------------------
#
# The tier-1 backtest costs nothing but wall-clock time, so these are tuning
# knobs rather than budget decisions.

BACKTEST_START_CASH = _env_float("VALUE_BACKTEST_CASH", 100_000.0)
# 10 bps per side. A quarterly-rebalanced screen holding tens of names trades
# rarely, but a backtest run at zero commission is a backtest of a broker that
# does not exist.
BACKTEST_COMMISSION = _env_float("VALUE_BACKTEST_COMMISSION", 0.001)
BACKTEST_BENCHMARK = os.environ.get("VALUE_BACKTEST_BENCHMARK", "SPY")

# Hard USD ceilings. The per-run cap bounds a runaway loop inside one job; the
# per-month cap bounds the whole deployment. Both fail closed (see budget.py).
RUN_BUDGET_USD = _env_float("VALUE_RUN_BUDGET_USD", 2.0)
MONTH_BUDGET_USD = _env_float("VALUE_MONTH_BUDGET_USD", 10.0)

# USD per million tokens, as (input, output).
#
# VERIFY against DeepSeek's current pricing page before phase 6 — these are
# planning estimates (open item #2 in the plan). A model absent from this table
# raises instead of being charged $0: an untracked model is exactly how a budget
# cap silently stops capping.
MODEL_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "deepseek-v4-flash": (0.28, 0.42),
    "deepseek-v4-pro": (0.55, 2.19),
}
