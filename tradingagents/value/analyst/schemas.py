"""The tier-3 output schema.

Flat by requirement, not by preference (plan section 8): DeepSeek has no
``json_schema`` support, so structured output goes through function calling, and
nested models or dicts come back unreliably through that path. Everything here
is a scalar, an enum, or a ``list[str]``.

Enums rather than free text on every judgement that downstream code branches on.
A verdict of "probably fine, but note that..." is unusable by a filter; the prose
belongs in ``thesis``, where nothing branches on it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Verdict(str, Enum):
    """What tier 3 concludes about a name the numbers already passed."""

    PROCEED = "proceed"
    CAUTION = "caution"
    AVOID = "avoid"


class Moat(str, Enum):
    WIDE = "wide"
    NARROW = "narrow"
    NONE = "none"


class MoatTrend(str, Enum):
    """The reason tier 3 exists: criteria 1-13 read the past, not its direction."""

    STRENGTHENING = "strengthening"
    STABLE = "stable"
    ERODING = "eroding"


class Concentration(str, Enum):
    NONE_DISCLOSED = "none_disclosed"
    MODERATE = "moderate"
    SEVERE = "severe"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ValueAssessment(BaseModel):
    """One analyst's read of Items 1, 1A and 7 for one company."""

    ticker: str = Field(description="Ticker the assessment is about, uppercase.")
    verdict: Verdict = Field(
        description=(
            "proceed: nothing in the filing contradicts the numeric case. "
            "caution: a real concern that does not sink the thesis. "
            "avoid: the filing describes a business the numbers flatter."
        )
    )
    moat: Moat = Field(description="Durable competitive advantage evident in Item 1.")
    moat_trend: MoatTrend = Field(
        description="Direction of that advantage over the period Item 7 discusses."
    )
    customer_concentration: Concentration = Field(
        description="severe when a named customer or few customers carry a large revenue share."
    )
    accounting_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Specific accounting concerns in MD&A or the footnotes it references — "
            "revenue-recognition changes, one-off gains presented as operating, "
            "growing receivables against flat revenue. Empty when there are none."
        ),
    )
    key_risks: list[str] = Field(
        default_factory=list,
        description="Risks from Item 1A that are specific to this business, not boilerplate.",
    )
    thesis: str = Field(
        description="Two to four sentences: what the business is and what would break it."
    )
    confidence: Confidence = Field(
        description="low when the sections were thin, truncated, or mostly boilerplate."
    )
    evidence_gaps: list[str] = Field(
        default_factory=list,
        description="What the provided sections did not answer. Empty when nothing is missing.",
    )
