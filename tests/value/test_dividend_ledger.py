"""The dividend ledger and the book folded out of it.

Arithmetic first: average cost, realised profit on a partial sale, the cash
identity, and the money-weighted return. Then the two things that make those
numbers wrong when nobody is looking — a split between purchase and today, and
a dividend whose ex-date fell before the shares were bought.
"""

import unittest
from datetime import date, timedelta

from tradingagents.value.dividend import book, ledger, store

TODAY = date.today().isoformat()


def days_ago(count: int) -> str:
    return (date.today() - timedelta(days=count)).isoformat()


def no_splits(ticker, trades, as_of):
    """Injected in place of the network call; the basis never moved."""
    return trades


def flat(price: float):
    return lambda ticker, as_of: price


class FoldTest(unittest.TestCase):
    def fold(self, *trades):
        return ledger.fold(trades)

    def buy(self, shares, price, on="2024-01-02", fees=0.0, ticker="PG"):
        return ledger.Trade(ticker, on, "buy", shares, price, fees)

    def sell(self, shares, price, on="2025-01-02", fees=0.0, ticker="PG"):
        return ledger.Trade(ticker, on, "sell", shares, price, fees)

    def test_two_purchases_average_into_one_cost_basis(self):
        position = self.fold(self.buy(10, 100.0), self.buy(10, 140.0, on="2024-06-02"))["PG"]
        self.assertAlmostEqual(position.shares, 20.0)
        self.assertAlmostEqual(position.cost, 2400.0)
        self.assertAlmostEqual(position.average_cost, 120.0)

    def test_fees_join_the_cost_basis_on_a_purchase(self):
        position = self.fold(self.buy(10, 100.0, fees=9.95))["PG"]
        self.assertAlmostEqual(position.cost, 1009.95)

    def test_a_partial_sale_realises_against_the_average_not_the_first_lot(self):
        # FIFO would realise 5 * (150 - 100) = 250 here. Average cost realises
        # against 120, and the difference is the whole point of the choice.
        position = self.fold(
            self.buy(10, 100.0),
            self.buy(10, 140.0, on="2024-06-02"),
            self.sell(5, 150.0),
        )["PG"]
        self.assertAlmostEqual(position.realised, 5 * (150.0 - 120.0))
        self.assertAlmostEqual(position.shares, 15.0)
        self.assertAlmostEqual(position.cost, 1800.0)
        self.assertAlmostEqual(position.average_cost, 120.0)

    def test_fees_on_a_sale_come_out_of_the_realised_profit(self):
        position = self.fold(self.buy(10, 100.0), self.sell(5, 150.0, fees=9.95))["PG"]
        self.assertAlmostEqual(position.realised, 5 * 50.0 - 9.95)

    def test_a_closed_position_keeps_its_realised_profit_and_reports_no_basis(self):
        position = self.fold(self.buy(10, 100.0), self.sell(10, 150.0))["PG"]
        self.assertFalse(position.is_open)
        self.assertAlmostEqual(position.shares, 0.0)
        self.assertAlmostEqual(position.cost, 0.0)
        self.assertIsNone(position.average_cost)
        self.assertAlmostEqual(position.realised, 500.0)

    def test_selling_more_than_is_held_raises_rather_than_going_negative(self):
        with self.assertRaises(ledger.LedgerError):
            self.fold(self.buy(10, 100.0), self.sell(11, 150.0))

    def test_a_sale_dated_before_the_purchase_raises(self):
        with self.assertRaises(ledger.LedgerError):
            self.fold(self.buy(10, 100.0, on="2024-06-02"), self.sell(5, 150.0, on="2024-01-02"))

    def test_trade_cash_is_negative_to_buy_and_positive_to_sell(self):
        self.assertAlmostEqual(self.buy(10, 100.0, fees=5.0).cash, -1005.0)
        self.assertAlmostEqual(self.sell(10, 100.0, fees=5.0).cash, 995.0)


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def rows(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_a_trade_that_would_break_the_book_is_not_written(self):
        ledger.record_trade(self.conn, "PG", "buy", 10, 100.0, traded_on="2024-01-02")
        with self.assertRaises(ledger.LedgerError):
            ledger.record_trade(self.conn, "PG", "sell", 25, 150.0, traded_on="2025-01-02")
        self.assertEqual(self.rows("dividend_lots"), 1)

    def test_a_back_dated_sale_is_validated_against_the_whole_name(self):
        ledger.record_trade(self.conn, "PG", "buy", 10, 100.0, traded_on="2024-06-02")
        with self.assertRaises(ledger.LedgerError):
            ledger.record_trade(self.conn, "PG", "sell", 5, 150.0, traded_on="2024-01-02")

    def test_cash_amounts_are_magnitudes(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.record_cash(self.conn, "deposit", -500.0)
        with self.assertRaises(ledger.LedgerError):
            ledger.record_cash(self.conn, "deposit", 0.0)

    def test_a_dividend_without_a_ticker_is_refused(self):
        with self.assertRaises(ledger.LedgerError):
            ledger.record_cash(self.conn, "dividend", 12.0)
        self.assertEqual(self.rows("dividend_cash"), 0)


class BookTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)
        ledger.record_cash(self.conn, "deposit", 5000.0, happened_on=days_ago(400))
        ledger.record_trade(self.conn, "PG", "buy", 20, 100.0, traded_on=days_ago(390))

    def build(self, price=120.0, **kwargs):
        return book.build(self.conn, prices=flat(price), splits=no_splits, **kwargs)

    def test_market_value_and_unrealised_come_off_the_folded_position(self):
        holding = self.build().holdings[0]
        self.assertAlmostEqual(holding.market_value, 2400.0)
        self.assertAlmostEqual(holding.unrealised, 400.0)

    def test_cash_is_what_went_in_minus_what_the_trades_cost(self):
        self.assertAlmostEqual(self.build().cash, 3000.0)

    def test_total_pnl_equals_its_own_components(self):
        ledger.record_cash(self.conn, "dividend", 37.50, ticker="PG", happened_on=days_ago(200))
        ledger.record_trade(self.conn, "PG", "sell", 5, 130.0, traded_on=days_ago(100))
        result = self.build()
        self.assertAlmostEqual(
            result.total_pnl,
            result.realised + result.unrealised + result.dividends_recorded,
        )

    def test_equity_is_cash_plus_market_value(self):
        result = self.build()
        self.assertAlmostEqual(result.equity, result.cash + result.market_value)

    def test_offline_refuses_to_invent_the_priced_figures(self):
        result = book.build(self.conn, offline=True)
        self.assertFalse(result.priced)
        self.assertIsNone(result.market_value)
        self.assertIsNone(result.total_pnl)
        self.assertIn("offline", "\n".join(book.render(result, None)))

    def test_income_and_yield_on_cost_use_the_last_full_year(self):
        year = date.today().year - 1
        store.upsert(self.conn, "PG", [(f"{year}-{m:02d}-15", 1.0) for m in (2, 5, 8, 11)],
                     "2026-01-01T00:00:00+00:00")
        holding = self.build().holdings[0]
        self.assertAlmostEqual(holding.annual_income, 80.0)          # 20 shares * 4.00
        self.assertAlmostEqual(holding.yield_on_cost, 80.0 / 2000.0)

    def test_a_dividend_whose_ex_date_predates_the_purchase_earns_nothing(self):
        store.upsert(self.conn, "PG",
                     [(days_ago(500), 1.0), (days_ago(300), 1.0)],
                     "2026-01-01T00:00:00+00:00")
        # Only the second ex-date falls after the 390-days-ago purchase.
        self.assertAlmostEqual(self.build().dividends_expected, 20.0)

    def test_recorded_and_expected_dividends_are_reported_separately(self):
        store.upsert(self.conn, "PG", [(days_ago(300), 1.0)], "2026-01-01T00:00:00+00:00")
        ledger.record_cash(self.conn, "dividend", 14.0, ticker="PG", happened_on=days_ago(290))
        result = self.build()
        # 30% withholding: gross 20.00 arrives as 14.00. Neither figure is
        # adjusted into the other.
        self.assertAlmostEqual(result.dividends_expected, 20.0)
        self.assertAlmostEqual(result.dividends_recorded, 14.0)


class SplitTest(unittest.TestCase):
    def test_a_split_since_purchase_moves_shares_without_moving_dollars(self):
        trades = [ledger.Trade("PG", "2020-01-02", "buy", 10, 300.0)]
        original = book.market.split_basis_factors
        book.market.split_basis_factors = lambda ticker, filed, as_of: {0: 3.0}
        try:
            adjusted = book.adjust_for_splits("PG", trades, TODAY)
        finally:
            book.market.split_basis_factors = original

        self.assertAlmostEqual(adjusted[0].shares, 30.0)
        self.assertAlmostEqual(adjusted[0].price, 100.0)
        self.assertAlmostEqual(adjusted[0].shares * adjusted[0].price, 3000.0)


class ReturnTest(unittest.TestCase):
    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)

    def test_a_ten_percent_year_solves_to_ten_percent(self):
        rate = book._irr([(days_ago(365), -1000.0), (TODAY, 1100.0)])
        self.assertAlmostEqual(rate, 0.10, places=3)

    def test_dividends_are_not_treated_as_fresh_capital(self):
        ledger.record_cash(self.conn, "deposit", 1000.0, happened_on=days_ago(365))
        ledger.record_trade(self.conn, "PG", "buy", 10, 100.0, traded_on=days_ago(365))
        ledger.record_cash(self.conn, "dividend", 50.0, ticker="PG", happened_on=days_ago(180))

        result = book.build(self.conn, prices=flat(105.0), splits=no_splits)
        # Equity is 1100: 1050 of stock plus the 50 of dividend sitting in cash.
        self.assertAlmostEqual(result.equity, 1100.0)
        self.assertAlmostEqual(book.money_weighted_return(self.conn, result), 0.10, places=3)

    def test_no_external_flows_means_no_return_rather_than_a_zero(self):
        self.assertIsNone(
            book.money_weighted_return(self.conn, book.build(self.conn, prices=flat(1.0),
                                                            splits=no_splits))
        )


