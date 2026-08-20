"""Tier 3: read Items 1, 1A and 7, return a validated ``ValueAssessment``.

The whole of the module's LLM spend passes through this one function, so three
things are non-negotiable here:

- **cached.** The key is the exact prompt, so a re-run of the daily job and a
  replay of the sampled backtest are both free (``llm/cache.py``).
- **budgeted.** Every miss is charged against the run and month ceilings, which
  fail closed (``llm/budget.py``).
- **no free-text fallback.** The repo's shared helper retries a failed structured
  call as prose, which is right for an agent that renders markdown and wrong
  here: the caller needs a typed verdict, and an unparsed answer is a missing
  one. It raises instead.

The system prompt is a byte-stable prefix so DeepSeek's automatic prompt caching
applies across names (plan section 8), and because the cache key is the prompt,
editing it invalidates every stored answer — intended, a stale answer to a
changed question is worse than a paid one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tradingagents.agents.utils.structured import NO_EXTERNAL_TOOLS
from tradingagents.llm_clients.factory import create_llm_client

from ..config import ANALYST_MODEL
from ..edgar.filings import Sections
from ..llm.budget import Budget
from ..llm.cache import LLMResult, cached_call
from .schemas import ValueAssessment

PROVIDER = "deepseek"

SYSTEM_PROMPT = (
    "You are a value analyst in the Buffett tradition. The company below has "
    "already cleared a thirteen-criterion numeric screen over ten years of "
    "financial statements, so do not re-derive the numbers: your job is the part "
    "the statements cannot show — whether the competitive advantage is eroding, "
    "whether the language of MD&A hides an accounting problem, and whether the "
    "business depends on a handful of customers.\n\n"
    "Judge the business over a five-to-ten year horizon, not the next quarter. "
    "Boilerplate risk factors are not risks; cite only what is specific to this "
    "filer. If a section is thin or truncated, say so in evidence_gaps and lower "
    "your confidence rather than inventing coverage.\n\n"
    # The operator reads these on a phone, in Vietnamese. Only the prose moves:
    # the enum fields are the ones `message.briefing` and every downstream filter
    # branch on, and a translated token fails schema validation — which this
    # module refuses to paper over with a free-text fallback.
    "Write every free-text field — thesis, accounting_flags, key_risks and "
    "evidence_gaps — in Vietnamese. The enum fields (verdict, moat, moat_trend, "
    "customer_concentration, confidence) must keep their exact English schema "
    "values; they are parsed, not read. Leave tickers, financial-statement line "
    "items and accounting terms of art in English wherever Vietnamese has no "
    "settled equivalent — a translated term the operator has to guess at is "
    "worse than the English one they already know.\n\n"
    f"{NO_EXTERNAL_TOOLS}"
)


class ValueAnalystError(RuntimeError):
    """The call completed but produced no usable assessment."""


def build_prompt(
    ticker: str,
    sections: Sections,
    numeric_summary: str,
    company_name: str = "",
) -> str:
    """The full prompt, deterministic in its inputs — it is also the cache key."""
    header = f"{ticker}" + (f" — {company_name}" if company_name else "")
    dropped = (
        "; ".join(f"{name}: {chars:,} characters dropped" for name, chars in sections.dropped)
        or "none"
    )
    missing = ", ".join(sections.missing) or "none"
    # Appended only when there is something to say. This prompt is the cache key,
    # so an unconditional line would invalidate every assessment already paid for.
    suspect = (
        "\nExtraction may be wrong — " + "; ".join(sections.suspect)
        if sections.suspect else ""
    )

    return "\n".join(
        (
            SYSTEM_PROMPT,
            "",
            f"## Company\n{header}",
            "",
            f"## Numeric screen result\n{numeric_summary}",
            "",
            f"## Extraction notes\nTruncated: {dropped}\n"
            f"Sections not found: {missing}{suspect}",
            "",
            f"## Item 1 — Business\n{sections.business or '(not found in the filing)'}",
            "",
            f"## Item 1A — Risk Factors\n{sections.risk_factors or '(not found in the filing)'}",
            "",
            f"## Item 7 — MD&A\n{sections.mdna or '(not found in the filing)'}",
        )
    )


def assess(
    ticker: str,
    sections: Sections,
    numeric_summary: str,
    *,
    company_name: str = "",
    llm: Any | None = None,
    model: str = ANALYST_MODEL,
    budget: Budget | None = None,
    cache_dir: Path | None = None,
) -> ValueAssessment:
    """Return the assessment for one company, from cache when the prompt repeats.

    ``llm`` accepts an already-built chat model; left as ``None`` one is created
    for ``model`` through the shared factory.
    """
    prompt = build_prompt(ticker, sections, numeric_summary, company_name)

    def call() -> LLMResult:
        return _invoke(llm if llm is not None else _chat_model(model), prompt, model, budget)

    result = cached_call(PROVIDER, model, prompt, call, cache_dir=cache_dir, budget=budget)
    return ValueAssessment.model_validate_json(result.text)


def _chat_model(model: str) -> Any:
    return create_llm_client(PROVIDER, model).get_llm()


def _invoke(llm: Any, prompt: str, model: str, budget: Budget | None) -> LLMResult:
    """One structured call, returning the JSON and what it cost in tokens."""
    # include_raw, because the parsed model alone carries no usage metadata and a
    # call whose cost is unknown cannot be charged against the cap.
    structured = llm.with_structured_output(ValueAssessment, include_raw=True)
    response = structured.invoke(prompt)

    usage = getattr(response.get("raw"), "usage_metadata", None) or {}
    try:
        prompt_tokens = int(usage["input_tokens"])
        completion_tokens = int(usage["output_tokens"])
    except (KeyError, TypeError, ValueError) as exc:
        # An uncharged call is exactly how a budget cap stops capping.
        raise ValueAnalystError(
            f"Response carried no usable token usage ({usage!r}); cannot charge the budget."
        ) from exc

    parsed = response.get("parsed")
    if parsed is None:
        # The tokens were spent whether or not the answer parsed, and the cache
        # charges only what it stores — so an unparsed answer is charged here.
        # Otherwise a loop of malformed responses runs against no ceiling at all.
        if budget is not None:
            budget.charge(model, prompt_tokens, completion_tokens)
        error = response.get("parsing_error")
        raise ValueAnalystError(
            f"DeepSeek returned no parsed ValueAssessment ({error or 'no tool call'}). "
            "Refusing to fall back to free text: the caller needs a typed verdict."
        )

    return LLMResult(
        text=parsed.model_dump_json(),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
