"""The dividend screen: window arithmetic, the four criteria, the runner, independence."""

import ast
import unittest
from pathlib import Path

from tradingagents.value.dividend import criteria, history, runner, store
from tradingagents.value.store import db

from .factories import decade

FIRST_YEAR = 2015
YEARS = 10
AS_OF = "2025-06-30"
# 2015..2024 — the ten calendar years fully elapsed on AS_OF.
WINDOW = history.window_years(AS_OF, YEARS)


def rising_dps(start: float = 1.0, growth: float = 0.06) -> dict[int, float]:
    return {year: start * (1 + growth) ** index for index, year in enumerate(WINDOW)}


def payments(dps: dict[int, float]) -> list[tuple[str, float]]:
    """One annual payment per year, mid-year, as the cache stores them."""
    return [(f"{year}-06-15", amount) for year, amount in sorted(dps.items())]


class WindowTest(unittest.TestCase):
    def test_window_excludes_the_year_in_progress(self):
        self.assertEqual(history.window_years("2025-06-30", 10), tuple(range(2015, 2025)))

    def test_window_excludes_the_current_year_even_on_new_years_eve(self):
        # 2025 is complete in every ordinary sense on 2025-12-31, but a payer
        # whose ex-date falls in the first week of January has not paid yet.
        self.assertEqual(history.window_years("2025-12-31", 3), (2022, 2023, 2024))


class AnnualTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_quarterly_payments_sum_into_their_calendar_year(self):
        store.upsert(
            self.conn,
            "PG",
            [(f"2024-{month:02d}-15", 0.25) for month in (2, 5, 8, 11)],
            "2025-01-01T00:00:00+00:00",
        )
        totals = history.annual(self.conn, "PG", AS_OF, WINDOW)
        self.assertAlmostEqual(totals[2024], 1.0)

    def test_a_year_with_no_payment_is_zero_not_absent(self):
        store.upsert(self.conn, "PG", [("2016-06-15", 1.0)], "2025-01-01T00:00:00+00:00")
        totals = history.annual(self.conn, "PG", AS_OF, WINDOW)
        self.assertEqual(sorted(totals), sorted(WINDOW))
        self.assertEqual(totals[2017], 0.0)

    def test_a_dividend_after_the_as_of_date_does_not_leak_in(self):
        store.upsert(
            self.conn,
            "PG",
            [("2024-06-15", 1.0), ("2024-12-15", 5.0)],
            "2025-01-01T00:00:00+00:00",
        )
        totals = history.annual(self.conn, "PG", "2024-07-01", history.window_years("2024-07-01"))
        self.assertAlmostEqual(totals[2023], 0.0)
        self.assertNotIn(2024, totals)

    def test_refresh_caches_what_the_fetcher_returns(self):
        rows = [("2023-06-15", 1.0), ("2024-06-15", 1.1)]
        cached = history.refresh(self.conn, "PG", fetcher=lambda ticker: rows,
                                 now="2025-01-01T00:00:00+00:00")
        self.assertEqual(cached, 2)
        self.assertEqual(len(store.as_of(self.conn, "PG", AS_OF)), 2)


