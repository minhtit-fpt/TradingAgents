"""D7 -- the filing read attached to the D5 basket.

Two properties carry the phase. The read must not be able to change the list,
because phase 6 measured what an LLM gate at entry is worth; and a filing that
cannot be reached or parsed must produce a note rather than a substituted
verdict, because an invented read is worse than an absent one.
"""

import unittest

from tradingagents.value.analyst.schemas import (
    Concentration,
    Confidence,
    Moat,
    MoatTrend,
    ValueAssessment,
    Verdict,
)
from tradingagents.value.dividend import brief, stability
from tradingagents.value.edgar import filings
from tradingagents.value.screen.criteria import CriterionResult, ScreenResult

AS_OF = "2026-01-02"


def assessment(ticker: str = "PG", verdict: Verdict = Verdict.AVOID) -> ValueAssessment:
    return ValueAssessment(
        ticker=ticker,
        verdict=verdict,
        moat=Moat.WIDE,
        moat_trend=MoatTrend.ERODING,
        customer_concentration=Concentration.SEVERE,
        accounting_flags=["receivables growing against flat revenue"],
        key_risks=["two retailers carry a third of revenue"],
        evidence_gaps=["segment margins not broken out"],
        thesis="Brand still prices, distribution is narrowing.",
        confidence=Confidence.MEDIUM,
    )


YEARS = tuple(range(2016, 2026))


def criterion(number, name, passed=True, violations=(), missing=(), values=()):
    return CriterionResult(
        number=number,
        name=name,
        passed=passed,
        violation_years=tuple(violations),
        missing_years=tuple(missing),
        values=tuple(values),
    )


def screen_result(passed: bool = True) -> ScreenResult:
    criteria = (
        criterion(1, "PaidEveryYear", values=((2025, 3.76),)),
        criterion(2, "DividendNeverCut"),
        criterion(3, "PayoutRatio", passed=passed, violations=() if passed else (2020, 2021)),
        criterion(4, "FreeCashFlowCovers", missing=(2016,)),
    )
    return ScreenResult(
        passed=passed, criteria=criteria, years=YEARS, years_required=len(YEARS)
    )


class Outcome:
    """A ``dividend.runner.Outcome`` stand-in with only what the brief reads."""

    def __init__(self, ticker="PG", result=None, latest_dps=3.76):
        self.ticker = ticker
        self.result = result if result is not None else screen_result()
        self.latest_dps = latest_dps


def selection(tickers=("PG", "KO"), yields=None) -> stability.Selection:
    rows = [
        stability.Stability(
            ticker=ticker, volatility=0.18, max_drawdown=-0.24, annual_return=0.09
        )
        for ticker in tickers
    ]
    return stability.Selection(
        chosen=rows,
        yields=yields if yields is not None else dict.fromkeys(tickers, 0.025),
        book=stability.Stability("BASKET", 0.15, -0.21, 0.10),
        cuts=stability.Cuts(unyielding=40, volatile=20, deep=5),
        universe=100,
        dropped=3,
        window=("2016-01-02", AS_OF),
        passes={t: Outcome(t) for t in tickers},
    )


class Sections:
    """A ``filings.Sections`` stand-in. ``missing``/``suspect`` drive the render."""

    def __init__(self, missing=(), suspect=()):
        self.business = "Item 1 text"
        self.risk_factors = "Item 1A text"
        self.mdna = "Item 7 text"
        self.dropped = ()
        self.suspect = suspect
        self.missing = missing


class Filing:
    def __init__(self):
        self.filed = "2025-08-01"
        self.accession = "0000080424-25-000050"
        self.url = "https://sec.gov/x.htm"


class NumericSummaryTest(unittest.TestCase):
    def test_it_names_the_dividend_screen_rather_than_the_business_screen(self):
        summary = brief.numeric_summary(
            selection().chosen[0], Outcome(), 0.0243
        )
        self.assertIn("DIVIDEND screen, not the thirteen-criterion business", summary)

    def test_every_criterion_is_named_with_its_bad_years(self):
        summary = brief.numeric_summary(
            selection().chosen[0], Outcome(result=screen_result(passed=False)), 0.02
        )
        self.assertIn("PayoutRatio: FAIL (violated 2020, 2021)", summary)
        self.assertIn("FreeCashFlowCovers: pass (no data 2016)", summary)

    def test_an_unknown_yield_says_unknown_rather_than_zero(self):
        summary = brief.numeric_summary(selection().chosen[0], Outcome(), None)
        self.assertIn("Forward yield at today's price: unknown", summary)
        self.assertNotIn("0.00%", summary)


