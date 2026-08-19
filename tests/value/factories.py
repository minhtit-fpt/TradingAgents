"""Synthetic financials and prices for the screening and backtest tests.

One deliberately excellent company — every criterion cleared with room to spare —
so that a test which mutates a single number is testing exactly that number.
"""

from tradingagents.value.edgar.concepts import Fact

# A clean base year. Ratios: 50% gross margin, 25% net margin, SG&A 20% of gross
# profit, ROE 25%, debt/equity 0.45. Assets = liabilities + equity, so the
# balance sheet also reads as a balance sheet.
BASE_YEAR: dict[str, float] = {
    "Revenue": 1000.0,
    "CostOfRevenue": 500.0,
    "GrossProfit": 500.0,
    "SGA": 100.0,
    "RnD": 50.0,
    "DepreciationAmortization": 25.0,
    "OperatingIncome": 300.0,
    "InterestExpense": 10.0,
    "IncomeTax": 50.0,
    "NetIncome": 250.0,
    "Assets": 1500.0,
    "AssetsCurrent": 600.0,
    "Liabilities": 500.0,
    "LiabilitiesCurrent": 200.0,
    "Equity": 1000.0,
    "LongTermDebt": 400.0,
    "RetainedEarnings": 800.0,
    "TreasuryStock": 100.0,
    "OperatingCashFlow": 300.0,
    "Capex": 50.0,
    "DilutedShares": 100.0,
    "DividendsPaid": 30.0,
}

# Concepts that scale with the business. The balance-sheet lines stay flat so a
# rising ROE comes from earnings, not from shrinking equity.
_GROWING = ("Revenue", "CostOfRevenue", "GrossProfit", "SGA", "RnD",
            "DepreciationAmortization", "OperatingIncome", "InterestExpense",
            "IncomeTax", "NetIncome", "RetainedEarnings", "OperatingCashFlow",
            "Capex", "DividendsPaid")


def decade(
    first_year: int = 2015,
    years: int = 10,
    growth: float = 0.10,
) -> dict[int, dict[str, float]]:
    """``{fiscal_year: {concept: value}}`` for a company compounding at ``growth``."""
    series = {}
    for index in range(years):
        factor = (1 + growth) ** index
        series[first_year + index] = {
            concept: value * factor if concept in _GROWING else value
            for concept, value in BASE_YEAR.items()
        }
    return series


def price_frame(
    start: str = "2005-01-01",
    end: str = "2026-06-30",
    price: float = 100.0,
    drift: float = 0.0,
    splits=None,
):
    """What ``market._history`` returns: a yfinance OHLCV frame plus ``AS_TRADED``.

    Built through ``with_as_traded`` rather than by hand, so a test never sees a
    frame shaped differently from the one the fetcher actually produces.
    """
    import pandas as pd

    from tradingagents.value.screen.market import with_as_traded

    index = pd.bdate_range(start=start, end=end)
    closes = [price * (1 + drift) ** step for step in range(len(index))]
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [1_000_000] * len(index),
        },
        index=index,
    )
    return with_as_traded(frame, splits)


def facts_for(series: dict[int, dict[str, float]]) -> list[Fact]:
    """Turn a financials mapping into store-shaped facts, filed the usual February."""
    return [
        Fact(
            concept=concept,
            fiscal_year=year,
            period_end=f"{year}-12-31",
            filed=f"{year + 1}-02-10",
            value=value,
            unit="shares" if concept == "DilutedShares" else "USD",
            source_tag=f"tag:{concept}",
            accn=f"acc-{year}",
        )
        for year, facts in series.items()
        for concept, value in facts.items()
    ]
