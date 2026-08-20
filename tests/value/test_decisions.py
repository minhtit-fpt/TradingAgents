"""The decision log — including the decisions to do nothing.

The log's whole value is that it is complete and unedited, so that is what these
assert: a ``pass`` records as fully as a ``buy``, the numbers are snapshotted at
the moment of the decision, a reason is not optional, and changing your mind
appends rather than rewrites.
"""

import unittest

from tradingagents.value import decisions
from tradingagents.value.store import db

from .factories import decade, facts_for
from .test_runner import StubPrices

AS_OF = "2026-01-01"
NOW = "2026-01-01T00:00:00+00:00"


class DecisionLogTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        db.upsert_facts(self.conn, "GOOD", 1, facts_for(decade()))

    def record(self, action="buy", why="because", price=1.0, **kwargs):
        return decisions.record(
            self.conn, "GOOD", action, why,
            decided_on=AS_OF,
            prices=StubPrices({"GOOD": price}),
            now=NOW,
            **kwargs,
        )

    def test_a_decision_snapshots_the_numbers_as_they_stood(self):
        self.record(price=100.0)

        row = db.decisions(self.conn)[0]

        self.assertEqual(row["ticker"], "GOOD")
        self.assertEqual(row["action"], "buy")
        self.assertEqual(row["price"], 100.0)
        self.assertIsNotNone(row["intrinsic_value"])
        self.assertIsNotNone(row["mos_pct"])
        self.assertEqual(row["screen_passed"], 1)
        self.assertEqual(row["recorded_at"], NOW)

    def test_a_pass_is_recorded_as_fully_as_a_buy(self):
        self.record(action="pass", why="entry price not reached", price=100.0)

        row = db.decisions(self.conn)[0]

        self.assertEqual(row["action"], "pass")
        self.assertEqual(row["reason"], "entry price not reached")
        # A declined name is the counterfactual; without its numbers the log is
        # only the purchases, which is survivorship applied to oneself.
        self.assertIsNotNone(row["price"])

    def test_a_decision_with_no_reason_is_refused(self):
        with self.assertRaises(ValueError):
            self.record(why="   ")

    def test_an_unknown_action_is_refused_rather_than_recorded_as_itself(self):
        with self.assertRaises(ValueError):
            self.record(action="yolo")

    def test_changing_your_mind_appends_rather_than_edits(self):
        self.record(action="watch", why="too expensive", price=100.0)
        self.record(action="buy", why="price came to me", price=60.0)

        rows = db.decisions(self.conn)

        self.assertEqual([r["action"] for r in rows], ["watch", "buy"])
        self.assertEqual([r["price"] for r in rows], [100.0, 60.0])

    def test_the_snapshot_can_be_skipped_without_inventing_numbers(self):
        decisions.record(
            self.conn, "GOOD", "watch", "offline", decided_on=AS_OF, snapshot=False, now=NOW
        )

        row = db.decisions(self.conn)[0]

        self.assertIsNone(row["price"])
        self.assertIsNone(row["screen_passed"])
        self.assertIn("no snapshot", "\n".join(decisions.render([row])))

    def test_the_log_filters_by_name_and_date(self):
        self.record(price=100.0)
        decisions.record(
            self.conn, "OTHER", "pass", "not for me",
            decided_on="2026-06-01", snapshot=False, now=NOW,
        )

        self.assertEqual(len(db.decisions(self.conn, ticker="GOOD")), 1)
        self.assertEqual(len(db.decisions(self.conn, since="2026-02-01")), 1)

    def test_an_empty_log_says_so_rather_than_rendering_nothing(self):
        self.assertEqual(decisions.render([]), ["no decisions recorded yet"])
