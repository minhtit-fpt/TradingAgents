"""Phase 6: the veto must be applied where it is claimed, and judged against the floor.

No network and no LLM anywhere here — the analyst, the filing fetch and the
simulator are all injected. What is pinned is the arithmetic that turns a set of
verdicts into a verdict about the verdicts.
"""

import random
import unittest

from tradingagents.value.analyst.schemas import ValueAssessment, Verdict
from tradingagents.value.backtest import llm_sample, numeric, portfolio, stats
from tradingagents.value.edgar import filings
from tradingagents.value.llm.budget import BudgetExceeded

SECTIONS = filings.Sections(
    business="We sell fasteners.",
    risk_factors="A construction downturn would hurt.",
    mdna="Sales rose on price.",
)


def assessment(ticker="FAST", verdict=Verdict.PROCEED):
    return ValueAssessment(
        ticker=ticker,
        verdict=verdict,
        moat="wide",
        moat_trend="stable",
        customer_concentration="none_disclosed",
        accounting_flags=[],
        key_risks=["Construction cycle"],
        thesis="Branch density is the advantage.",
        confidence="high",
        evidence_gaps=[],
    )


def curve(values, first_year=2015):
    """A portfolio value per quarter-end, as ``Result.curve`` records it."""
    days = []
    year, quarter = first_year, 0
    for value in values:
        month, day = ((3, 31), (6, 30), (9, 30), (12, 31))[quarter]
        days.append((f"{year}-{month:02d}-{day:02d}", float(value)))
        quarter += 1
        if quarter == 4:
            quarter, year = 0, year + 1
    return tuple(days)


def result(values, years=5.0):
    return portfolio.Result(
        start_value=values[0],
        end_value=values[-1],
        years=years,
        max_drawdown=0.1,
        trades=10,
        winners=6,
        average_bars_held=200.0,
        curve=curve(values),
    )


class EventTest(unittest.TestCase):
    """The unit tier 3 is paid per is (company, fiscal year), not (company, date)."""

    schedule = [
        ("2020-03-31", ("AAA", "BBB")),
        ("2020-06-30", ("AAA", "CCC")),
        ("2021-03-31", ("AAA",)),
    ]

    def test_one_event_per_ticker_year_at_the_earliest_date_it_was_held(self):
        found = llm_sample.events(self.schedule)

        self.assertEqual(
            {(e.ticker, e.year, e.as_of) for e in found},
            {
                ("AAA", 2020, "2020-03-31"),
                ("BBB", 2020, "2020-03-31"),
                ("CCC", 2020, "2020-06-30"),
                ("AAA", 2021, "2021-03-31"),
            },
        )

    def test_the_as_of_date_never_postdates_the_first_holding(self):
        # The filing is fetched at this date, so a later one would read a 10-K
        # the decision could not have seen.
        by_key = {(e.ticker, e.year): e.as_of for e in llm_sample.events(self.schedule)}

        self.assertEqual(by_key[("AAA", 2020)], "2020-03-31")

    def test_a_sample_smaller_than_the_population_is_seeded_and_repeatable(self):
        found = llm_sample.events(self.schedule)

        first = llm_sample.sample(found, 2, seed=7)
        again = llm_sample.sample(found, 2, seed=7)

        self.assertEqual(len(first), 2)
        self.assertEqual(first, again)

    def test_sampling_zero_or_more_than_exists_takes_everything(self):
        found = llm_sample.events(self.schedule)

        self.assertEqual(llm_sample.sample(found, 0), found)
        self.assertEqual(llm_sample.sample(found, 99), found)


class VetoTest(unittest.TestCase):
    schedule = [
        ("2020-03-31", ("AAA", "BBB")),
        ("2020-06-30", ("AAA", "BBB")),
        ("2021-03-31", ("AAA", "BBB")),
    ]

    def test_only_avoid_is_vetoed_by_default(self):
        events = llm_sample.events(self.schedule)
        by_name = {(e.ticker, e.year): e for e in events}
        assessments = {
            by_name[("AAA", 2020)]: assessment("AAA", Verdict.AVOID),
            by_name[("BBB", 2020)]: assessment("BBB", Verdict.CAUTION),
        }

        self.assertEqual(
            llm_sample.vetoed(assessments, "avoid"), frozenset({by_name[("AAA", 2020)]})
        )
        self.assertEqual(len(llm_sample.vetoed(assessments, "avoid+caution")), 2)

    def test_a_veto_removes_the_name_from_every_date_in_that_year(self):
        # The verdict is about a filing, and the filing does not change between
        # quarters — vetoing only the entry would re-buy on unread-since evidence.
        events = llm_sample.events(self.schedule)
        veto = frozenset(e for e in events if e.ticker == "AAA" and e.year == 2020)

        filtered = llm_sample.apply_veto(self.schedule, veto)

        self.assertEqual(filtered[0], ("2020-03-31", ("BBB",)))
        self.assertEqual(filtered[1], ("2020-06-30", ("BBB",)))

    def test_a_veto_does_not_reach_into_the_next_year(self):
        events = llm_sample.events(self.schedule)
        veto = frozenset(e for e in events if e.ticker == "AAA" and e.year == 2020)

        filtered = llm_sample.apply_veto(self.schedule, veto)

        self.assertEqual(filtered[2], ("2021-03-31", ("AAA", "BBB")))

    def test_book_cut_is_the_share_of_held_slots_removed(self):
        filtered = [(day, names[1:]) for day, names in self.schedule]

        self.assertAlmostEqual(llm_sample.book_cut(self.schedule, filtered), 0.5)

    def test_a_random_veto_of_the_full_population_takes_all_of_it(self):
        events = llm_sample.events(self.schedule)

        drawn = llm_sample.random_veto(events, 99, random.Random(0))

        self.assertEqual(drawn, frozenset(events))
        self.assertEqual(llm_sample.random_veto(events, 0, random.Random(0)), frozenset())


