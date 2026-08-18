"""Statistical power: bootstrap intervals, the pre-registered criterion, and the
bound on the residual that cannot be priced (phase 4b step 3).

No network, no backtrader — every assertion here is arithmetic on return series,
which is the point: the apparatus that decides the gate has to be testable
without a two-hour replay.
"""

import unittest

from tradingagents.value.backtest import numeric, stats


def curve(values, start_year: int = 2014) -> tuple[tuple[str, float], ...]:
    """A quarter-end equity curve, one point per value."""
    dates = numeric.quarter_ends(f"{start_year}-01-01", f"{start_year + 30}-12-31")
    return tuple((day, float(v)) for day, v in zip(dates, values, strict=False))


class SamplingTest(unittest.TestCase):
    def test_a_value_is_taken_at_or_before_the_date_never_after(self):
        series = (("2024-03-31", 100.0), ("2024-06-30", 110.0))

        self.assertEqual(stats.value_at(series, "2024-05-01"), 100.0)
        self.assertEqual(stats.value_at(series, "2024-06-30"), 110.0)
        self.assertIsNone(stats.value_at(series, "2024-01-01"))

    def test_paired_returns_drop_dates_either_series_cannot_answer(self):
        strategy = (("2024-06-30", 100.0), ("2024-09-30", 110.0))
        benchmark = (("2024-03-31", 50.0), ("2024-06-30", 50.0), ("2024-09-30", 55.0))

        theirs, ours = stats.paired_returns(
            strategy, benchmark, ["2024-03-31", "2024-06-30", "2024-09-30"]
        )

        self.assertEqual(len(theirs), 1)
        self.assertAlmostEqual(theirs[0], 0.10)
        self.assertAlmostEqual(ours[0], 0.10)


class ArithmeticTest(unittest.TestCase):
    def test_cagr_annualises_the_compounded_period_returns(self):
        self.assertAlmostEqual(stats.cagr([0.10, 0.10], years=2.0), 0.10, places=6)

    def test_a_total_loss_is_minus_one_not_an_exception(self):
        self.assertEqual(stats.cagr([0.05, -1.0], years=2.0), -1.0)

    def test_max_drawdown_is_measured_peak_to_trough(self):
        # 1.0 -> 1.2 -> 0.6: half the peak is given back.
        self.assertAlmostEqual(stats.max_drawdown([0.2, -0.5]), 0.5)

    def test_a_series_that_only_rises_has_no_drawdown(self):
        self.assertEqual(stats.max_drawdown([0.1, 0.1]), 0.0)


class BootstrapTest(unittest.TestCase):
    def test_a_constant_edge_yields_an_interval_that_excludes_zero(self):
        strategy = [0.05] * 40
        benchmark = [0.01] * 40

        excess, _ = stats.bootstrap(strategy, benchmark, years=10.0, samples=200, seed=1)

        self.assertTrue(excess.excludes_zero)
        self.assertGreater(excess.low, 0.0)

    def test_noise_around_zero_yields_an_interval_that_contains_zero(self):
        strategy = [0.20, -0.18] * 20
        benchmark = [0.01] * 40

        excess, _ = stats.bootstrap(strategy, benchmark, years=10.0, samples=400, seed=1)

        self.assertFalse(excess.excludes_zero)

    def test_the_point_estimate_is_the_observed_series_not_a_resample(self):
        strategy = [0.05] * 40
        benchmark = [0.01] * 40

        excess, drawdown = stats.bootstrap(
            strategy, benchmark, years=10.0, samples=50, seed=7
        )

        self.assertAlmostEqual(
            excess.point,
            stats.cagr(strategy, 10.0) - stats.cagr(benchmark, 10.0),
            places=6,
        )
        self.assertAlmostEqual(drawdown.point, stats.max_drawdown(strategy), places=6)

    def test_the_same_seed_gives_the_same_interval(self):
        args = ([0.03, -0.01] * 20, [0.01] * 40)
        first, _ = stats.bootstrap(*args, years=10.0, samples=100, seed=3)
        again, _ = stats.bootstrap(*args, years=10.0, samples=100, seed=3)

        self.assertEqual((first.low, first.high), (again.low, again.high))

    def test_too_few_periods_to_resample_is_none_rather_than_a_fake_interval(self):
        self.assertEqual(stats.bootstrap([0.01], [0.01], years=1.0), (None, None))


class CriterionTest(unittest.TestCase):
    def positive(self) -> stats.Interval:
        return stats.Interval(point=0.03, low=0.01, high=0.05)

    def test_an_edge_within_the_index_drawdown_passes(self):
        verdict = stats.judge(self.positive(), drawdown=0.30, benchmark_drawdown=0.34)

        self.assertTrue(verdict.passes)

    def test_an_interval_containing_zero_fails_however_good_the_point_is(self):
        wide = stats.Interval(point=0.03, low=-0.02, high=0.08)

        verdict = stats.judge(wide, drawdown=0.20, benchmark_drawdown=0.34)

        self.assertFalse(verdict.passes)
        self.assertTrue(any("zero" in reason for reason in verdict.reasons))

    def test_a_deeper_drawdown_than_the_index_fails_even_with_a_clear_edge(self):
        verdict = stats.judge(self.positive(), drawdown=0.48, benchmark_drawdown=0.34)

        self.assertFalse(verdict.passes)
        self.assertTrue(any("drawdown" in reason for reason in verdict.reasons))

    def test_a_negative_interval_fails_rather_than_counting_as_separated(self):
        losing = stats.Interval(point=-0.03, low=-0.05, high=-0.01)

        self.assertFalse(stats.judge(losing, drawdown=0.10, benchmark_drawdown=0.34).passes)


