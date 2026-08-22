"""Tag fallback chains: the same number, however the filer chose to label it."""

import unittest
from datetime import date, timedelta

from tradingagents.value.edgar.concepts import annual_facts, fiscal_year_for


def duration(end, val, filed, accn="acc-1", form="10-K", days=364):
    start = (date.fromisoformat(end) - timedelta(days=days)).isoformat()
    return {"start": start, "end": end, "val": val, "filed": filed, "accn": accn, "form": form}


def instant(end, val, filed, accn="acc-1", form="10-K"):
    return {"end": end, "val": val, "filed": filed, "accn": accn, "form": form}


def payload(**tags):
    """``payload(Revenues=("USD", [row, ...]))`` -> a companyfacts-shaped dict."""
    return {
        "facts": {
            "us-gaap": {tag: {"units": {unit: rows}} for tag, (unit, rows) in tags.items()}
        }
    }


def quarters(first_start, ends, vals, filed, accn="acc-1"):
    """Contiguous quarterly rows, as the quarterly note of a 10-K reports them."""
    rows = []
    start = first_start
    for end, val in zip(ends, vals, strict=True):
        rows.append({"start": start, "end": end, "val": val,
                     "filed": filed, "accn": accn, "form": "10-K"})
        start = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
    return rows


def values(facts, concept):
    return {f.fiscal_year: f.value for f in facts if f.concept == concept}


def tags_used(facts, concept):
    return {f.source_tag for f in facts if f.concept == concept}


class FiscalYearTest(unittest.TestCase):
    def test_calendar_year_end_keeps_its_year(self):
        self.assertEqual(fiscal_year_for(date(2024, 12, 31)), 2024)

    def test_september_year_end_keeps_its_year(self):
        self.assertEqual(fiscal_year_for(date(2024, 9, 28)), 2024)

    def test_january_year_end_belongs_to_the_previous_year(self):
        # A retailer closing 2025-01-31 calls that fiscal 2024.
        self.assertEqual(fiscal_year_for(date(2025, 1, 31)), 2024)


