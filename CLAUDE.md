# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

`TradingAgents` — a multi-agent LLM framework for financial analysis, built on
LangGraph. It produces **research reports and ratings**, not orders. Nothing in
this repository executes trades, and no part of it constitutes investment
advice.

Python ≥3.10. Entry points: the `tradingagents` CLI (`cli/main.py`) and
`main.py` for programmatic use.

## Subsystems — three, kept apart

| # | Subsystem | Location | Horizon |
|---|---|---|---|
| 1 | Stock graph | `tradingagents/graph/`, `tradingagents/agents/` | short / medium term |
| 2 | Crypto graph | same pipeline, fundamentals analyst filtered out (`cli/utils.py`) | short / medium term |
| 3 | **Value module** | `tradingagents/value/` *(phases 1-8 built)* | **long term** |

### Isolation rule (hard requirement)

Subsystem 3 is deliberately independent of 1 and 2. When working on it:

- **Do not edit existing files.** No new analyst in the graph, no new vendor in
  `VENDOR_METHODS` / `VENDOR_LIST`, no new key in `DEFAULT_CONFIG`, no change
  to `cli/`.
- All code lives under `tradingagents/value/`, tests under `tests/value/`.
- Import allowlist from the rest of the repo: `llm_clients.factory`,
  `llm_clients.capabilities`, `agents.utils.structured`. Nothing else — in
  particular **not** `graph.*`, `agents.analysts.*`, or `dataflows.interface`.
- It calls `yfinance` directly rather than routing through the shared vendor
  registry. Duplicating a small price fetch is the intended trade.
- Own env-var namespace `VALUE_*`; own state directory
  `~/.tradingagents/value/`.
- `tests/value/test_isolation.py` enforces this mechanically.

Conversely, when working on subsystems 1 or 2, do not reach into
`tradingagents/value/`.

**Plan for subsystem 3:**
[.claude/plans/long-term-value-investing.md](.claude/plans/long-term-value-investing.md).
Read it before touching anything under `tradingagents/value/`, together with the
phase findings beside it: `phase4-findings.md`, `phase4b-clean-universe.md`,
`phase6-llm-veto.md`, `phase7-human-entry.md`, `phase8-alerts-and-ledger.md`. Two
of them are stop conditions — the numeric strategy does not beat SPY (4b) and an
automated LLM veto at entry does not earn its cost (6) — so the module produces
evidence for an operator who decides, not signals that act.

Three surfaces, in the order they are used. The dossier answers "what do the
numbers say about this name, and at what price would it be cheap":

```bash
python -m tradingagents.value.report --ticker PG --read-filing
```

The daily job screens everything, alerts on names at the trigger, and sends one
heartbeat line whether or not anything fired — silence is this screen's normal
state, so the heartbeat is what separates a quiet market from a dead cron:

```bash
python -m tradingagents.value.jobs.daily --dry-run
```

The decision log is the only feedback loop the module has. Record the passes as
well as the buys — a journal holding only the purchases is survivorship applied
to yourself:

```bash
python -m tradingagents.value.decisions record --ticker PG --action pass --why "..."
```

Two rules that phase 8 rests on and that are easy to erode: alerts fire on
arithmetic and name no action, and nothing downstream of `alerts/` may write a
position. The tier-3 filing read is a **briefing, not a gate** — `verdict`
renders last everywhere because phase 6 measured it covering 70% of picks. Order
the fields in `alerts.message.briefing`, which the dossier reuses, so the two
surfaces cannot drift.

## Commands

```bash
pip install -e ".[dev]"
```

```bash
pytest
```

```bash
ruff check .
```

```bash
tradingagents
```

Tests are plain `unittest.TestCase` classes discovered by pytest
(`testpaths = ["tests"]`). Markers available: `unit`, `integration`, `smoke`.

Ruff: line length 100, `E W F I B UP C4 SIM`, `E501` ignored. Run it before
committing; whole-repo `ruff format` is deliberately not adopted yet.

## LLM provider

This deployment uses **DeepSeek**.

| Setting | Value |
|---|---|
| Provider key | `deepseek` (base URL `https://api.deepseek.com`) |
| API key env | `DEEPSEEK_API_KEY` |
| Quick model | `deepseek-v4-flash` |
| Deep model | `deepseek-v4-pro` |

DeepSeek quirks that matter when writing agents — the declarative table in
`tradingagents/llm_clients/capabilities.py` is the source of truth:

- **No `json_schema` support.** Structured output goes through
  `function_calling`, so Pydantic schemas must stay **flat** — scalars, enums,
  `list[str]`. Avoid nested models and dicts.
- **`tool_choice` is rejected** by the thinking models; the client suppresses
  the kwarg.
- Thinking models require the `reasoning_content` round-trip, handled by
  `DeepSeekChatOpenAI`.
- Context is limited relative to GPT-tier models. Long documents (10-K filings
  in particular) must be section-extracted and truncated before being sent.

Provider and models are configurable via `TRADINGAGENTS_*` env vars — see
`tradingagents/default_config.py`.

## Configuration

`DEFAULT_CONFIG` in `tradingagents/default_config.py` is the single source of
truth for subsystems 1 and 2. To expose a new key to the environment, add a row
to `_ENV_OVERRIDES` — do not edit entry-point scripts. Invalid env values raise
at startup rather than falling back silently; keep it that way.

Secrets live in `.env` (see `.env.example`), never in source and never baked
into the Docker image.

## Conventions

- Type annotations on function signatures; PEP 8.
- Prefer immutable data — `@dataclass(frozen=True)`, `NamedTuple`.
- Small focused files. Extract rather than grow past ~800 lines.
- Handle errors explicitly. **Never swallow an error into a default value** —
  this codebase feeds numbers to decision logic, and a silent fallback becomes
  a confidently wrong figure. The vendor-error taxonomy
  (`dataflows/errors.py`) and the "no data" guards exist for this reason.
- Look-ahead bias is a correctness bug, not a style issue. Any code touching
  historical data must filter on the as-of date. See
  `dataflows/stockstats_utils.py` and the `tests/test_*_lookahead.py` suite for
  the established pattern.
- Reuse what is already declared in `pyproject.toml` before adding a
  dependency. `backtrader`, `pandas`, `requests` and `yfinance` are already
  present.

## Commit style

```
<type>: <description>
```

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `perf`, `ci`.
Scope in parentheses where useful — e.g. `fix(dataflows): ...`, matching the
existing history.

Commit or push only when asked. Branch first if on `main`.