class AssessEventsTest(unittest.TestCase):
    """Failures are named and left unvetoed; a breached budget is not a failure."""

    def setUp(self):
        from tradingagents.value.store import db

        self.conn = db.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute(
            "INSERT INTO facts VALUES (?,?,?,?,?,?,?,?,?,?)",
            (1234567, "AAA", "NetIncome", 2020, "2020-12-31", "2021-02-10",
             1.0, "USD", "Tag", "acc-1"),
        )
        self.event = llm_sample.Event("AAA", 2020, "2020-03-31")
        self.absent = llm_sample.Event("ZZZ", 2020, "2020-03-31")
        self.summaries = {self.event: "numbers", self.absent: "numbers"}

    def _run(self, events, assess=None, sections_for=None):
        return llm_sample.assess_events(
            events,
            self.summaries,
            conn=self.conn,
            client=object(),
            sections_for=sections_for or (lambda client, cik, as_of: (None, SECTIONS)),
            assess=assess or (lambda *a, **k: assessment("AAA")),
        )

    def test_an_assessed_event_comes_back_typed(self):
        assessments, failures = self._run([self.event])

        self.assertEqual(assessments[self.event].verdict, Verdict.PROCEED)
        self.assertEqual(failures, {})

    def test_the_filing_is_fetched_as_of_the_events_date(self):
        seen = {}

        def sections_for(client, cik, as_of):
            seen["cik"], seen["as_of"] = cik, as_of
            return None, SECTIONS

        self._run([self.event], sections_for=sections_for)

        self.assertEqual(seen, {"cik": 1234567, "as_of": "2020-03-31"})

    def test_a_ticker_the_store_has_no_cik_for_is_a_named_failure(self):
        assessments, failures = self._run([self.absent])

        self.assertEqual(assessments, {})
        self.assertIn("no CIK", failures[self.absent])

    def test_a_missing_filing_is_a_named_failure_and_leaves_the_name_in_the_book(self):
        def sections_for(client, cik, as_of):
            raise filings.FilingNotFound("no 10-K on or before 2020-03-31")

        assessments, failures = self._run([self.event], sections_for=sections_for)

        self.assertEqual(assessments, {})
        self.assertIn("FilingNotFound", failures[self.event])
        self.assertEqual(llm_sample.vetoed(assessments, "avoid"), frozenset())

    def test_a_breached_budget_aborts_rather_than_being_recorded_as_a_failure(self):
        def assess(*a, **k):
            raise BudgetExceeded("Run budget exceeded: $2.01 > $2.00")

        with self.assertRaises(BudgetExceeded):
            self._run([self.event], assess=assess)


class SummaryTest(unittest.TestCase):
    def test_the_prompt_summary_carries_the_screen_result_at_that_date(self):
        snap = numeric.Snapshot(
            as_of="2020-03-31",
            screened=200,
            passed=7,
            valued=(("AAA", 0.31),),
            quality=(("AAA", 0.92),),
        )

        text = llm_sample.numeric_summary(snap, "AAA")

        self.assertIn("2020-03-31", text)
        self.assertIn("+31.0%", text)
        self.assertIn("92%", text)
        self.assertIn("7 of 200", text)


