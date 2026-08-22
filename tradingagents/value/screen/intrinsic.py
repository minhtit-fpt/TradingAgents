"""Intrinsic value, Buffett's "equity bond" framing (plan section 6).

Treat the share as a bond whose coupon is EPS and whose coupon grows. Fit the
growth from ten years of diluted EPS, project a decade forward, capitalise at a
normalised multiple, discount back at the long bond yield:

    intrinsic = sum_t=1..n [ EPS_0 x (1 + g)^t x payout / (1 + r)^t ]
              + EPS_0 x (1 + g)^n x PE_terminal / (1 + r)^n

A holder of the share collects the dividends *and* owns the business at the end,
so the value is both: the discounted payout stream over the projection, plus the
terminal capitalisation. Leaving the stream out understated every income-heavy
business by the whole decade of cash it hands over.

Every input that can run away is capped — growth at 15%, the terminal multiple
at 15x, the discount rate floored at 4%, the payout at 100% of earnings —
because the failure mode of this formula is not being wrong by 10%, it is
extrapolating one lucky decade into a number ten times too large.

One deliberate simplification, recorded rather than hidden:
``graham_number`` and ``owner_earnings_per_share`` are sanity anchors, not
inputs. The Graham number ignores growth, so it is compared against
``earnings_power_value`` — this same formula with growth set to zero — and not
against the projected value. Comparing it to the projected value only measured
whether the company grows, which every name reaching the trigger does.

Pure functions: no store, no network. ``price`` and ``median_pe`` are supplied
by the caller, which is what keeps this module replayable in a backtest.
"""

import math
import statistics
from dataclasses import dataclass

from ..config import (
    DISCOUNT_RATE_FLOOR,
    GROWTH_RATE_CAP,
    PROJECTION_YEARS,
    TERMINAL_PE_CAP,
)

Financials = dict[int, dict[str, float]]


class ValuationError(ValueError):
    """The inputs cannot support a valuation. Never returns a fallback number."""


@dataclass(frozen=True)
class Valuation:
    """A valuation and enough of its working to argue with."""

    eps: tuple[tuple[int, float], ...]
    growth_rate: float
    growth_capped: bool
    discount_rate: float
    discount_floored: bool
    terminal_pe: float
    projected_eps: float
    payout_ratio: float | None
    dividend_value: float
    intrinsic_value: float
    earnings_power_value: float
    price: float
    margin_of_safety: float
    graham_number: float | None
    owner_earnings_per_share: float | None

    @property
    def graham_disagrees(self) -> bool:
        """True when the two no-growth methods differ by more than 3x.

        Both sides value the latest year's earnings without projecting growth:
        the Graham number off book value, ``earnings_power_value`` off the
        terminal multiple. A gap here means an input is wrong — book value,
        share count, or the earnings figure — not that the company compounds.
        """
        if not self.graham_number or self.graham_number <= 0:
            return False
        ratio = self.earnings_power_value / self.graham_number
        return ratio > 3.0 or ratio < 1 / 3.0


def on_current_share_basis(
    financials: Financials, factors: dict[int, float]
) -> Financials:
    """Copy of ``financials`` with every share count on one, current, basis.

    Share counts are filed on the basis in force at the filing, so a split
    partway through a ten-year history leaves the series with a step in it that
    is not a business event. Each year is multiplied by its own factor (see
    ``market.split_basis_factors``); a year whose factor is unknown loses its
    share count entirely rather than joining the series on a guessed basis,
    which drops it from the EPS history instead of bending it.
    """
    rebased: Financials = {}
    for year, facts in financials.items():
        if "DilutedShares" not in facts:
            rebased[year] = dict(facts)
            continue
        factor = factors.get(year)
        rebased[year] = {k: v for k, v in facts.items() if k != "DilutedShares"}
        if factor:
            rebased[year]["DilutedShares"] = facts["DilutedShares"] * factor
    return rebased


def eps_history(financials: Financials) -> list[tuple[int, float]]:
    """Diluted EPS per fiscal year, for years carrying both inputs."""
    history = []
    for year in sorted(financials):
        net_income = financials[year].get("NetIncome")
        shares = financials[year].get("DilutedShares")
        if net_income is None or not shares:
            continue
        history.append((year, net_income / shares))
    return history


def fit_growth(eps: list[tuple[int, float]]) -> float:
    """Annual growth from a least-squares fit of ``ln(EPS)`` against the year.

    A fit rather than a first-to-last CAGR: endpoint arithmetic hands the entire
    answer to two data points, so one exceptional final year — or one depressed
    starting year — sets the growth rate for the whole projection.

    Uncapped here. Capping is the caller's decision and is recorded in the
    ``Valuation``, so the raw fit stays visible.
    """
    if len(eps) < 2:
        raise ValuationError(f"need at least 2 years of EPS, got {len(eps)}")
    if any(value <= 0 for _, value in eps):
        raise ValuationError("cannot fit growth through a loss year")

    years = [float(year) for year, _ in eps]
    logs = [math.log(value) for _, value in eps]
    mean_year = sum(years) / len(years)
    mean_log = sum(logs) / len(logs)
    variance = sum((year - mean_year) ** 2 for year in years)
    if variance == 0:
        raise ValuationError("EPS history spans a single fiscal year")

    covariance = sum(
        (y - mean_year) * (log - mean_log) for y, log in zip(years, logs, strict=True)
    )
    return math.exp(covariance / variance) - 1


