"""Split handling: the price a valuation divides must be the price as traded."""

import unittest
from datetime import date

import pandas as pd

from tradingagents.value.screen.market import AS_TRADED, basis_factors, with_as_traded


def frame(rows: dict[str, float]):
    index = pd.DatetimeIndex([date.fromisoformat(day) for day in rows])
    return pd.DataFrame({"Close": list(rows.values())}, index=index)


def splits(rows: dict[str, float]):
    return pd.Series(
        list(rows.values()),
        index=pd.DatetimeIndex([date.fromisoformat(day) for day in rows]),
    )


class AsTradedTest(unittest.TestCase):
    def test_a_later_split_is_undone(self):
        # Netflix closed 2024-12-31 at 891.32; after the 10-for-1 split of
        # 2025-11-17, yfinance serves that same day as 89.132.
        priced = with_as_traded(
            frame({"2024-12-31": 89.132, "2025-12-31": 100.0}),
            splits({"2025-11-17": 10.0}),
        )

        self.assertAlmostEqual(priced[AS_TRADED].iloc[0], 891.32, places=2)
        self.assertAlmostEqual(priced[AS_TRADED].iloc[1], 100.0, places=2)

    def test_the_adjusted_column_is_left_alone(self):
        # The simulator computes returns across the split from this one.
        priced = with_as_traded(
            frame({"2024-12-31": 89.132}), splits({"2025-11-17": 10.0})
        )

        self.assertAlmostEqual(priced["Close"].iloc[0], 89.132, places=3)

    def test_splits_compound(self):
        priced = with_as_traded(
            frame({"2020-01-02": 1.0}), splits({"2021-06-01": 4.0, "2023-06-01": 3.0})
        )

        self.assertAlmostEqual(priced[AS_TRADED].iloc[0], 12.0, places=6)

    def test_a_split_on_the_day_itself_is_already_in_the_price(self):
        # The close of the split day is quoted post-split by the exchange too.
        priced = with_as_traded(
            frame({"2021-06-01": 25.0}), splits({"2021-06-01": 4.0})
        )

        self.assertAlmostEqual(priced[AS_TRADED].iloc[0], 25.0, places=6)

    def test_a_never_split_company_is_unchanged(self):
        priced = with_as_traded(frame({"2024-12-31": 50.0}), splits({}))

        self.assertAlmostEqual(priced[AS_TRADED].iloc[0], 50.0, places=6)

    def test_the_input_frame_is_not_mutated(self):
        original = frame({"2024-12-31": 89.132})
        with_as_traded(original, splits({"2025-11-17": 10.0}))

        self.assertNotIn(AS_TRADED, original.columns)


class HistoryColumnTest(unittest.TestCase):
    """The wiring: valuations read as-traded, the simulator reads adjusted."""

    def test_history_close_is_as_traded_and_frame_stays_adjusted(self):
        from tests.value.factories import price_frame
        from tradingagents.value.backtest.prices import History

        # A 10-for-1 split after the date being screened.
        frames = {"NFLX": price_frame(
            "2024-01-01", "2026-01-01", price=89.132,
            splits=splits({"2025-11-17": 10.0}),
        )}
        history = History("2024-01-01", "2026-01-01",
                          fetch=lambda t, *a, **k: frames[t])

        self.assertAlmostEqual(history.close("NFLX", "2024-12-31"), 891.32, places=2)
        self.assertAlmostEqual(
            float(history.frame("NFLX")["Close"].iloc[0]), 89.132, places=3
        )


class ShareBasisTest(unittest.TestCase):
    """A split between two filings must not read as an earnings collapse."""

    def rebased(self, filed: dict[int, str], counts: dict[int, float]):
        from tradingagents.value.screen.intrinsic import on_current_share_basis

        # One bar a quarter through the whole span: a filing date must land on
        # the right side of the split for its ratio to be read correctly.
        days = {f"{year}-{month:02d}-01": 100.0
                for year in range(2021, 2027) for month in (1, 4, 7, 10)}
        priced = with_as_traded(frame(days), splits({"2024-06-10": 10.0}))
        factors = basis_factors(priced, filed, date.fromisoformat("2026-01-02"))
        financials = {y: {"NetIncome": 1e9, "DilutedShares": c} for y, c in counts.items()}
        return on_current_share_basis(financials, factors)

    def test_a_pre_split_filing_is_scaled_onto_the_current_basis(self):
        # Nvidia's shape: FY2021 filed before the 10-for-1, FY2022 after it.
        rebased = self.rebased(
            filed={2021: "2024-02-21", 2022: "2025-02-26"},
            counts={2021: 2_535_000_000.0, 2022: 25_070_000_000.0},
        )

        self.assertAlmostEqual(rebased[2021]["DilutedShares"], 25_350_000_000.0, places=0)
        self.assertAlmostEqual(rebased[2022]["DilutedShares"], 25_070_000_000.0, places=0)

    def test_the_eps_series_no_longer_steps_across_the_split(self):
        from tradingagents.value.screen.intrinsic import eps_history

        eps = dict(eps_history(self.rebased(
            filed={2021: "2024-02-21", 2022: "2025-02-26"},
            counts={2021: 2_535_000_000.0, 2022: 25_070_000_000.0},
        )))

        # Same earnings, near-identical share count: no 90% cliff.
        self.assertLess(abs(eps[2022] / eps[2021] - 1), 0.05)

    def test_a_year_with_no_factor_loses_its_share_count(self):
        from tradingagents.value.screen.intrinsic import on_current_share_basis

        rebased = on_current_share_basis({2021: {"DilutedShares": 100.0}}, {})

        self.assertNotIn("DilutedShares", rebased[2021])
