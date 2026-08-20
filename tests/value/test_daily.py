"""The cron: dedupe, heartbeat, and what survives a failure.

An alert-only system fails silently, so the assertions here are mostly about the
two ways that happens — a message sent twice, and a message never sent at all
with nothing left behind to show it was owed. No network: prices are stubbed and
the sender is a list.
"""

import unittest
from unittest.mock import patch

from tradingagents.value.jobs import daily
from tradingagents.value.llm.budget import BudgetExceeded
from tradingagents.value.store import db

from .factories import decade, facts_for
from .test_runner import StubPrices

AS_OF = "2026-01-01"
NOW = "2026-01-01T00:00:00+00:00"


class StubSender:
    """Stands in for Telegram, and remembers everything it was asked to send."""

    def __init__(self, delivers: bool = True):
        self.delivers = delivers
        self.sent: list[str] = []

    def __call__(self, text: str) -> bool:
        self.sent.append(text)
        return self.delivers

    @property
    def alerts(self) -> list[str]:
        return [t for t in self.sent if "MoS —" in t]

    @property
    def heartbeats(self) -> list[str]:
        return [t for t in self.sent if " ok — screened" in t]


class DailyTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        db.upsert_facts(self.conn, "GOOD", 1, facts_for(decade()))

    def run_daily(self, sender, price=1.0, **kwargs):
        return daily.daily(
            self.conn, AS_OF,
            prices=StubPrices({"GOOD": price}),
            with_filing=False,
            refresh_limit=0,
            sender=sender,
            now=lambda: NOW,
            **kwargs,
        )

    def triggered_outcome(self, price=1.0):
        return next(
            o for o in daily.run(self.conn, AS_OF, prices=StubPrices({"GOOD": price}))
            if o.triggered
        )

    def test_a_triggered_name_alerts_once_no_matter_how_often_the_job_runs(self):
        sender = StubSender()

        for _ in range(3):
            self.run_daily(sender)

        self.assertEqual(len(sender.alerts), 1, "the same trigger date must alert once")
        self.assertEqual(len(sender.heartbeats), 3, "every run still reports it ran")
        self.assertIn("GOOD", sender.alerts[0])

    def test_a_quiet_day_still_produces_exactly_one_heartbeat(self):
        sender = StubSender()

        self.run_daily(sender, price=10_000.0)  # far above intrinsic: nothing triggers

        self.assertEqual(sender.alerts, [])
        self.assertEqual(len(sender.heartbeats), 1)
        self.assertIn("triggered 0", sender.heartbeats[0])

    def test_a_send_that_never_lands_leaves_a_row_the_next_run_retries(self):
        # The first run queues the alert and the channel refuses it.
        stalled = StubSender(delivers=False)
        self.run_daily(stalled)

        unsent = db.unsent_alerts(self.conn)
        self.assertEqual([row["ticker"] for row in unsent], ["GOOD"])

        # A later run picks it up. Same trigger date, so it is still one alert.
        recovered = StubSender()
        notes = daily.retry_unsent(self.conn, "2026-01-02", sender=recovered, now=lambda: NOW)

        self.assertEqual(db.unsent_alerts(self.conn), [])
        self.assertTrue(any("re-sent GOOD" in n for n in notes))
        self.assertIn("delayed", recovered.sent[0])

    def test_the_same_day_is_not_retried_while_the_run_is_still_working_on_it(self):
        stalled = StubSender(delivers=False)
        self.run_daily(stalled)
        again = StubSender()

        daily.retry_unsent(self.conn, AS_OF, sender=again, now=lambda: NOW)

        self.assertEqual(again.sent, [])

    def test_an_exhausted_budget_costs_the_briefing_and_not_the_alert(self):
        sender = StubSender()
        outcome = self.triggered_outcome()

        with patch.object(daily, "read_filing", side_effect=BudgetExceeded("run cap reached")):
            daily.alert_once(
                self.conn, outcome, AS_OF, sender=sender, now=lambda: NOW, client=object()
            )

        self.assertEqual(len(sender.alerts), 1)
        self.assertIn("budget exhausted", sender.alerts[0])

    def test_the_queue_row_is_written_before_the_send_so_a_crash_cannot_lose_it(self):
        outcome = self.triggered_outcome()

        def explode(_text):
            raise RuntimeError("channel down")

        with self.assertRaises(RuntimeError):
            daily.alert_once(
                self.conn, outcome, AS_OF, with_filing=False, sender=explode, now=lambda: NOW
            )

        self.assertEqual([row["ticker"] for row in db.unsent_alerts(self.conn)], ["GOOD"])

    def test_a_dry_run_leaves_no_trace_that_could_silence_the_real_one(self):
        rehearsal = StubSender()
        self.run_daily(rehearsal, record=False)

        self.assertEqual(len(rehearsal.alerts), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0], 0,
            "a dry run that claims the dedupe row would mute the real run behind it",
        )

        # And the real run that follows still alerts.
        real = StubSender()
        self.run_daily(real)
        self.assertEqual(len(real.alerts), 1)

    def test_only_stale_names_are_queued_for_re_ingest(self):
        # The newest synthetic fact is filed 2025-02-10, so the window closes
        # 400 days later, in mid-March 2026. Either side of that boundary.
        self.assertEqual(daily.stale_tickers(self.conn, "2026-03-01", 5), [])
        self.assertEqual(daily.stale_tickers(self.conn, "2026-04-01", 5), ["GOOD"])
