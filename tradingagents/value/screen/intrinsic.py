"""Intrinsic value, Buffett's "equity bond" framing (plan section 6).

Treat the share as a bond whose coupon is EPS and whose coupon grows. Fit the
growth from ten years of diluted EPS, project a decade forward, capitalise at a
normalised multiple, discount back at the long bond yield:

    intrinsic = EPS_0 x (1 + g)^n x PE_terminal / (1 + r)^n

Every input that can run away is capped — growth at 15%, the terminal multiple
at 15x, the discount rate floored at 4% — because the failure mode of this
formula is not being wrong by 10%, it is extrapolating one lucky decade into a
number ten times too large.

Two deliberate simplifications, recorded rather than hidden:

- Dividends paid during the projection are ignored. The value is the terminal
  capitalisation alone, which understates income-heavy businesses.
- ``graham_number`` and ``owner_earnings_per_share`` are sanity anchors, not
  inputs. When the equity-bond value and the Graham number disagree by an order
  of magnitude, the DCF is broken and the report should show it.

Pure functions: no store, no network. ``price`` and ``median_pe`` are supplied
by the caller, which is what keeps this module replayable in a backtest.
"""

import math
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
    intrinsic_value: float
    price: float
    margin_of_safety: float
    graham_number: float | None
    owner_earnings_per_share: float | None

    @property
    def graham_disagrees(self) -> bool:
        """True when the two methods differ by more than 3x — read the inputs."""
        if not self.graham_number or self.graham_number <= 0:
            return False
        ratio = self.intrinsic_value / self.graham_number
        return ratio > 3.0 or ratio < 1 / 3.0


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
    intrinsic = projected * terminal_pe / (1 + rate) ** projection_years

    return Valuation(
        eps=tuple(eps),
        growth_rate=growth,
        growth_capped=raw_growth > GROWTH_RATE_CAP,
        discount_rate=rate,
        discount_floored=discount_rate < DISCOUNT_RATE_FLOOR,
        terminal_pe=terminal_pe,
        projected_eps=projected,
        intrinsic_value=intrinsic,
        price=price,
        margin_of_safety=(intrinsic - price) / intrinsic,
        graham_number=graham_number(financials),
        owner_earnings_per_share=owner_earnings_per_share(financials),
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
