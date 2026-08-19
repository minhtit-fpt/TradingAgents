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