class ReadOneTest(unittest.TestCase):
    """Every failure path returns a note. None of them returns a verdict."""

    def setUp(self):
        self.row = selection().chosen[0]
        self.outcome = Outcome()

    def _read(self, conn, **kwargs):
        return brief.read_one(conn, self.row, self.outcome, 0.025, AS_OF, **kwargs)

    def test_a_name_with_no_cik_is_reported_not_assessed(self):
        with self._store() as conn:
            read = self._read(conn)
        self.assertIsNone(read.assessment)
        self.assertIn("no CIK on file", read.note)

    def test_an_unreachable_filing_is_reported_not_assessed(self):
        def boom(*args, **kwargs):
            raise filings.FilingNotFound("no 10-K on or before 2026-01-02")

        with self._store(cik=80424) as conn, _patched(filings, "sections_for", boom):
            read = self._read(conn, client=object())
        self.assertIsNone(read.assessment)
        self.assertIn("filing not read", read.note)

    def test_an_unparsed_answer_is_reported_not_substituted(self):
        from tradingagents.value.analyst import value_analyst

        def boom(*args, **kwargs):
            raise value_analyst.ValueAnalystError("no tool call")

        with (
            self._store(cik=80424) as conn,
            _patched(filings, "sections_for", lambda *a, **k: (Filing(), Sections())),
            _patched(value_analyst, "assess", boom),
        ):
            read = self._read(conn, client=object())
        self.assertIsNone(read.assessment)
        self.assertIn("filing not assessed", read.note)
        self.assertEqual(read.label, "10-K filed 2025-08-01 (accession 0000080424-25-000050)")

    def test_a_suspect_extraction_travels_out_on_the_note(self):
        from tradingagents.value.analyst import value_analyst

        sections = Sections(suspect=("Item 7 span overlaps Item 8",))
        with (
            self._store(cik=80424) as conn,
            _patched(filings, "sections_for", lambda *a, **k: (Filing(), sections)),
            _patched(value_analyst, "assess", lambda *a, **k: assessment()),
        ):
            read = self._read(conn, client=object())
        self.assertIsNotNone(read.assessment)
        self.assertIn("extraction suspect", read.note)

    def test_the_prompt_the_analyst_receives_carries_the_dividend_criteria(self):
        from tradingagents.value.analyst import value_analyst

        seen = {}

        def capture(ticker, sections, summary, **kwargs):
            seen["summary"] = summary
            return assessment()

        with (
            self._store(cik=80424) as conn,
            _patched(filings, "sections_for", lambda *a, **k: (Filing(), Sections())),
            _patched(value_analyst, "assess", capture),
        ):
            self._read(conn, client=object())
        self.assertIn("PaidEveryYear", seen["summary"])
        self.assertIn("Trailing", seen["summary"])

    def _store(self, cik=None):
        from tradingagents.value.store import db

        class Conn:
            def __enter__(inner):
                inner.conn = db.connect(":memory:")
                if cik is not None:
                    _seed_cik(inner.conn, "PG", cik)
                return inner.conn

            def __exit__(inner, *exc):
                inner.conn.close()
                return False

        return Conn()


