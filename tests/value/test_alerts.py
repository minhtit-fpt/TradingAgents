"""What gets sent, and what it says first.

The composition is pure, so these run with no network and no store. The one
thing worth asserting hardest is the ordering: phase 6 measured the ``verdict``
as a gate and found it covers 70% of this screen's picks, so a message that
leads with it would be leading with the least informative line it has.
"""

import unittest

from tradingagents.value.alerts import message, telegram
from tradingagents.value.analyst.schemas import (
    Concentration,
    Confidence,
    Moat,
    MoatTrend,
    ValueAssessment,
    Verdict,
)
from tradingagents.value.config import BACKTEST_MIN_POSITIONS, BACKTEST_POSITION_CAP
from tradingagents.value.report import build
from tradingagents.value.screen.runner import Outcome
from tradingagents.value.store import db

from .factories import decade, facts_for
from .test_runner import StubPrices

AS_OF = "2026-01-01"

ASSESSMENT = ValueAssessment(
    ticker="GOOD",
    verdict=Verdict.CAUTION,
    moat=Moat.WIDE,
    moat_trend=MoatTrend.ERODING,
    customer_concentration=Concentration.SEVERE,
    accounting_flags=["receivables up 30% against flat revenue"],
    key_risks=["one plant makes 60% of units"],
    thesis="A good business at a fair price.",
    confidence=Confidence.MEDIUM,
    evidence_gaps=["Item 7 was truncated"],
)


def outcome_at(price: float) -> Outcome:
    """A real screened outcome — no hand-built Valuation to drift from the real one."""
    conn = db.connect(":memory:")
    try:
        db.upsert_facts(conn, "GOOD", 1, facts_for(decade()))
        return build(
            conn, "GOOD", AS_OF, prices=StubPrices({"GOOD": price}), ingest_missing=False
        ).outcome
    finally:
        conn.close()


class BriefingOrderTest(unittest.TestCase):
    def test_flags_risks_and_gaps_all_precede_the_verdict(self):
        text = "\n".join(message.briefing(ASSESSMENT))

        verdict_at = text.index("verdict:")
        for field in ("accounting flags:", "key risks:", "evidence gaps:", "thesis:"):
            self.assertLess(text.index(field), verdict_at, f"{field} must precede the verdict")

    def test_the_verdict_carries_the_measurement_that_deflates_it(self):
        text = "\n".join(message.briefing(ASSESSMENT))

        self.assertIn("verdict: caution", text)
        self.assertIn("70%", text)

    def test_empty_lists_say_none_stated_rather_than_vanishing(self):
        quiet = ASSESSMENT.model_copy(update={"accounting_flags": [], "key_risks": []})

        text = "\n".join(message.briefing(quiet))

        self.assertIn("accounting flags: none stated", text)
        self.assertIn("key risks: none stated", text)


class TriggerAlertTest(unittest.TestCase):
    def test_the_alert_carries_the_measured_sizing_note_and_names_no_action(self):
        text = message.trigger_alert(outcome_at(1.0), AS_OF)

        self.assertIn(f"{BACKTEST_POSITION_CAP:.0%} max per name", text)
        self.assertIn(f"{BACKTEST_MIN_POSITIONS} names minimum", text)
        self.assertIn("48.6% to 18.9%", text)
        # The one thing the alert must never do is tell the operator to act.
        self.assertNotIn("buy now", text.lower())
        self.assertIn("Not a recommendation", text)

    def test_an_unread_filing_says_so_rather_than_reading_as_a_clean_one(self):
        text = message.trigger_alert(outcome_at(1.0), AS_OF, None, "budget exhausted")

        self.assertIn("filing: not read — budget exhausted", text)
        self.assertNotIn("verdict:", text)

    def test_a_read_filing_leads_with_the_flags(self):
        text = message.trigger_alert(outcome_at(1.0), AS_OF, ASSESSMENT)

        self.assertLess(text.index("accounting flags:"), text.index("verdict:"))
        self.assertIn("python -m tradingagents.value.decisions record", text)


class HeartbeatTest(unittest.TestCase):
    def test_a_day_with_nothing_triggered_still_produces_a_line(self):
        line = message.heartbeat([], "2026-11-14")

        self.assertIn("2026-11-14 ok", line)
        self.assertIn("triggered 0", line)

    def test_the_heartbeat_is_one_line_so_it_cannot_be_mistaken_for_an_alert(self):
        line = message.heartbeat([outcome_at(1000.0)], "2026-11-14")

        self.assertNotIn("\n", line)
        self.assertIn("closest GOOD", line)


class StubResponse:
    def __init__(self, status_code=200, text="ok"):
        self.status_code = status_code
        self.text = text


class StubSession:
    def __init__(self, response=None):
        self.response = response or StubResponse()
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append((url, json, timeout))
        return self.response


class SendTest(unittest.TestCase):
    def test_no_token_prints_instead_of_sending_and_reports_that_it_did_not_send(self):
        session = StubSession()

        sent = telegram.send("hello", token="", chat_id="", session=session)

        self.assertFalse(sent)
        self.assertEqual(session.calls, [])

    def test_a_non_200_raises_so_the_caller_never_records_it_as_sent(self):
        session = StubSession(StubResponse(429, "Too Many Requests"))

        with self.assertRaises(telegram.TelegramError):
            telegram.send("hello", token="t", chat_id="c", session=session)

    def test_an_overlong_message_is_split_rather_than_truncated(self):
        session = StubSession()
        long_text = "\n".join(f"line {i} " + "x" * 100 for i in range(100))

        telegram.send(long_text, token="t", chat_id="c", session=session)

        self.assertGreater(len(session.calls), 1)
        for _url, payload, _timeout in session.calls:
            self.assertLessEqual(len(payload["text"]), telegram.MAX_CHARS)
        # Nothing was dropped: the last line still arrives.
        self.assertIn("line 99", session.calls[-1][1]["text"])