class Snap:
    """The three counts of a ``Snapshot`` that the residual bound reads."""

    def __init__(self, screened, passed, valued, absent, triggered):
        self.screened, self.passed, self.absent = screened, passed, absent
        self.valued = tuple((f"V{i}", 0.5) for i in range(valued))
        self._triggered = triggered

    def triggered(self, minimum):
        return tuple(f"V{i}" for i in range(self._triggered))


class ResidualBoundTest(unittest.TestCase):
    def test_nothing_unpriceable_means_no_phantom_weight(self):
        snap = Snap(screened=100, passed=10, valued=10, absent=0, triggered=5)

        self.assertEqual(stats.phantom_weight(snap, held=5), 0.0)

    def test_a_passer_with_no_price_takes_its_share_of_nav(self):
        # 10 passed, 8 valued: 2 passers had no price. Trigger rate 4/8, so one
        # of the two would have been held beside the four that were.
        snap = Snap(screened=100, passed=10, valued=8, absent=0, triggered=4)

        self.assertAlmostEqual(stats.phantom_weight(snap, held=4), 1.0 / 5.0)

    def test_members_absent_from_the_store_are_estimated_at_the_observed_pass_rate(self):
        # 100 screened, 10 passed: 10%. 20 absent members imply 2 more passers,
        # and the selection rate is 1.0 here, so both would have been held.
        snap = Snap(screened=100, passed=10, valued=10, absent=20, triggered=10)

        self.assertAlmostEqual(stats.phantom_weight(snap, held=10), 2.0 / 12.0)

    def test_a_carried_book_larger_than_the_days_valued_set_caps_the_rate(self):
        # Step 5 carries incumbents forward, so 12 names can be held on a date
        # that valued 8. Uncapped, the 2 unpriceable passers would scale to 3
        # phantom positions — more phantom names than exist.
        snap = Snap(screened=100, passed=10, valued=8, absent=0, triggered=8)

        self.assertAlmostEqual(stats.phantom_weight(snap, held=12), 2.0 / 14.0)

    def test_a_total_loss_stub_drags_the_blended_return_toward_minus_one(self):
        blended = stats.blend([0.10, 0.10], [0.5, 0.0], stub=-1.0)

        self.assertAlmostEqual(blended[0], 0.5 * 0.10 + 0.5 * -1.0)
        self.assertAlmostEqual(blended[1], 0.10)

    def test_a_terminal_stub_is_amortised_over_the_holding_span_not_charged_per_period(self):
        # A position held four periods that ends at +25% contributed 25%/4 per
        # period, not 25% four times over.
        blended = stats.blend([0.0], [1.0], stub=0.25, holding_periods=4.0)

        self.assertAlmostEqual(blended[0], 0.0625)

    def test_a_wipeout_decays_linearly_over_the_holding_span(self):
        blended = stats.blend([0.0, 0.0], [1.0, 1.0], stub=-1.0, holding_periods=4.0)

        self.assertAlmostEqual(blended[0], -0.25)

    def test_a_holding_span_below_one_period_cannot_amplify_the_stub(self):
        blended = stats.blend([0.0], [1.0], stub=-1.0, holding_periods=0.25)

        self.assertAlmostEqual(blended[0], -1.0)

    def test_the_sweep_reports_one_interval_per_stub_return(self):
        rows = stats.sweep(
            [0.05] * 40, [0.01] * 40, [0.1] * 40, years=10.0, samples=100, seed=2
        )

        self.assertEqual([stub for stub, _, _ in rows], list(stats.STUB_RETURNS))
        losing = {stub: interval for stub, interval, _ in rows}
        self.assertLess(losing[-1.0].point, losing[0.25].point)

    def test_a_sweep_with_no_phantom_weight_repeats_the_headline_interval(self):
        rows = stats.sweep(
            [0.05] * 40, [0.01] * 40, [0.0] * 40, years=10.0, samples=100, seed=2
        )
        points = {interval.point for _, interval, _ in rows}

        self.assertEqual(len(points), 1)


class SummaryTest(unittest.TestCase):
    def snaps(self, n: int = 40):
        return [Snap(100, 10, 10, 0, 5) for _ in range(n)]

    def curves(self, n: int = 40):
        dates = numeric.quarter_ends("2014-01-01", "2026-06-30")[:n]
        ours = tuple((d, 100.0 * 1.03**i) for i, d in enumerate(dates))
        theirs = tuple((d, 100.0 * 1.01**i) for i, d in enumerate(dates))
        return dates, ours, theirs

    def test_the_summary_states_the_criterion_and_a_verdict(self):
        dates, ours, theirs = self.curves()
        snaps = self.snaps()
        for snap, day in zip(snaps, dates, strict=True):
            snap.as_of = day

        lines = stats.summary(
            snaps, ours, theirs,
            held=dict.fromkeys(dates, 5), setting="the configured trigger 30%",
            years=10.0, benchmark="SPY", samples=100,
        )
        text = "\n".join(lines)

        self.assertIn("pre-registered", text)
        self.assertIn("VERDICT", text)
        self.assertIn("residual", text)

    def test_a_curve_too_short_to_resample_says_so_instead_of_inventing_a_ci(self):
        dates, ours, theirs = self.curves(n=2)
        snaps = self.snaps(n=2)
        for snap, day in zip(snaps, dates, strict=True):
            snap.as_of = day

        text = "\n".join(stats.summary(
            snaps, ours, theirs,
            held=dict.fromkeys(dates, 5), setting="the configured trigger 30%",
            years=0.5, benchmark="SPY", samples=100,
        ))

        self.assertIn("too few", text)
        self.assertNotIn("VERDICT: pass", text)


if __name__ == "__main__":
    unittest.main()
