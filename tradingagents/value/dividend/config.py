"""Knobs for the dividend screen, deliberately not in ``value/config.py``.

Independence here is a property of the import graph, not of the directory name:
every arrow points from this package into ``value``, none comes back. No file
outside ``value/dividend/`` mentions a dividend knob, a dividend table or a
dividend criterion, so deleting this directory deletes the entire feature and
leaves the rest of the module compiling. ``tests/value/test_dividend.py``
enforces that mechanically.

Env-var namespace is ``VALUE_DIVIDEND_*``. The parsing helpers are borrowed from
``value/config.py`` rather than copied: they raise on a bad value instead of
falling back to a default, and that behaviour is the point (CLAUDE.md,
"Configuration"). A second copy would be a second place for it to drift.
"""

from ..config import _env_float, _env_int, _env_nonneg_int

# How many fully elapsed calendar years of dividend history the screen wants.
HISTORY_YEARS = _env_int("VALUE_DIVIDEND_HISTORY_YEARS", 10)

# Dividends as a share of earnings, per fiscal year. Above this the payout is
# funded by something other than the year's profit -- the level at which a
# recession forces the board to choose between the dividend and the balance
# sheet.
PAYOUT_MAX = _env_float("VALUE_DIVIDEND_PAYOUT_MAX", 0.60)

# Zero on purpose. A margin that dips for one year is noise; a dividend cut for
# one year is the board telling you what it thinks. Tolerating one cut in ten
# years defeats the whole screen.
CUT_TOLERANCE = _env_nonneg_int("VALUE_DIVIDEND_CUT_TOLERANCE", 0)

# Bad years tolerated on the two coverage criteria. Same default as the business
# screen's and a separate knob all the same: retuning what counts as a durable
# margin must not silently retune what counts as a safe payout.
VIOLATION_TOLERANCE = _env_nonneg_int("VALUE_DIVIDEND_VIOLATION_TOLERANCE", 2)

# --- D5, the price-stability rank -------------------------------------------

# Trailing window the price properties are measured over. Ten years on purpose:
# it reaches back through March 2020, and a window with no crash in it scores
# every name as calm.
STABILITY_YEARS = _env_int("VALUE_DIVIDEND_STABILITY_YEARS", 10)

# Annualised standard deviation of daily returns. Above this the name moves too
# much for a book whose stated job is not to move.
MAX_VOLATILITY = _env_float("VALUE_DIVIDEND_MAX_VOLATILITY", 0.28)

# Worst peak-to-trough fall tolerated, as a magnitude. Price only -- income
# spent as it arrives is not in the account to cushion the fall.
MAX_DRAWDOWN = _env_float("VALUE_DIVIDEND_MAX_DRAWDOWN", 0.40)

# How many names the proposed basket holds. Equal weight, and a count rather
# than a weighting scheme: the module has no basis for preferring one name over
# another once both cleared the same limits.
BASKET_SIZE = _env_int("VALUE_DIVIDEND_BASKET_SIZE", 15)

# Forward yield a name must pay to be in the basket. The screen already proves
# the dividend is durable; this is the operator's income requirement, which no
# amount of durability substitutes for -- ranking by return alone returned a
# 1.32% basket of durable payers on the first live run.
MIN_YIELD = _env_float("VALUE_DIVIDEND_MIN_YIELD", 0.02)
