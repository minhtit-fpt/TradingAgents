"""Budget cap: exceeding it aborts the run, it never silently continues."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from tradingagents.value.llm.budget import (
    Budget,
    BudgetError,
    BudgetExceeded,
    UnknownModelPrice,
    cost_usd,
)

MODEL = "deepseek-v4-flash"


class CostTest(unittest.TestCase):
    def test_cost_scales_with_tokens(self):
        one = cost_usd(MODEL, 1_000_000, 0)
        two = cost_usd(MODEL, 2_000_000, 0)

        self.assertGreater(one, 0.0)
        self.assertAlmostEqual(two, 2 * one)

    def test_unpriced_model_raises_instead_of_costing_nothing(self):
        with self.assertRaises(UnknownModelPrice):
            cost_usd("some-model-nobody-priced", 1000, 100)


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "llm_budget.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def _budget(self, run_cap=1.0, month_cap=1000.0):
        return Budget(ledger_path=self.ledger, run_cap_usd=run_cap, month_cap_usd=month_cap)

    def test_charge_under_the_cap_records_and_returns_cost(self):
        budget = self._budget()

        usd = budget.charge(MODEL, 1000, 200)

        self.assertGreater(usd, 0.0)
        self.assertAlmostEqual(budget.run_spend_usd, usd)
        self.assertAlmostEqual(budget.month_spend_usd(), usd)

    def test_run_cap_aborts(self):
        budget = self._budget(run_cap=0.0)

        with self.assertRaises(BudgetExceeded):
            budget.charge(MODEL, 1000, 200)

    def test_month_cap_counts_spend_from_earlier_runs(self):
        first = self._budget(run_cap=100.0, month_cap=100.0)
        spent = first.charge(MODEL, 1_000_000, 0)

        # A separate process, same ledger, a cap now below what the month holds.
        second = Budget(ledger_path=self.ledger, run_cap_usd=100.0, month_cap_usd=spent / 2)

        with self.assertRaises(BudgetExceeded):
            second.charge(MODEL, 1000, 100)

    def test_other_months_do_not_count_against_this_month(self):
        budget = self._budget()
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(
            json.dumps({"at": "1999-01-01T00:00:00Z", "model": MODEL, "usd": 999.0}) + "\n",
            encoding="utf-8",
        )

        current = datetime.now(timezone.utc).strftime("%Y-%m")

        self.assertEqual(budget.month_spend_usd(current), 0.0)

    def test_corrupt_ledger_raises_rather_than_reading_as_zero_spent(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("this is not json\n", encoding="utf-8")
        budget = self._budget()

        with self.assertRaises(BudgetError):
            budget.month_spend_usd()

    def test_the_charge_is_recorded_even_when_it_trips_the_cap(self):
        budget = self._budget(run_cap=0.0)

        with self.assertRaises(BudgetExceeded):
            budget.charge(MODEL, 1000, 200)

        # Money left the wallet, so the ledger must show it.
        self.assertGreater(Budget(ledger_path=self.ledger).month_spend_usd(), 0.0)


if __name__ == "__main__":
    unittest.main()