class MeasurementTest(unittest.TestCase):
    """The paired comparison and the floor it has to clear."""

    def setUp(self):
        self.dates = [day for day, _ in curve([0] * 12)]
        self.baseline = result([100, 105, 110, 116, 121, 127, 134, 140, 147, 155, 162, 170])

    def test_a_better_filtered_book_reads_as_a_positive_excess(self):
        better = result([100, 107, 114, 122, 131, 140, 150, 160, 172, 184, 197, 210])

        excess = llm_sample.paired_excess(
            self.baseline, better, self.dates, samples=200, seed=1
        )

        self.assertIsNotNone(excess)
        self.assertGreater(excess.point, 0.0)

    def test_an_identical_book_has_no_excess(self):
        excess = llm_sample.paired_excess(
            self.baseline, self.baseline, self.dates, samples=200, seed=1
        )

        self.assertAlmostEqual(excess.point, 0.0, places=9)

    def test_the_noise_floor_is_measured_from_random_vetoes(self):
        schedule = [(day, ("AAA", "BBB")) for day in self.dates]
        events = llm_sample.events(schedule)
        # A simulator whose outcome depends on how much was vetoed, so the floor
        # has something to resolve.
        def simulate(sched):
            held = sum(len(names) for _, names in sched)
            return result([100 * (1 + 0.05 * i) ** (held / 24) for i in range(12)])

        floor = llm_sample.noise_floor(
            self.baseline, schedule, events, 2, simulate, self.dates,
            reps=3, samples=200, seed=0,
        )

        self.assertIsNotNone(floor)
        point, high = floor
        self.assertGreaterEqual(high, point)


class JudgeTest(unittest.TestCase):
    """The criterion, applied where phase 4 kept being talked out of it."""

    def test_an_effect_that_clears_zero_and_the_floor_passes(self):
        excess = stats.Interval(point=0.06, low=0.02, high=0.10)

        verdict = llm_sample.judge(excess, (0.001, 0.02))

        self.assertTrue(verdict.passes)
        self.assertEqual(verdict.reasons, ())

    def test_an_interval_containing_zero_fails(self):
        excess = stats.Interval(point=0.06, low=-0.02, high=0.14)

        verdict = llm_sample.judge(excess, (0.0, 0.01))

        self.assertFalse(verdict.passes)
        self.assertIn("contains zero", verdict.reasons[0])

    def test_an_effect_inside_the_noise_floor_fails_even_when_it_clears_zero(self):
        excess = stats.Interval(point=0.02, low=0.005, high=0.04)

        verdict = llm_sample.judge(excess, (0.0, 0.03))

        self.assertFalse(verdict.passes)
        self.assertTrue(any("noise floor" in reason for reason in verdict.reasons))

    def test_no_floor_means_no_pass(self):
        excess = stats.Interval(point=0.06, low=0.02, high=0.10)

        verdict = llm_sample.judge(excess, None)

        self.assertFalse(verdict.passes)

    def test_no_interval_means_no_verdict(self):
        verdict = llm_sample.judge(None, (0.0, 0.01))

        self.assertFalse(verdict.passes)
        self.assertIn("no verdict", verdict.reasons[0])


class ReportTest(unittest.TestCase):
    def _report(self, **overrides):
        event = llm_sample.Event("AAA", 2020, "2020-03-31")
        base = {
            "baseline": result([100, 110, 120]),
            "filtered": llm_sample.Arm(
                "filtered",
                result([100, 112, 125]),
                stats.Interval(point=0.02, low=-0.01, high=0.05),
            ),
            "floor": (0.0, 0.03),
            "verdict": stats.Verdict(passes=False, reasons=("the effect is inside the floor",)),
            "all_events": (event, llm_sample.Event("BBB", 2020, "2020-03-31")),
            "chosen": (event,),
            "assessments": {event: assessment("AAA", Verdict.AVOID)},
            "failures": {},
            "veto": frozenset({event}),
            "cut": 0.25,
            "spend_usd": 1.37,
            "start": "2020-01-01",
            "end": "2021-01-01",
            "setting": "the configured trigger 30%",
            "exit_rule": "rebalance",
            "reps": 10,
            "seed": 0,
        }
        base.update(overrides)
        return "\n".join(llm_sample.report(**base))

    def test_the_report_prints_the_cost_the_question_is_about(self):
        self.assertIn("$1.37", self._report())

    def test_the_report_prints_the_criterion_and_the_verdict(self):
        text = self._report()

        self.assertIn("pre-registered pass criterion", text)
        self.assertIn("does not earn its cost", text)

    def test_partial_coverage_is_stated_as_a_caveat(self):
        self.assertIn("coverage: 50%", self._report())

    def test_the_quality_exit_carries_the_leverage_warning(self):
        self.assertIn("LEVERAGE", self._report(exit_rule="quality"))
        self.assertNotIn("LEVERAGE", self._report(exit_rule="rebalance"))

    def test_failures_are_listed_by_name(self):
        event = llm_sample.Event("CCC", 2020, "2020-03-31")

        text = self._report(failures={event: "FilingNotFound: no 10-K"})

        self.assertIn("CCC", text)
        self.assertIn("left unvetoed", text)


class CommandLineTest(unittest.TestCase):
    """The step-5 lesson: the suite covered every function but not the entry point."""

    def test_the_parser_builds(self):
        with self.assertRaises(SystemExit):
            llm_sample.main(["--help"])

    def test_a_window_with_no_quarter_end_is_refused(self):
        self.assertEqual(llm_sample.main(["--start", "2024-01-01", "--end", "2024-01-05"]), 2)


if __name__ == "__main__":
    unittest.main()
