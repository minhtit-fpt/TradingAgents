"""The dividend screen: is the cash this business hands shareholders durable?

Separate from ``screen/`` on purpose. That one asks whether the *business* is
durable and values it; this one asks whether the *payout* is. A name passes one
and fails the other often enough that collapsing them would hide which.

**Self-contained by construction.** The feature owns its knobs (``config.py``),
its table (``store.py``) and its criteria, and every import points outward into
``value`` — never the reverse. No file outside this directory mentions a
dividend, so removing the directory removes the feature and leaves the rest of
the module intact. ``tests/value/test_dividend.py`` asserts both directions.

Phase D1 is quality only — no prices, no yield, no positions. Everything here is
arithmetic over EDGAR facts plus a per-share dividend history, so the whole
screen runs offline once the cache is warm and costs nothing to replay. And like
every other surface in this module it proposes rather than holds: it names no
action and writes to no ledger. Recording what you did with a name is
``tradingagents.value.decisions``, a separate command on purpose.
"""