class CriteriaTest(unittest.TestCase):
    def setUp(self):
        self.dps = rising_dps()
        self.financials = decade(FIRST_YEAR, YEARS)

    def evaluate(self, dps=None, financials=None):
        return criteria.evaluate(
            dps if dps is not None else self.dps,
            financials if financials is not None else self.financials,
            years_required=YEARS,
        )

    def named(self, result, name):
        return next(c for c in result.criteria if c.name == name)

    def test_a_clean_decade_of_rising_dividends_passes(self):
        result = self.evaluate()
        self.assertTrue(result.passed, result.failed_criteria)
        self.assertEqual(result.quality, 1.0)

    def test_a_single_cut_fails_even_though_the_level_is_high(self):
        dps = dict(self.dps)
        dps[2020] = dps[2019] * 0.5
        result = self.evaluate(dps=dps)
        self.assertFalse(result.passed)
        self.assertIn("DividendNeverCut", result.failed_criteria)
        self.assertEqual(self.named(result, "DividendNeverCut").violation_years, (2020,))

    def test_holding_the_dividend_flat_is_not_a_cut(self):
        dps = dict(self.dps)
        dps[2020] = dps[2019]
        dps[2021] = dps[2019]
        self.assertTrue(self.evaluate(dps=dps).passed)

    def test_a_skipped_year_fails_regardless_of_tolerance(self):
        dps = dict(self.dps)
        dps[2018] = 0.0
        result = criteria.evaluate(dps, self.financials, years_required=YEARS, tolerance=5)
        self.assertFalse(result.passed)
        self.assertIn("PaidEveryYear", result.failed_criteria)

    def test_a_payout_above_the_limit_across_the_decade_fails(self):
        financials = {
            year: {**facts, "DividendsPaid": facts["NetIncome"] * 0.9}
            for year, facts in self.financials.items()
        }
        result = self.evaluate(financials=financials)
        self.assertFalse(result.passed)
        self.assertIn("PayoutRatio", result.failed_criteria)

    def test_two_bad_payout_years_are_tolerated_but_three_are_not(self):
        def with_bad(years):
            return {
                year: ({**facts, "DividendsPaid": facts["NetIncome"] * 0.9}
                       if year in years else facts)
                for year, facts in self.financials.items()
            }

        self.assertTrue(self.evaluate(financials=with_bad({2016, 2017})).passed)
        self.assertFalse(self.evaluate(financials=with_bad({2016, 2017, 2018})).passed)

    def test_a_loss_year_is_a_payout_violation_not_missing_data(self):
        financials = dict(self.financials)
        for year in (2018, 2019, 2020):
            financials[year] = {**financials[year], "NetIncome": -10.0}
        result = self.evaluate(financials=financials)
        payout = self.named(result, "PayoutRatio")
        self.assertEqual(payout.violation_years, (2018, 2019, 2020))
        self.assertEqual(payout.missing_years, ())

    def test_a_dividend_free_cash_flow_cannot_cover_fails(self):
        financials = {
            year: {**facts, "DividendsPaid": (facts["OperatingCashFlow"] - facts["Capex"]) * 1.2}
            for year, facts in self.financials.items()
        }
        result = self.evaluate(financials=financials)
        self.assertFalse(result.passed)
        self.assertIn("FreeCashFlowCover", result.failed_criteria)

    def test_a_missing_concept_counts_against_the_year_like_a_violation(self):
        financials = dict(self.financials)
        for year in (2016, 2017, 2018):
            financials[year] = {k: v for k, v in financials[year].items() if k != "OperatingCashFlow"}
        result = self.evaluate(financials=financials)
        self.assertFalse(result.passed)
        self.assertEqual(self.named(result, "FreeCashFlowCover").missing_years, (2016, 2017, 2018))

    def test_a_short_history_cannot_pass(self):
        dps = {year: value for year, value in self.dps.items() if year >= 2020}
        financials = {y: f for y, f in self.financials.items() if y >= 2020}
        result = criteria.evaluate(dps, financials, years_required=YEARS)
        self.assertFalse(result.passed)
        self.assertLess(result.quality, 1.0)


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)
        store.upsert(self.conn, "PG", payments(rising_dps()), "2025-01-01T00:00:00+00:00")

    def store_financials(self, ticker="PG", financials=None):
        from tradingagents.value.edgar.concepts import Fact

        for year, facts in (financials or decade(FIRST_YEAR, YEARS)).items():
            db.upsert_facts(self.conn, ticker, 1, [
                Fact(concept=concept, fiscal_year=year, period_end=f"{year}-12-31",
                     filed=f"{year + 1}-02-15", value=value, unit="USD",
                     source_tag=concept, accn=f"{ticker}-{year}")
                for concept, value in facts.items()
            ])

    def test_a_clean_payer_with_facts_passes_offline(self):
        self.store_financials()
        outcome = runner.screen_one(self.conn, "PG", AS_OF, offline=True, years=YEARS)
        self.assertIsNone(outcome.error)
        self.assertTrue(outcome.result.passed)

    def test_a_name_with_no_facts_reports_an_error_rather_than_a_verdict(self):
        outcome = runner.screen_one(self.conn, "PG", AS_OF, offline=True, years=YEARS)
        self.assertIsNone(outcome.result)
        self.assertIn("no 10-K facts", outcome.error)

    def test_a_name_with_no_dividends_reports_an_error_rather_than_a_verdict(self):
        self.store_financials(ticker="XYZ")
        outcome = runner.screen_one(self.conn, "XYZ", AS_OF, offline=True, years=YEARS)
        self.assertIsNone(outcome.result)
        self.assertIn("no dividends", outcome.error)

    def test_render_explains_a_rejection_by_naming_the_failed_criteria(self):
        cut = dict(rising_dps())
        cut[2020] = 0.1
        store.upsert(self.conn, "KO", payments(cut), "2025-01-01T00:00:00+00:00")
        self.store_financials(ticker="KO")
        self.store_financials(ticker="PG")

        outcomes = runner.screen(self.conn, ["KO", "PG"], AS_OF, offline=True, years=YEARS)
        # Passing names rank ahead of failing ones.
        self.assertEqual(outcomes[0].ticker, "PG")
        text = "\n".join(runner.render(outcomes, AS_OF))
        self.assertIn("1 of 2 pass", text)
        self.assertIn("DividendNeverCut: failed 2020", text)