def value(
    financials: Financials,
    price: float,
    discount_rate: float,
    *,
    median_pe: float | None = None,
    projection_years: int = PROJECTION_YEARS,
) -> Valuation:
    """Value one company. Raises ``ValuationError`` rather than guessing."""
    if price <= 0:
        raise ValuationError(f"price must be positive, got {price}")

    eps = eps_history(financials)
    if not eps:
        raise ValuationError("no year carries both NetIncome and DilutedShares")

    raw_growth = fit_growth(eps)
    growth = min(raw_growth, GROWTH_RATE_CAP)
    rate = max(discount_rate, DISCOUNT_RATE_FLOOR)
    terminal_pe = min(median_pe, TERMINAL_PE_CAP) if median_pe else TERMINAL_PE_CAP
    if terminal_pe <= 0:
        raise ValuationError(f"terminal P/E must be positive, got {terminal_pe}")

    latest_eps = eps[-1][1]
    projected = latest_eps * (1 + growth) ** projection_years
    terminal = projected * terminal_pe / (1 + rate) ** projection_years

    payout = payout_ratio(financials)
    dividends = _dividend_stream(latest_eps, growth, rate, payout, projection_years)
    intrinsic = dividends + terminal

    # The growth-free twin of the same formula, used only as the Graham anchor.
    earnings_power = _dividend_stream(
        latest_eps, 0.0, rate, payout, projection_years
    ) + latest_eps * terminal_pe / (1 + rate) ** projection_years

    return Valuation(
        eps=tuple(eps),
        growth_rate=growth,
        growth_capped=raw_growth > GROWTH_RATE_CAP,
        discount_rate=rate,
        discount_floored=discount_rate < DISCOUNT_RATE_FLOOR,
        terminal_pe=terminal_pe,
        projected_eps=projected,
        payout_ratio=payout,
        dividend_value=dividends,
        intrinsic_value=intrinsic,
        earnings_power_value=earnings_power,
        price=price,
        margin_of_safety=(intrinsic - price) / intrinsic,
        graham_number=graham_number(financials),
        owner_earnings_per_share=owner_earnings_per_share(financials),
    )


def payout_ratio(financials: Financials) -> float | None:
    """Median share of earnings paid out, or None for a company that pays nothing.

    Median rather than the latest year: a single special dividend is one board
    decision, not the decade of policy this projects forward. Capped at all of
    earnings — a payout above 100% happens, but a decade of it does not, and
    projecting one compounds cash the business cannot fund.

    None and 0.0 mean different things here and both are honest: None is a
    company that has never paid, 0.0 would be one that pays a rounding error.
    """
    ratios = []
    for year in sorted(financials):
        facts = financials[year]
        dividends = facts.get("DividendsPaid")
        net_income = facts.get("NetIncome")
        if dividends is None or not net_income or net_income <= 0:
            continue
        ratios.append(min(dividends / net_income, 1.0))
    return statistics.median(ratios) if ratios else None


def _dividend_stream(
    latest_eps: float,
    growth: float,
    rate: float,
    payout: float | None,
    projection_years: int,
) -> float:
    """Present value of the projected payout, or 0.0 for a non-payer."""
    if not payout:
        return 0.0
    return sum(
        latest_eps * (1 + growth) ** year * payout / (1 + rate) ** year
        for year in range(1, projection_years + 1)
    )


def graham_number(financials: Financials) -> float | None:
    """``sqrt(22.5 x EPS x book value per share)`` for the latest year, or None.

    Graham's ceiling for a defensive buy. Here it is only a second opinion: it
    ignores growth entirely, so it should sit below the equity-bond value for a
    genuine compounder and far below it for a hyped one.
    """
    eps = eps_history(financials)
    if not eps:
        return None
    year = eps[-1][0]
    equity = financials[year].get("Equity")
    shares = financials[year].get("DilutedShares")
    if equity is None or not shares:
        return None

    book_value_per_share = equity / shares
    if eps[-1][1] <= 0 or book_value_per_share <= 0:
        return None
    return math.sqrt(22.5 * eps[-1][1] * book_value_per_share)


def owner_earnings_per_share(financials: Financials) -> float | None:
    """``(net income + D&A - maintenance capex) / diluted shares``, latest year.

    Maintenance capex is approximated as ``min(capex, D&A)`` — the plan says so
    explicitly, and the approximation travels in the output so a reviewer can
    see that growth capex was never separated out.
    """
    eps = eps_history(financials)
    if not eps:
        return None
    facts = financials[eps[-1][0]]
    net_income = facts.get("NetIncome")
    depreciation = facts.get("DepreciationAmortization")
    capex = facts.get("Capex")
    shares = facts.get("DilutedShares")
    if net_income is None or depreciation is None or capex is None or not shares:
        return None

    maintenance_capex = min(capex, depreciation)
    return (net_income + depreciation - maintenance_capex) / shares