if __name__ == "__main__":
    unittest.main()


class UncachedIncomeTest(unittest.TestCase):
    """An empty dividend cache must not read as a name that pays nothing."""

    def setUp(self):
        self.conn = store.connect(":memory:")
        self.addCleanup(self.conn.close)
        ledger.record_cash(self.conn, "deposit", 5000.0, happened_on=days_ago(400))
        ledger.record_trade(self.conn, "PG", "buy", 20, 100.0, traded_on=days_ago(390))

    def build(self):
        return book.build(self.conn, prices=flat(120.0), splits=no_splits)

    def test_no_cached_history_reports_unknown_rather_than_zero(self):
        holding = self.build().holdings[0]
        self.assertIsNone(holding.annual_dps)
        self.assertIsNone(holding.annual_income)
        self.assertIsNone(holding.yield_on_cost)

    def test_the_income_total_says_which_names_it_excludes(self):
        result = self.build()
        self.assertEqual(result.uncached, ("PG",))
        text = "\n".join(book.render(result, None))
        self.assertIn("no dividend history cached", text)
        self.assertIn("total excludes PG", text)

    def test_a_cached_history_that_paid_nothing_last_year_is_a_real_zero(self):
        # Rows exist, they are simply all older than the window: a payer that
        # stopped, which is a finding rather than a gap.
        store.upsert(self.conn, "PG", [("2015-06-15", 1.0)], "2026-01-01T00:00:00+00:00")
        holding = self.build().holdings[0]
        self.assertEqual(holding.annual_dps, 0.0)
        self.assertEqual(holding.annual_income, 0.0)
        self.assertEqual(self.build().uncached, ())