class FallbackChainTest(unittest.TestCase):
    def test_a_company_that_only_used_a_deprecated_tag_still_resolves(self):
        facts = annual_facts(payload(
            SalesRevenueNet=("USD", [duration("2014-12-31", 500.0, "2015-02-10")]),
        ))

        self.assertEqual(values(facts, "Revenue"), {2014: 500.0})
        self.assertEqual(tags_used(facts, "Revenue"), {"SalesRevenueNet"})

    def test_a_mid_history_tag_switch_resolves_on_both_sides(self):
        # What ASC 606 did to almost every issuer's revenue line.
        facts = annual_facts(payload(
            SalesRevenueNet=("USD", [duration("2016-12-31", 100.0, "2017-02-10")]),
            RevenueFromContractWithCustomerExcludingAssessedTax=(
                "USD", [duration("2019-12-31", 200.0, "2020-02-10")]),
        ))

        self.assertEqual(values(facts, "Revenue"), {2016: 100.0, 2019: 200.0})

    def test_the_earlier_tag_in_the_chain_wins_a_contested_year(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 111.0, "2021-02-10")]),
            RevenueFromContractWithCustomerExcludingAssessedTax=(
                "USD", [duration("2020-12-31", 999.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "Revenue"), {2020: 999.0})


class RowFilterTest(unittest.TestCase):
    def test_quarterly_rows_are_not_annual_facts(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 25.0, "2021-02-10", days=90)]),
        ))

        self.assertEqual(values(facts, "Revenue"), {})

    def test_non_10k_forms_are_ignored(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 25.0, "2021-02-10", form="10-Q")]),
        ))

        self.assertEqual(values(facts, "Revenue"), {})

    def test_10k_amendments_are_kept(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 25.0, "2021-06-10", form="10-K/A")]),
        ))

        self.assertEqual(values(facts, "Revenue"), {2020: 25.0})

    def test_a_balance_sheet_tag_does_not_take_a_duration_row(self):
        facts = annual_facts(payload(
            Assets=("USD", [duration("2020-12-31", 900.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "Assets"), {})

    def test_a_balance_sheet_tag_takes_an_instant_row(self):
        facts = annual_facts(payload(
            Assets=("USD", [instant("2020-12-31", 900.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "Assets"), {2020: 900.0})


class OpeningBalanceTest(unittest.TestCase):
    """A fiscal year has one closing balance, whatever else the filing carries."""

    def test_a_new_year_opening_balance_does_not_join_the_previous_year(self):
        # Arista's 10-K carries both, the second being the cumulative-effect
        # adjustment from adopting a new standard on the first day of 2019.
        facts = annual_facts(payload(
            RetainedEarningsAccumulatedDeficit=("USD", [
                instant("2018-12-31", 1190803.0, "2020-02-14", accn="acc-2020"),
                instant("2019-01-01", 1194505.0, "2020-02-14", accn="acc-2020"),
            ]),
        ))

        self.assertEqual(values(facts, "RetainedEarnings"), {2018: 1190803.0})

    def test_a_year_genuinely_ending_in_early_january_still_resolves(self):
        # A 52/53-week filer whose year ends on the Sunday nearest 31 December.
        facts = annual_facts(payload(
            Assets=("USD", [instant("2022-01-02", 900.0, "2022-02-20")]),
        ))

        self.assertEqual(values(facts, "Assets"), {2021: 900.0})

    def test_restatements_of_the_closing_balance_are_still_all_kept(self):
        facts = annual_facts(payload(
            StockholdersEquity=("USD", [
                instant("2020-12-31", 443164.0, "2021-03-01", accn="acc-2021"),
                instant("2020-12-31", 443164.0, "2022-02-28", accn="acc-2022"),
                instant("2021-01-01", 436736.0, "2024-02-26", accn="acc-2024"),
            ]),
        ))

        equity = [f for f in facts if f.concept == "Equity" and f.fiscal_year == 2020]

        self.assertEqual(sorted(f.filed for f in equity), ["2021-03-01", "2022-02-28"])


class NormalisationTest(unittest.TestCase):
    def test_capex_sign_is_normalised_to_a_magnitude(self):
        facts = annual_facts(payload(
            PaymentsToAcquirePropertyPlantAndEquipment=(
                "USD", [duration("2020-12-31", -40.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "Capex"), {2020: 40.0})

    def test_net_income_keeps_its_sign_because_a_loss_year_matters(self):
        facts = annual_facts(payload(
            NetIncomeLoss=("USD", [duration("2020-12-31", -12.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "NetIncome"), {2020: -12.0})


class DerivationTest(unittest.TestCase):
    def test_gross_profit_is_derived_when_the_tag_is_absent(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfRevenue=("USD", [duration("2020-12-31", 60.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "GrossProfit"), {2020: 40.0})
        self.assertEqual(tags_used(facts, "GrossProfit"), {"derived:Revenue-CostOfRevenue"})

    def test_a_reported_gross_profit_is_never_overwritten_by_a_derivation(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfRevenue=("USD", [duration("2020-12-31", 60.0, "2021-02-10")]),
            GrossProfit=("USD", [duration("2020-12-31", 41.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "GrossProfit"), {2020: 41.0})

    def test_a_derivation_never_mixes_two_different_filings(self):
        # Revenue from one filing minus cost from another is not a gross profit.
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10", accn="acc-1")]),
            CostOfRevenue=("USD", [duration("2020-12-31", 60.0, "2022-02-10", accn="acc-2")]),
        ))

        self.assertEqual(values(facts, "GrossProfit"), {})

    def test_sga_is_summed_from_its_components_when_the_combined_tag_is_absent(self):
        facts = annual_facts(payload(
            GeneralAndAdministrativeExpense=(
                "USD", [duration("2020-12-31", 30.0, "2021-02-10")]),
            SellingAndMarketingExpense=("USD", [duration("2020-12-31", 12.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "SGA"), {2020: 42.0})

    def test_a_selling_line_without_its_admin_half_is_not_reported_as_sga(self):
        facts = annual_facts(payload(
            SellingAndMarketingExpense=("USD", [duration("2020-12-31", 12.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "SGA"), {})

    def test_a_lone_admin_line_stands_in_for_sga(self):
        # REITs, banks and insurers report general and administrative expense
        # with no selling line at all. Requiring the pair left them with no SG&A
        # ratio and no way to pass criterion 3.
        facts = annual_facts(payload(
            GeneralAndAdministrativeExpense=(
                "USD", [duration("2020-12-31", 30.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "SGA"), {2020: 30.0})

    def test_the_pair_sum_beats_the_lone_admin_line(self):
        # Undercounting SG&A makes an expensive business look disciplined, so a
        # filer that splits the two lines must be summed, never read as G&A only.
        facts = annual_facts(payload(
            GeneralAndAdministrativeExpense=(
                "USD", [duration("2020-12-31", 30.0, "2021-02-10")]),
            SellingExpense=("USD", [duration("2020-12-31", 12.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "SGA"), {2020: 42.0})

    def test_a_reported_sga_is_never_replaced_by_a_component(self):
        facts = annual_facts(payload(
            SellingGeneralAndAdministrativeExpense=(
                "USD", [duration("2020-12-31", 50.0, "2021-02-10")]),
            GeneralAndAdministrativeExpense=(
                "USD", [duration("2020-12-31", 30.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "SGA"), {2020: 50.0})

    def test_cost_of_services_carries_a_service_business_gross_profit(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfServices=("USD", [duration("2020-12-31", 60.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "GrossProfit"), {2020: 40.0})

    def test_split_product_and_service_costs_are_summed(self):
        # Thermo Fisher, Northrop and Intuit report the two halves with no
        # combined total. Reading the goods half alone would put gross profit
        # 25 too high here, which is criterion 1 passing on a number the filing
        # does not contain.
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfGoodsSold=("USD", [duration("2020-12-31", 40.0, "2021-02-10")]),
            CostOfServices=("USD", [duration("2020-12-31", 25.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {2020: 65.0})
        self.assertEqual(values(facts, "GrossProfit"), {2020: 35.0})

    def test_a_goods_only_filer_still_resolves_its_cost_of_revenue(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfGoodsSold=("USD", [duration("2020-12-31", 40.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {2020: 40.0})

    def test_a_split_pair_is_never_mixed_across_two_filings(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10", accn="acc-1")]),
            CostOfGoodsSold=(
                "USD", [duration("2020-12-31", 40.0, "2021-02-10", accn="acc-1")]),
            CostOfServices=(
                "USD", [duration("2020-12-31", 25.0, "2022-02-10", accn="acc-2")]),
        ))

        # The goods half stands alone rather than being summed with a figure
        # from a different filing.
        self.assertEqual(values(facts, "CostOfRevenue"), {2020: 40.0})

    def test_a_reported_combined_cost_is_never_replaced_by_the_split_halves(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfRevenue=("USD", [duration("2020-12-31", 60.0, "2021-02-10")]),
            CostOfGoodsSold=("USD", [duration("2020-12-31", 40.0, "2021-02-10")]),
            CostOfServices=("USD", [duration("2020-12-31", 25.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {2020: 60.0})

    def test_a_year_is_rebuilt_from_the_quarterly_columns_of_a_10k(self):
        # Since 2018 Thermo Fisher and Intuit publish no dimensionless annual
        # cost line, only the quarterly note. Without this the modern half of
        # their history leaves four criteria unevaluable.
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfRevenue=("USD", quarters(
                "2020-01-01",
                ["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"],
                [15.0, 15.0, 15.0, 15.0],
                "2021-02-10",
            )),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {2020: 60.0})
        self.assertEqual(values(facts, "GrossProfit"), {2020: 40.0})

    def test_a_rolling_four_quarters_is_not_reported_as_a_fiscal_year(self):
        # Six quarters in one filing also contain windows that span twelve
        # months without being anyone's fiscal year. Only the run closing on the
        # year end the filer itself reported counts.
        rows = quarters(
            "2020-07-01",
            ["2020-09-30", "2020-12-31", "2021-03-31", "2021-06-30",
             "2021-09-30", "2021-12-31"],
            [10.0, 10.0, 20.0, 20.0, 30.0, 30.0],
            "2022-02-10",
        )
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2021-12-31", 300.0, "2022-02-10")]),
            CostOfRevenue=("USD", rows),
        ))

        # 2021 is the calendar year the filer closed on: 20+20+30+30.
        self.assertEqual(values(facts, "CostOfRevenue"), {2021: 100.0})

    def test_a_reported_annual_cost_is_never_replaced_by_a_rollup(self):
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfRevenue=("USD", [duration("2020-12-31", 55.0, "2021-02-10")] + quarters(
                "2020-01-01",
                ["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"],
                [15.0, 15.0, 15.0, 15.0],
                "2021-02-10",
            )),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {2020: 55.0})

    def test_a_gap_in_the_quarterly_note_produces_no_year(self):
        rows = quarters(
            "2020-01-01",
            ["2020-03-31", "2020-06-30", "2020-09-30"],
            [15.0, 15.0, 15.0],
            "2021-02-10",
        )
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostOfRevenue=("USD", rows),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {})

    def test_total_operating_expenses_are_not_read_as_cost_of_revenue(self):
        # CostsAndExpenses and OperatingExpenses are the whole expense base, not
        # the cost of revenue. Subtracting either would invent a gross profit.
        facts = annual_facts(payload(
            Revenues=("USD", [duration("2020-12-31", 100.0, "2021-02-10")]),
            CostsAndExpenses=("USD", [duration("2020-12-31", 90.0, "2021-02-10")]),
            OperatingExpenses=("USD", [duration("2020-12-31", 85.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "CostOfRevenue"), {})
        self.assertEqual(values(facts, "GrossProfit"), {})

    def test_depreciation_and_amortization_resolves_without_depletion(self):
        facts = annual_facts(payload(
            DepreciationAndAmortization=("USD", [duration("2020-12-31", 25.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "DepreciationAmortization"), {2020: 25.0})

    def test_rnd_resolves_from_the_excluding_in_process_tag(self):
        facts = annual_facts(payload(
            ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost=(
                "USD", [duration("2020-12-31", 40.0, "2021-02-10")]),
        ))

        self.assertEqual(values(facts, "RnD"), {2020: 40.0})


class RestatementTest(unittest.TestCase):
    def test_every_filed_version_of_a_year_is_kept(self):
        # Apple's FY2008 net income: 4,834M as filed, 6,119M as restated a year on.
        facts = annual_facts(payload(
            NetIncomeLoss=("USD", [
                duration("2008-09-27", 4834.0, "2009-10-27", accn="acc-2009"),
                duration("2008-09-27", 6119.0, "2010-10-27", accn="acc-2010"),
            ]),
        ))

        versions = [f for f in facts if f.concept == "NetIncome" and f.fiscal_year == 2008]

        self.assertEqual(
            sorted((f.value, f.filed) for f in versions),
            [(4834.0, "2009-10-27"), (6119.0, "2010-10-27")],
        )


if __name__ == "__main__":
    unittest.main()


class ShareScaleTest(unittest.TestCase):
    """A filer tagging the share count in millions must not reach the store."""

    def test_a_share_count_in_millions_is_dropped_not_rescaled(self):
        # Bruker filed 156.6 for 156,600,000. Kept, it makes EPS a million
        # times too large; rescaled, it is a guess about what was meant.
        facts = annual_facts(payload(
            WeightedAverageNumberOfDilutedSharesOutstanding=("shares", [
                duration("2018-12-31", 157_200_000.0, "2019-02-10", accn="a-1"),
                duration("2019-12-31", 156.6, "2020-02-10", accn="a-2"),
            ]),
        ))

        self.assertEqual(values(facts, "DilutedShares"), {2018: 157_200_000.0})

    def test_a_plausible_share_count_survives(self):
        facts = annual_facts(payload(
            WeightedAverageNumberOfDilutedSharesOutstanding=("shares", [
                duration("2019-12-31", 1_500_000.0, "2020-02-10"),
            ]),
        ))

        self.assertEqual(values(facts, "DilutedShares"), {2019: 1_500_000.0})

    def test_the_floor_applies_to_no_other_concept(self):
        # A company can genuinely earn 156.6 dollars; it cannot have 156.6 shares.
        facts = annual_facts(payload(
            NetIncomeLoss=("USD", [duration("2019-12-31", 156.6, "2020-02-10")]),
        ))

        self.assertEqual(values(facts, "NetIncome"), {2019: 156.6})