class RenderTest(unittest.TestCase):
    def test_the_verdict_renders_last_under_each_name(self):
        picked = selection(("PG",))
        lines = brief.render(picked, [brief.Read("PG", assessment())], AS_OF)
        body = [line.strip() for line in lines if line.strip()]
        verdict_at = next(i for i, line in enumerate(body) if line.startswith("verdict:"))
        for field in ("accounting flags:", "key risks:", "evidence gaps:", "thesis:"):
            self.assertLess(
                next(i for i, line in enumerate(body) if line.startswith(field)),
                verdict_at,
                f"{field} must render above the verdict",
            )

    def test_an_avoid_verdict_does_not_remove_the_name_from_the_list(self):
        """The whole point of D7. Phase 6 closed the gate; this is the briefing."""
        picked = selection(("PG", "KO"))
        reads = [
            brief.Read("PG", assessment("PG", Verdict.AVOID)),
            brief.Read("KO", assessment("KO", Verdict.PROCEED)),
        ]
        lines = brief.render(picked, reads, AS_OF)
        text = "\n".join(lines)
        self.assertIn("PG ", text)
        self.assertIn("KO ", text)
        self.assertIn("2 names from D5", text)
        self.assertIn("not a gate", text)

    def test_the_order_of_the_names_is_d5s_order_not_the_verdicts(self):
        picked = selection(("PG", "KO"))
        reads = [
            brief.Read("KO", assessment("KO", Verdict.PROCEED)),
            brief.Read("PG", assessment("PG", Verdict.AVOID)),
        ]
        lines = brief.render(picked, reads, AS_OF)
        names = [line.split()[0] for line in lines if line and not line.startswith(" ")]
        self.assertLess(names.index("PG"), names.index("KO"))

    def test_a_missing_section_is_shown_beside_the_read_it_weakened(self):
        picked = selection(("PG",))
        read = brief.Read("PG", assessment(), missing_sections=("mdna",))
        lines = brief.render(picked, [read], AS_OF)
        self.assertIn("sections extraction could not find: mdna", "\n".join(lines))

    def test_an_unknown_yield_renders_as_na_rather_than_zero(self):
        picked = selection(("PG",), yields={"PG": None})
        lines = brief.render(picked, [brief.Read("PG", assessment())], AS_OF)
        self.assertIn("n/a", lines[2])
        self.assertNotIn("0.00%", "\n".join(lines))


class RunTest(unittest.TestCase):
    def test_a_dry_run_names_the_filings_and_spends_nothing(self):
        from tradingagents.value.store import db

        conn = db.connect(":memory:")
        try:
            with _patched(stability, "selection", lambda *a, **k: selection()):
                lines = brief.run(conn, AS_OF, dry_run=True)
        finally:
            conn.close()
        self.assertIn("dry run, nothing spent", lines[0])
        self.assertIn("would read 2 filings: PG, KO", lines[1])

    def test_tickers_narrows_the_read_to_a_subset_of_the_basket(self):
        from tradingagents.value.store import db

        conn = db.connect(":memory:")
        try:
            with _patched(stability, "selection", lambda *a, **k: selection()):
                lines = brief.run(conn, AS_OF, tickers=["ko"], dry_run=True)
        finally:
            conn.close()
        self.assertIn("would read 1 filings: KO", lines[1])

    def test_a_ticker_outside_the_basket_reads_nothing_rather_than_screening_it(self):
        """The read follows D5's list. It is not a way to ask about any name."""
        from tradingagents.value.store import db

        conn = db.connect(":memory:")
        try:
            with _patched(stability, "selection", lambda *a, **k: selection()):
                lines = brief.run(conn, AS_OF, tickers=["MSFT"], dry_run=True)
        finally:
            conn.close()
        self.assertIn("would read 0 filings: none", lines[1])

    def test_the_read_runs_over_the_basket_not_the_pass_list(self):
        """Cost control, asserted: 2 chosen names out of a 100-name universe."""
        from tradingagents.value.analyst import value_analyst
        from tradingagents.value.store import db

        conn = db.connect(":memory:")
        _seed_cik(conn, "PG", 80424)
        _seed_cik(conn, "KO", 21344)
        calls = []
        try:
            with (
                _patched(stability, "selection", lambda *a, **k: selection()),
                _patched(filings, "sections_for", lambda *a, **k: (Filing(), Sections())),
                _patched(
                    value_analyst,
                    "assess",
                    lambda ticker, *a, **k: calls.append(ticker) or assessment(ticker),
                ),
            ):
                brief.run(conn, AS_OF, client=object())
        finally:
            conn.close()
        self.assertEqual(calls, ["PG", "KO"])


class _patched:
    """Minimal attribute patch; the suite avoids ``unittest.mock`` elsewhere."""

    def __init__(self, module, name, value):
        self.module, self.name, self.value = module, name, value

    def __enter__(self):
        self.original = getattr(self.module, self.name)
        setattr(self.module, self.name, self.value)
        return self.value

    def __exit__(self, *exc):
        setattr(self.module, self.name, self.original)
        return False


def _seed_cik(conn, ticker: str, cik: int) -> None:
    """The store is the only place a CIK comes from, so one fact is enough."""
    from tradingagents.value.store import db

    from .factories import facts_for

    db.upsert_facts(conn, ticker, cik, facts_for({2024: {"Revenue": 1.0}}))


if __name__ == "__main__":
    unittest.main()