if __name__ == "__main__":
    unittest.main()


class IndependenceTest(unittest.TestCase):
    """The feature is a directory you can delete, and the import graph proves it.

    Reviewing that by eye does not survive a year of edits, so it is asserted the
    same way ``test_isolation.py`` asserts the module's own contract.
    """

    # What the dividend screen is allowed to reach for inside ``value``. Each is
    # read-only reuse: knobs that must not have two parsing rules, the fact store
    # it screens over, and the two result dataclasses the business screen already
    # defines. Anything else belongs in this package.
    OUTWARD_ALLOWLIST = {
        "tradingagents.value.config",
        "tradingagents.value.store.db",
        "tradingagents.value.screen.criteria",
        "tradingagents.value.screen.market",
        "tradingagents.value.alerts.telegram",
    }

    def setUp(self):
        import tradingagents.value as value_pkg
        import tradingagents.value.dividend as dividend_pkg

        self.value_root = Path(value_pkg.__file__).parent
        self.dividend_root = Path(dividend_pkg.__file__).parent

    def _imports(self, path: Path, package: str) -> set[str]:
        """Absolute module names imported by ``path``, relative ones resolved."""
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    if node.module:
                        found.add(node.module)
                    continue
                base = package.rsplit(".", node.level - 1)[0] if node.level > 1 else package
                resolved = f"{base}.{node.module}" if node.module else base
                # ``from ..store import db`` names a module, not an attribute, so
                # resolve it the rest of the way — otherwise the allowlist would
                # have to admit whole packages to permit one file in them.
                submodules = {
                    module
                    for module in (self._submodule(resolved, alias.name) for alias in node.names)
                    if module
                }
                found.update(submodules or {resolved})
        return found

    def _submodule(self, package: str, name: str) -> str | None:
        """``package.name`` when that is a module on disk, else ``None``."""
        prefix = "tradingagents.value."
        if not package.startswith(prefix):
            return None
        relative = package[len(prefix):].replace(".", "/")
        if (self.value_root / relative / f"{name}.py").exists():
            return f"{package}.{name}"
        return None

    def test_nothing_in_the_value_module_depends_on_the_dividend_screen(self):
        offenders = []
        for path in sorted(self.value_root.rglob("*.py")):
            if self.dividend_root in path.parents:
                continue
            package = "tradingagents.value." + ".".join(
                path.relative_to(self.value_root).parts[:-1]
            )
            for module in self._imports(path, package.rstrip(".")):
                if module.startswith("tradingagents.value.dividend"):
                    offenders.append(f"{path.name}: {module}")
        self.assertEqual(offenders, [], "the arrow must point outward only")

    def test_the_dividend_screen_reaches_outward_only_through_the_allowlist(self):
        offenders = []
        for path in sorted(self.dividend_root.rglob("*.py")):
            for module in self._imports(path, "tradingagents.value.dividend"):
                if not module.startswith("tradingagents."):
                    continue
                if module.startswith("tradingagents.value.dividend"):
                    continue
                if module in self.OUTWARD_ALLOWLIST:
                    continue
                offenders.append(f"{path.name}: {module}")
        self.assertEqual(offenders, [])

    def test_no_existing_value_file_was_edited_to_make_room_for_this(self):
        config = (self.value_root / "config.py").read_text(encoding="utf-8")
        self.assertNotIn("VALUE_DIVIDEND", config)

        schema = (self.value_root / "store" / "db.py").read_text(encoding="utf-8")
        self.assertNotIn("TABLE IF NOT EXISTS dividends", schema)


class WhyTest(unittest.TestCase):
    """A criterion nobody could evaluate must not read as one the business failed."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def result(self, financials):
        return criteria.evaluate(rising_dps(), financials, years_required=YEARS)

    def test_missing_inputs_are_labelled_no_data_not_failed(self):
        financials = {
            year: {k: v for k, v in facts.items() if k != "Capex"}
            for year, facts in decade(FIRST_YEAR, YEARS).items()
        }
        text = "\n".join(runner.render([runner.Outcome("ADP", result=self.result(financials))],
                                       AS_OF))
        self.assertIn("FreeCashFlowCover: no data", text)
        self.assertNotIn("FreeCashFlowCover: failed", text)

    def test_real_violations_are_still_labelled_failed(self):
        financials = {
            year: {**facts, "DividendsPaid": facts["NetIncome"] * 0.9}
            for year, facts in decade(FIRST_YEAR, YEARS).items()
        }
        text = "\n".join(runner.render([runner.Outcome("XYZ", result=self.result(financials))],
                                       AS_OF))
        self.assertIn("PayoutRatio: failed", text)
