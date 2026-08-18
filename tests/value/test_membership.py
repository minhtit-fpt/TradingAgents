"""Point-in-time index membership: the universe as it was, not as it is.

Items 8 and 9 of the phase-4 findings were both overturned by the same defect —
a universe taken from EDGAR's *current* ticker file and applied to every
historical rebalance date, so every company acquired, taken private or demoted
since was never a candidate. These tests pin the two properties that fix it:
the lookup never sees a snapshot from the future, and it refuses rather than
guesses when the date falls outside the data.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tradingagents.value.backtest import membership


def _write(directory: str, rows: list[tuple[str, str]], header: bool = True) -> Path:
    path = Path(directory) / "members.csv"
    lines = ["date,tickers"] if header else []
    lines += [f'{day},"{tickers}"' for day, tickers in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


SAMPLE = [
    ("2014-01-06", "AAPL,KO,ATVI"),
    ("2014-03-21", "AAPL,KO,ATVI,BRK.B"),
    ("2015-06-30", "AAPL,KO,BRK.B"),
]


class MembershipLookupTest(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = _write(self._dir.name, SAMPLE)
        self.members = membership.load(self.path)

    def test_exact_snapshot_date_returns_that_snapshot(self):
        self.assertEqual(self.members.as_of("2014-03-21"), ("AAPL", "ATVI", "BRK-B", "KO"))

    def test_date_between_snapshots_returns_the_earlier_one(self):
        """The defining property: a rebalance never sees tomorrow's index."""
        self.assertEqual(self.members.as_of("2014-03-20"), ("AAPL", "ATVI", "KO"))

    def test_a_name_that_left_the_index_is_absent_afterwards(self):
        self.assertIn("ATVI", self.members.as_of("2014-03-21"))
        self.assertNotIn("ATVI", self.members.as_of("2015-06-30"))

    def test_date_before_the_first_snapshot_raises(self):
        """No snapshot means no universe. An empty tuple would read as 'nothing
        qualified' and silently produce a backtest with no trades."""
        with self.assertRaises(membership.MembershipError):
            self.members.as_of("1995-12-31")

    def test_date_shortly_after_the_last_snapshot_forward_fills(self):
        self.assertEqual(self.members.as_of("2015-09-30"), ("AAPL", "BRK-B", "KO"))

    def test_date_far_after_the_last_snapshot_raises(self):
        with self.assertRaises(membership.MembershipError):
            self.members.as_of("2016-06-30")

    def test_share_class_tickers_are_normalised_to_the_store_convention(self):
        """The dataset writes BRK.B; the store and yfinance write BRK-B."""
        self.assertIn("BRK-B", self.members.as_of("2015-06-30"))
        self.assertNotIn("BRK.B", self.members.as_of("2015-06-30"))

    def test_span_reports_the_dates_actually_covered(self):
        self.assertEqual(self.members.span, ("2014-01-06", "2015-06-30"))


class MembershipLoadTest(unittest.TestCase):
    def setUp(self):
        self._dir = TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def test_rows_out_of_order_are_sorted(self):
        path = _write(self._dir.name, list(reversed(SAMPLE)))
        self.assertEqual(membership.load(path).span, ("2014-01-06", "2015-06-30"))

    def test_an_empty_file_raises_rather_than_yielding_an_empty_universe(self):
        path = _write(self._dir.name, [])
        with self.assertRaises(membership.MembershipError):
            membership.load(path)

    def test_a_missing_file_names_the_path_and_the_env_var(self):
        with self.assertRaises(membership.MembershipError) as caught:
            membership.load(Path(self._dir.name) / "absent.csv")
        self.assertIn("VALUE_MEMBERSHIP_PATH", str(caught.exception))

    def test_a_row_with_no_tickers_raises(self):
        path = _write(self._dir.name, [("2014-01-06", "")])
        with self.assertRaises(membership.MembershipError):
            membership.load(path)


if __name__ == "__main__":
    unittest.main()
