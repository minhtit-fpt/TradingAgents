# Phase 7 — the human decides the entry

Deliverable: `tradingagents/value/report.py`. Tests: `tests/value/test_report.py`.
Branch: `feat/value-p7-dossier`.

## Why this phase exists

Phases 4b and 6 closed the automated path:

- 4b: the numeric strategy does not beat SPY over 2014–2026.
- 6: a three-state LLM verdict applied at entry over the screen's own picks
  cannot be separated from a random veto of the same size.

Both failures are about *automation deciding when to buy*. Neither says the
screen's output is worthless — 4b's own book compounded at +11.98% CAGR, and
phase 6 found the filing reads to be specific and checkable (the Gillette
intangible, TXN's 219-day inventory). What failed was handing the decision to
the apparatus.

So this phase changes who decides. Subsystems 1 and 2 already work this way: the
operator picks a ticker and a date, the machine assembles evidence, the operator
decides. Phase 7 gives subsystem 3 the same shape — on demand, one name, no
alerting, no trigger firing on its own, no order anywhere.

## Requirements

1. One command, one ticker, one as-of date, everything the screen knows about it.
2. Show the *reasoning*, not the verdict: which criteria failed, in which years,
   at what values. A `passed: False` with no detail is not evidence.
3. Show the price the trigger would fire at, so the answer to "when do I enter"
   is a number rather than a wait for an alert.
4. The 10-K read is **opt-in and priced**, presented as reading material, never
   as a veto — phase 6 measured the veto and it does not earn its cost.
5. Every line says what it is and what it is not. No recommendation, no sizing,
   no execution.

## Non-goals

- No alerting (`alerts/` stays unbuilt; a human asking is the trigger now).
- No decision journal. Worth adding once there are decisions to review; today it
  would be a schema with no rows.
- No fix for the phase-6 `fetch_10k` pagination defect: it only bites on filings
  10+ years old, and this command reads the current one.

## Design

Single new module, `tradingagents/value/report.py`, plus its test. Nothing else
is touched — isolation contract intact, `tests/value/test_isolation.py` covers it.

```
build(conn, ticker, as_of, ...) -> Dossier      # assemble, no printing
render(dossier) -> list[str]                    # format, no I/O
main(argv) -> int                               # CLI
```

Reused as-is, nothing re-derived:

| need | existing |
|---|---|
| screen + valuation for one name | `screen.runner.screen_one` |
| per-criterion detail | `criteria.CriterionResult` (already carries years + values) |
| price, treasury rate | `screen.market` |
| 10-K sections | `edgar.filings.sections_for` |
| filing read | `analyst.value_analyst.assess` |
| cost cap, disk cache | `llm.budget.Budget`, `llm.cache` |
| facts for a name never ingested | `jobs.bootstrap.ingest` |

Four sections out:

1. **QUALITY** — every criterion, pass/fail, violation years, worst value.
2. **PRICE** — intrinsic value, MoS against the 30% trigger, the inputs that
   produced it (growth and whether it was capped, discount rate, terminal PE),
   Graham number and owner earnings as cross-checks.
3. **ENTRY** — the price at which MoS reaches the trigger, and the distance from
   today's price. This is the phase's actual answer.
4. **FILING** — only with `--read-filing`: verdict, moat, flags, risks, thesis,
   confidence, gaps, and the dollars spent.

Missing facts are stated, never defaulted. A name with no rows in the store is
ingested once (needs `VALUE_SEC_USER_AGENT`) or the command says so and stops.

## How to run

```bash
python -m tradingagents.value.report --ticker PG
python -m tradingagents.value.report --ticker PG --as-of 2024-06-30
VALUE_SEC_USER_AGENT='TradingAgents research you@example.com' \
python -m tradingagents.value.report --ticker PG --read-filing --budget 0.50
```

## Risk

The one real risk is the operator reading section 3 as a target price. It is not
one: it is the price at which *this model's* MoS reaches 30%, and the model is
the one phase 4b showed does not beat SPY. The render says so on the line.

## Not investment advice

This module produces research output. It executes nothing and recommends
nothing.
