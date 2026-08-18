"""Point-in-time index membership: who was a candidate on a given date.

The backtest used to apply one static ticker list — EDGAR's `company_tickers.json`
in 2026 size order — to every rebalance date in the window. Every company that
mattered in 2014 and has since been acquired, taken private or demoted was
therefore never a candidate, and the bias runs one way: the names that vanish
are the ones that did badly. Items 7, 8 and 9 of the phase-4 findings each
produced a different verdict, and two of the three died on exactly this.

The fix is not a correction factor. It is reading membership *as of* the date,
which makes survivorship zero by construction. This module is the lookup; the
CSV is a third-party point-in-time dataset (`fja05680/sp500`) cached at
``MEMBERSHIP_PATH``.

Two rules the lookup does not bend:

- **The snapshot must be at or before the date.** Not the nearest — the nearest
  is a look-ahead of up to a week, and knowing that a company will join the
  index next Tuesday is exactly the kind of knowledge a 2014 rebalance did not
  have.
- **Outside the data it raises.** An empty universe is indistinguishable from
  "nothing qualified" once it reaches the report, and that reads as a strategy
  result rather than a missing file.
"""

from __future__ import annotations

import csv
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from ..config import MEMBERSHIP_MAX_STALE_DAYS, MEMBERSHIP_PATH


class MembershipError(Exception):
    """The universe for a date cannot be determined from the cached snapshots."""


@dataclass(frozen=True)
class Membership:
    """Index membership over time: one frozen set of tickers per snapshot date."""

    dates: tuple[str, ...]
    members: tuple[frozenset[str], ...]
    source: Path

    @property
    def span(self) -> tuple[str, str]:
        return self.dates[0], self.dates[-1]

    def as_of(self, day: str) -> tuple[str, ...]:
        """Members on ``day``, from the newest snapshot at or before it.

        Raises ``MembershipError`` rather than returning an empty tuple when
        ``day`` precedes the data or sits more than ``MEMBERSHIP_MAX_STALE_DAYS``
        past its end.
        """
        index = bisect_right(self.dates, day) - 1
        if index < 0:
            raise MembershipError(
                f"no membership snapshot at or before {day}; {self.source} starts "
                f"{self.dates[0]}"
            )
        newest = self.dates[-1]
        if day > newest:
            stale = (date.fromisoformat(day) - date.fromisoformat(newest)).days
            if stale > MEMBERSHIP_MAX_STALE_DAYS:
                raise MembershipError(
                    f"{day} is {stale} days past the newest snapshot {newest} in "
                    f"{self.source} (limit {MEMBERSHIP_MAX_STALE_DAYS}); refresh the file"
                )
        return tuple(sorted(self.members[index]))

    def covers(self, day: str) -> bool:
        """Whether ``as_of(day)`` would succeed. For choosing a window, not for
        screening — screening should let the error surface."""
        if day < self.dates[0]:
            return False
        newest = self.dates[-1]
        if day <= newest:
            return True
        limit = date.fromisoformat(newest) + timedelta(days=MEMBERSHIP_MAX_STALE_DAYS)
        return date.fromisoformat(day) <= limit


def normalise(ticker: str) -> str:
    """Dataset share-class convention (``BRK.B``) to the store's (``BRK-B``)."""
    return ticker.strip().upper().replace(".", "-")


def load(path: Path | str | None = None) -> Membership:
    """Read the cached membership CSV (columns: ``date``, ``tickers``)."""
    source = Path(path) if path is not None else MEMBERSHIP_PATH
    if not source.exists():
        raise MembershipError(
            f"no membership file at {source}; set VALUE_MEMBERSHIP_PATH or cache the "
            "point-in-time S&P 500 snapshots there (see plan phase 4b step 1)"
        )

    rows: dict[str, frozenset[str]] = {}
    with source.open(encoding="utf-8", newline="") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            day = (row.get("date") or "").strip()
            raw = row.get("tickers") or ""
            if not day:
                raise MembershipError(f"{source}:{line_no} has no date")
            tickers = frozenset(normalise(t) for t in raw.split(",") if t.strip())
            if not tickers:
                raise MembershipError(f"{source}:{line_no} ({day}) lists no tickers")
            rows[day] = tickers

    if not rows:
        raise MembershipError(f"{source} holds no snapshots")

    dates = tuple(sorted(rows))
    return Membership(dates, tuple(rows[day] for day in dates), source)
