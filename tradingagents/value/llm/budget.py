"""Hard USD budget cap for LLM calls, backed by an append-only JSONL ledger.

Fail-closed by construction:

- a model with no price entry raises instead of costing $0;
- an unreadable or corrupt ledger raises instead of being read as $0 spent;
- exceeding a cap raises ``BudgetExceeded``, which the caller must let abort the
  run — there is no "continue anyway" path.

The charge is recorded *before* the cap is checked, because by the time we know
the token count the money has already left the wallet. That bounds an overrun to
a single call, which is the point: the caps exist to stop a runaway loop, not to
predict the cost of one request.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from ..config import (
    BUDGET_LEDGER_PATH,
    MODEL_PRICES_USD_PER_MTOK,
    MONTH_BUDGET_USD,
    RUN_BUDGET_USD,
)

_PER_MILLION = 1_000_000


class BudgetError(RuntimeError):
    """Base for budget failures. Always fatal to the run."""


class BudgetExceeded(BudgetError):
    """A USD ceiling was reached. The run must abort."""


class UnknownModelPrice(BudgetError):
    """No price is configured for the model, so its spend cannot be tracked."""


def cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """USD cost of one call. Raises for an unpriced model rather than returning 0."""
    try:
        price_in, price_out = MODEL_PRICES_USD_PER_MTOK[model]
    except KeyError:
        raise UnknownModelPrice(
            f"No price configured for model {model!r}; add it to "
            "MODEL_PRICES_USD_PER_MTOK before spending against it."
        ) from None
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError(f"Token counts must not be negative: {prompt_tokens}, {completion_tokens}")
    return (prompt_tokens * price_in + completion_tokens * price_out) / _PER_MILLION


class Budget:
    """Per-run and per-calendar-month USD ceilings over a shared ledger file.

    The run total lives in memory (one instance == one run); the month total is
    recomputed from the ledger so separate processes on the same server share it.
    """

    def __init__(
        self,
        ledger_path: Path | None = None,
        run_cap_usd: float | None = None,
        month_cap_usd: float | None = None,
    ) -> None:
        self.ledger_path = Path(ledger_path) if ledger_path else BUDGET_LEDGER_PATH
        self.run_cap_usd = RUN_BUDGET_USD if run_cap_usd is None else run_cap_usd
        self.month_cap_usd = MONTH_BUDGET_USD if month_cap_usd is None else month_cap_usd
        self.run_spend_usd = 0.0

    def month_spend_usd(self, month: str | None = None) -> float:
        """Total recorded spend for ``month`` (``YYYY-MM``, default: current UTC month)."""
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        if not self.ledger_path.exists():
            return 0.0
        total = 0.0
        with self.ledger_path.open(encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    at, usd = entry["at"], float(entry["usd"])
                except (ValueError, KeyError, TypeError) as exc:
                    # Fail closed: a ledger we cannot read is not a ledger of zero.
                    raise BudgetError(
                        f"Corrupt budget ledger {self.ledger_path}:{line_no}: {exc}"
                    ) from exc
                if at[:7] == month:
                    total += usd
        return total

    def charge(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Record one call's cost, then raise ``BudgetExceeded`` if a cap is breached."""
        usd = cost_usd(model, prompt_tokens, completion_tokens)
        now = datetime.now(timezone.utc)
        self._append(
            {
                "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "usd": usd,
            }
        )
        self.run_spend_usd += usd

        if self.run_spend_usd > self.run_cap_usd:
            raise BudgetExceeded(
                f"Run budget exceeded: ${self.run_spend_usd:.4f} > ${self.run_cap_usd:.2f}"
            )
        month_spend = self.month_spend_usd(now.strftime("%Y-%m"))
        if month_spend > self.month_cap_usd:
            raise BudgetExceeded(
                f"Monthly budget exceeded: ${month_spend:.4f} > ${self.month_cap_usd:.2f}"
            )
        return usd

    def _append(self, entry: dict) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
