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
