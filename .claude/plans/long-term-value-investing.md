# Plan: Long-Term Value Investing Module (US Equities)

Status: **DRAFT — awaiting confirmation. No code written yet.**
Created: 2026-08-07
Target LLM provider: **DeepSeek** (`deepseek-v4-flash` / `deepseek-v4-pro`)

---

## 1. Requirements Restatement

Build a **third, independent subsystem** in this repository: a long-term,
Buffett-style value screener for US equities that reads financial statements
and sends Telegram alerts when a high-quality business trades at a ≥30% margin
of safety.

Confirmed decisions:

| Item | Decision |
|---|---|
| Market | **United States only.** AU / UK / CN deferred. |
| Data budget | **Free sources only.** SEC EDGAR + yfinance + FRED. |
| Universe | **All US 10-K filers** (~5–6k active; ~2.5–3k with 10y history). |
| MoS trigger | **30%** |
| Output | **Alerts only.** No order execution, no broker, no position management. |
| Alert channel | **Telegram** |
| Geopolitics / mid-term | **Deferred to a later phase.** Not in scope. |
| LLM budget | **~$100 total**, mostly reserved for the backtest tier. |
| Runtime | **Server**, Docker + host cron. |
| Isolation | **Hard.** See §2. |

Out of scope: crypto, non-US markets, order execution, position sizing
enforcement, geopolitical/macro regime analysis.

---

## 2. Isolation Contract (hard constraint)

The repository already contains two working subsystems:

1. **Stock graph** — short/medium-term multi-agent LangGraph pipeline
   (`tradingagents/graph/`, `tradingagents/agents/`).
2. **Crypto graph** — the same pipeline with the fundamentals analyst filtered
   out (`cli/utils.py: filter_analysts_for_asset_type`).

This module is a **third subsystem that must not perturb either**.

### Rules

- **Zero edits to existing files.** No new analyst registered into the graph.
  No new vendor registered into `VENDOR_METHODS` / `VENDOR_LIST`. No new keys
  added to `DEFAULT_CONFIG`. No changes to `cli/`.
- All new code lives under **`tradingagents/value/`** — a self-contained
  package with its own config, its own data store, its own entrypoint.
- **Import allowlist.** The module may import, read-only, from:
  - `tradingagents.llm_clients.factory` (DeepSeek client construction)
  - `tradingagents.llm_clients.capabilities` (structured-output method)
  - `tradingagents.agents.utils.structured` (structured-output helper)

  Nothing else. In particular it must **not** import `tradingagents.graph.*`,
  `tradingagents.agents.analysts.*`, or `tradingagents.dataflows.interface`.
- Prices: the module calls `yfinance` **directly**, not through
  `route_to_vendor`. Duplicating ~30 lines of a price fetch is the correct
  trade for keeping the vendor registry untouched.
- Own state directory: `~/.tradingagents/value/` (separate from `results_dir`,
  `data_cache_dir`, `memory_log_path`).
- Own env-var namespace: **`VALUE_*`**, never `TRADINGAGENTS_*`.
- Own tests: `tests/value/`. The existing test suite must stay green and must
  not need modification.

### Verification of isolation

`git diff --stat` for every phase must show **only** new files under
`tradingagents/value/`, `tests/value/`, `.claude/`, `CLAUDE.md`, and optionally
`docker-compose.yml` (new service block only — existing services untouched).
Any change to an existing Python file is a plan violation.

---

## 3. Architecture

```
tradingagents/value/
├── __init__.py
├── config.py                 # VALUE_* env vars, thresholds, paths
├── edgar/
│   ├── bulk.py               # companyfacts.zip download + streaming read
│   ├── daily_index.py        # incremental: new filings since last run
│   ├── concepts.py           # XBRL concept -> tag fallback chains
│   ├── filings.py            # 10-K section extraction (Item 1/1A/7)
│   └── client.py             # HTTP: User-Agent, 10 req/s limiter, retries
├── store/
│   ├── schema.sql            # SQLite DDL
│   └── db.py                 # facts, screen results, alert log, llm cache
├── screen/
│   ├── universe.py           # 10-K operating filers, exclude ETF/SPAC/20-F
│   ├── criteria.py           # Buffett numeric thresholds (pure functions)
│   ├── intrinsic.py          # equity-bond valuation + margin of safety
│   └── runner.py             # full-universe screen pass
├── analyst/
│   ├── schemas.py            # ValueAssessment (flat, DeepSeek-safe)
│   └── value_analyst.py      # qualitative pass, DeepSeek, structured output
├── llm/
│   ├── cache.py              # hash(provider+model+prompt) -> response on disk
│   └── budget.py             # token counter + hard USD cap, fail-closed
├── alerts/
│   ├── telegram.py           # requests POST, no extra dependency
│   ├── dedupe.py             # (ticker, trigger_date) cooldown
│   └── heartbeat.py          # daily "ran OK" + weekly near-miss digest
├── backtest/
│   ├── numeric.py            # tier-1 backtest, $0 LLM
│   └── llm_sample.py         # tier-3 backtest on a sampled event set
└── jobs/
    ├── bootstrap.py          # one-shot: bulk download + 10y backfill
    └── daily.py              # cron entrypoint: tiers 1 -> 2 -> 3
```

Entrypoints:

```bash
python -m tradingagents.value.jobs.bootstrap
python -m tradingagents.value.jobs.daily
```

---

## 4. Data Sources (all free)

| Need | Source | Auth | Notes |
|---|---|---|---|
| US financial statements, full history | SEC EDGAR `companyfacts` bulk zip | none | `User-Agent` with a real email is **mandatory**; 10 req/s cap |
| New filings, incremental | EDGAR daily index | none | drives the daily tier-1 pass |
| Filing dates (point-in-time) | EDGAR `submissions` | none | use `filed`, never the period-end date |
| 10-K narrative sections | EDGAR filing HTML | none | needs section extraction, see §8 |
| Prices | yfinance | none | direct call, not via vendor registry |
| Risk-free rate (discount rate) | FRED `DGS10` | free key | `FRED_API_KEY` already used by the repo |

Approximate sizes (**verify with a HEAD request before implementing**):
`companyfacts.zip` ≈ 1–2 GB compressed, ≈ 15 GB uncompressed. Read members
directly out of the zip with `zipfile`; never extract to disk. Persist only the
~22 concepts in §5 into SQLite (expected a few hundred MB).

---

## 5. XBRL Concept Mapping

The single hardest part of this module. The same economic concept is tagged
differently across companies and across years, and the us-gaap taxonomy changed
several times in the last decade. `concepts.py` defines an **ordered fallback
chain per concept**, plus a coverage report.

| Concept | Primary tag | Fallbacks (ordered) |
|---|---|---|
| Revenue | `RevenueFromContractWithCustomerExcludingAssessedTax` | `Revenues`, `SalesRevenueNet`, `SalesRevenueGoodsNet` |
| Cost of revenue | `CostOfRevenue` | `CostOfGoodsAndServicesSold`, `CostOfGoodsSold` |
| Gross profit | `GrossProfit` | derive: revenue − cost of revenue |
| SG&A | `SellingGeneralAndAdministrativeExpense` | `GeneralAndAdministrativeExpense` + `SellingAndMarketingExpense` |
| R&D | `ResearchAndDevelopmentExpense` | — (absent is legitimate) |
| D&A | `DepreciationDepletionAndAmortization` | `DepreciationAmortizationAndAccretionNet`, `Depreciation` |
| Operating income | `OperatingIncomeLoss` | derive |
| Interest expense | `InterestExpense` | `InterestExpenseDebt`, `InterestIncomeExpenseNet` (watch sign) |
| Income tax | `IncomeTaxExpenseBenefit` | — |
| Net income | `NetIncomeLoss` | `ProfitLoss` |
| Assets | `Assets` | — |
| Current assets | `AssetsCurrent` | — |
| Liabilities | `Liabilities` | derive: assets − equity |
| Current liabilities | `LiabilitiesCurrent` | — |
| Equity | `StockholdersEquity` | `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` |
| Long-term debt | `LongTermDebtNoncurrent` | `LongTermDebt`, `LongTermDebtAndCapitalLeaseObligations` |
| Retained earnings | `RetainedEarningsAccumulatedDeficit` | — |
| Treasury stock | `TreasuryStockValue` | `TreasuryStockCommonValue` |
| Operating cash flow | `NetCashProvidedByUsedInOperatingActivities` | `...ContinuingOperations` |
| Capex | `PaymentsToAcquirePropertyPlantAndEquipment` | `PaymentsToAcquireProductiveAssets` |
| Diluted shares | `WeightedAverageNumberOfDilutedSharesOutstanding` | `WeightedAverageNumberOfSharesOutstandingBasic` |
| Dividends paid | `PaymentsOfDividendsCommonStock` | `PaymentsOfDividends` |

**The coverage report is a deliverable, not a nice-to-have.** For every ticker
it records which concepts resolved, via which tag, for which fiscal years. A
silent tag miss is the most likely way this module produces confidently wrong
ROE figures.

Sign conventions and unit scaling (`USD`, shares) are normalized on write, not
on read.

### Store shape (SQLite, `~/.tradingagents/value/value.db`)

Dates are `TEXT` in `YYYY-MM-DD`. Synthetic illustration:

```
facts(cik, ticker, concept, fiscal_year, period_end, filed, value, unit, source_tag)
  1234567 | ACME | Revenue    | 2024 | 2024-12-31 | 2025-02-18 | 1000000 | USD | Revenues
  1234567 | ACME | NetIncome  | 2024 | 2024-12-31 | 2025-02-18 |  210000 | USD | NetIncomeLoss

screen_results(ticker, as_of, passed, failed_criteria, intrinsic_value, price, mos_pct)
  ACME | 2026-08-07 | 1 | "" | 142.50 | 96.10 | 0.3256

alerts(ticker, trigger_date, mos_pct, sent_at)
  ACME | 2026-08-07 | 0.3256 | 2026-08-07T02:41:09Z
```

`filed` — never `period_end` — is the field every point-in-time query filters on.

---

## 6. Screening Criteria

Derived from *"Warren Buffett and the Interpretation of Financial Statements"*.
All numeric, all pure Python, **zero LLM cost**. Thresholds live in `config.py`
so they can be tuned in backtest without code edits.

| # | Criterion | Threshold | Rationale |
|---|---|---|---|
| 1 | Gross margin | > 40%, sustained | durable competitive advantage |
| 2 | Net margin | > 20% | pricing power |
| 3 | SG&A / gross profit | < 30% | lean cost structure |
| 4 | R&D / gross profit | < 30% (0 allowed) | not dependent on reinvention |
| 5 | D&A / gross profit | < 10% | not capital-hungry |
| 6 | Interest expense / operating income | < 15% | not carried by debt |
| 7 | Long-term debt | < 4× annual net income | payable within 4 years |
| 8 | Debt / equity (treasury-adjusted) | < 0.8 | balance-sheet safety |
| 9 | ROE | > 15% (prefer > 20%) | capital efficiency |
| 10 | Retained earnings | monotonically rising | reinvestment works |
| 11 | Capex / net income | < 25% | the moat is not a factory |
| 12 | Net income trend | rising, no loss years | consistency |
| 13 | Treasury stock present | yes = positive signal | buybacks |

**The binding constraint is "sustained over 10 years", not the level itself.**
One good year is common; ten consecutive is rare. This is what narrows ~3,000
candidates to an expected ~50–150.

Configurable tolerance: allow N violation-years out of 10 (default N=1) so a
single recession year does not disqualify an otherwise excellent business.

### Intrinsic value

Buffett's "equity bond" framing:

1. Take 10-year diluted EPS history.
2. Fit a growth rate; **cap it** (default 15%) to avoid extrapolating a fluke.
3. Project EPS 10 years forward.
4. Terminal value from a normalized P/E (default: `min(historical median P/E, 15)`).
5. Discount at the 10-year Treasury yield (FRED `DGS10`), floored at 4%.
6. `MoS% = (intrinsic_value − price) / intrinsic_value`.

Also record a simpler sanity anchor (Graham number, EV / owner earnings) so an
obviously broken DCF is visible rather than silently trusted.

Owner earnings = net income + D&A − maintenance capex. Maintenance capex is
approximated as `min(capex, D&A)`; the approximation is recorded in the output
so it can be reviewed.

---

## 7. Three-Tier Runtime

One cron job per day, three tiers in sequence. Cost is concentrated where it
buys the most.

```
02:30 America/New_York  (after US close + buffer)
  ├─ Tier 1  EDGAR daily index -> new filings? -> re-screen those tickers    $0
  ├─ Tier 2  latest close vs cached intrinsic value -> MoS >= 30%?           $0
  └─ Tier 3  only triggered tickers -> value_analyst (DeepSeek) -> Telegram   $
```

Tier 3 exists because criteria 1–13 cannot see a moat eroding, an accounting
red flag in the language of MD&A, or a footnote about customer concentration.
It reads Item 1 (Business), Item 1A (Risk Factors) and Item 7 (MD&A) and
produces a flat `ValueAssessment`.

Expected volume: ~100 names pass the screen, but the number simultaneously at
≥30% MoS in a normal market is small — often zero, occasionally a dozen during
a drawdown. **Long stretches with no alerts are the expected, correct
behaviour**, which is exactly why the heartbeat in §9 is mandatory.

---

## 8. DeepSeek Specifics

| Item | Value |
|---|---|
| Provider key | `deepseek`, base URL `https://api.deepseek.com` |
| Env var | `DEEPSEEK_API_KEY` (already wired: `tradingagents/llm_clients/api_key_env.py:22`) |
| Quick model | `deepseek-v4-flash` |
| Deep model | `deepseek-v4-pro` |
| Structured output | **`function_calling`** — DeepSeek has **no** `json_schema` support (`tradingagents/llm_clients/capabilities.py:54`) |
| Tool choice | **not supported** on thinking models; the client suppresses the kwarg |
| Reasoning round-trip | required; handled by `DeepSeekChatOpenAI` |

Consequences for design:

- `ValueAssessment` must be a **flat** Pydantic model: scalars, enums, and
  `list[str]`. No nested models, no dicts, no unions beyond `X | None`. Deeply
  nested schemas are unreliable through the function-calling path.
- **Context limit is the real constraint.** A full 10-K is frequently 300k+
  tokens and will not fit. Section extraction in `edgar/filings.py` is
  therefore load-bearing, not an optimization: extract Items 1, 1A and 7,
  truncate each to a configured token budget, and record what was dropped.
- Keep the system prompt prefix byte-stable across calls so DeepSeek's
  automatic prompt caching applies.

### Cost model

DeepSeek is roughly an order of magnitude cheaper than GPT-tier models, so the
$100 budget is comfortable. **Exact per-token prices must be verified against
DeepSeek's current pricing page before phase 6.** Planning estimate:

| Workload | Runs | Est. cost |
|---|---|---|
| Tier 1 + Tier 2, daily, forever | ∞ | **$0** |
| Tier 3 live alerts | ~10–20 / quarter | a few dollars / year |
| Tier 1 backtest, 10y × full universe | millions of rows | **$0** |
| Tier 3 backtest, sampled events | 50–100 | small fraction of budget |

With DeepSeek the binding constraint is **rate limiting and wall-clock time,
not money**. Budget the cap anyway (§9) — a runaway loop is the only realistic
way to burn $100 here.

---

## 9. Operational Safeguards

Non-negotiable, because an alert-only system fails **silently**.

1. **Heartbeat.** Daily one-line Telegram message: ran OK, N tickers screened,
   M passed, K triggered, closest candidate at X% MoS. A dead cron and a quiet
   market are otherwise indistinguishable. $0.
2. **Weekly near-miss digest.** Names at 20–30% MoS. Shows the pipeline is
   alive and lets pressure build visibly. $0.
3. **Hard LLM budget cap.** Per-run and per-month USD ceilings in
   `llm/budget.py`. **Fail closed** — abort the run and alert; never silently
   continue.
4. **Idempotency.** The job may run twice (retry, reboot). Alert dedupe keyed on
   `(ticker, trigger_date)`, written **before** the Telegram send. The LLM cache
   makes a repeat run free.
5. **SEC politeness.** Real `User-Agent` with a contact email, hard 10 req/s
   limiter, exponential backoff. A blocked server IP loses the only data source.
6. **Coverage alarm.** If tag coverage for a ticker drops below a threshold,
   exclude it from screening rather than compute ratios from partial data.
7. **Secrets.** `.env` on the server, `chmod 600`, never baked into the image.
   Required: `DEEPSEEK_API_KEY`, `FRED_API_KEY`, `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_CHAT_ID`, `SEC_USER_AGENT`.

---

## 10. Backtest Design

### Tier-1 backtest (numeric, $0)

Replay the screen over 10 years of point-in-time data across the full universe.
Answers: does the criteria set actually select outperformers? Free, so it can
be re-run for every threshold change.

**Reuse `backtrader`** — it is already a declared dependency of this project
(`pyproject.toml`) and currently unused by the two existing subsystems. It
supplies the portfolio accounting, commission model and metrics. Do not write a
new backtest engine; `backtest/numeric.py` should be a strategy plus a data
feed over the point-in-time SQLite store.

Metrics: CAGR vs SPY, max drawdown, hit rate, average holding period, count of
names passing per quarter, sensitivity to each threshold.

### Tier-3 backtest (LLM, sampled)

Sample 50–100 historical trigger events, run `value_analyst` on the 10-K **as
filed at the time**, and measure whether the qualitative pass improved outcomes
over the numeric screen alone. If it does not, tier 3 is decoration and should
be cut.

### Three ways this backtest lies if unguarded

1. **Look-ahead via period-end dates.** Q4-2024 statements did not exist on
   2024-12-31; they were filed Feb–Mar 2025. Use EDGAR's `filed` date. *(The
   existing repo already guards look-ahead for prices and news — see
   `tradingagents/dataflows/stockstats_utils.py:214` — but this module must
   implement its own guard for fundamentals, since it deliberately does not
   share that code path.)*
2. **Survivorship bias.** yfinance has no prices for delisted companies. EDGAR
   retains their filings, so the universe can be built correctly, but the price
   series will be incomplete. **Print the bias estimate in every backtest
   report.** Never quietly report the surviving-names-only number.
3. **Restatements.** `companyfacts` returns restated figures. True
   point-in-time requires parsing original filings. Accepted limitation for
   now; must be stated in the report.

---

## 11. Deployment

The existing `Dockerfile` has `ENTRYPOINT ["tradingagents"]` — the interactive
CLI, with `tty: true` in compose. Unusable from cron.

Add a **new compose service** (existing services untouched):

```yaml
value-daily:
  build: .
  env_file: [.env]
  environment:
    - TZ=America/New_York
  volumes:
    - tradingagents_data:/home/appuser/.tradingagents
  entrypoint: ["python", "-m", "tradingagents.value.jobs.daily"]
  profiles: [value]
```

Host cron drives it:

```
30 2 * * * cd /srv/TradingAgents && docker compose --profile value run --rm value-daily >> /var/log/value.log 2>&1
```

`TZ=America/New_York` avoids a one-hour DST drift. Cron lives on the host, not
in the container: no supervisor, no cron-in-Docker.

Bootstrap is a separate manual one-shot (30–60 min), never part of cron.

Disk: ~20 GB free during bootstrap, ~5 GB steady state.

---

## 12. Phases

Each phase must leave the existing test suite green and must touch no existing
Python file.

| # | Phase | Deliverable | Acceptance | LLM cost |
|---|---|---|---|---|
| 0 | LLM cache + budget cap | `llm/cache.py`, `llm/budget.py` | repeat call hits disk, zero network; cap aborts fail-closed | $0 |
| 1 | EDGAR ingest | `edgar/*`, `store/*` | 10y facts for 100 sample tickers in SQLite + coverage report | $0 |
| 2 | Screen + valuation | `screen/*` | full-universe pass completes; ~50–150 names with MoS | $0 |
| 3 | **Numeric backtest** | `backtest/numeric.py` | 10y report incl. survivorship-bias note | $0 |
| 4 | *(decision gate)* | — | **review tier-1 results before spending on LLM** | — |
| 5 | Value analyst | `analyst/*` | flat `ValueAssessment` validates via DeepSeek function-calling | small |
| 6 | LLM backtest | `backtest/llm_sample.py` | 50–100 events; verdict on whether tier 3 earns its cost | small |
| 7 | Alerts | `alerts/*` | Telegram alert + heartbeat + weekly digest; dedupe verified | $0 |
| 8 | Deploy | `jobs/*`, compose service | cron runs headless; two consecutive runs produce no duplicate alert | $0 |

**Phase 4 is a real stop.** If the numeric screen shows no edge, stop the
project there and spend nothing.

---

## 13. Test Plan

`tests/value/`, pytest, mirroring the existing suite's style (plain
`unittest.TestCase` classes are used throughout this repo).

- `test_concepts.py` — tag fallback chains; a company that only ever used a
  deprecated tag still resolves.
- `test_criteria.py` — each of the 13 criteria at boundary values; the
  violation-tolerance parameter.
- `test_intrinsic.py` — known-input valuation; growth-rate cap engages; the
  discount-rate floor engages.
- `test_point_in_time.py` — a fact filed after the as-of date is invisible.
  **The single most important test in this module.**
- `test_universe.py` — ETFs, SPACs and 20-F filers are excluded.
- `test_cache.py` — identical prompt → no second network call.
- `test_budget.py` — exceeding the cap aborts and does not silently continue.
- `test_dedupe.py` — the same `(ticker, date)` alerts once across repeated runs.
- `test_isolation.py` — **imports of `tradingagents.graph`,
  `tradingagents.agents.analysts` and `tradingagents.dataflows.interface` are
  absent from `tradingagents/value/`.** Enforces §2 mechanically.

---

## 14. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| XBRL tag chaos yields wrong ratios silently | **HIGH** | fallback chains + mandatory coverage report + exclude low-coverage tickers |
| 10-K exceeds DeepSeek context | **HIGH** | section extraction is load-bearing; record what was truncated |
| Survivorship bias inflates backtest | **HIGH** | build the universe from EDGAR incl. dead filers; print the bias estimate in every report |
| Silent cron death | **HIGH** | daily heartbeat (silence is otherwise normal) |
| Look-ahead via period-end dates | **HIGH** | use the `filed` date; dedicated test |
| SEC blocks the server IP | MEDIUM | real User-Agent, 10 req/s limiter, backoff |
| Restated figures ≠ point-in-time | MEDIUM | accepted, documented in reports |
| Screen passes ~0 names (too strict) | MEDIUM | violation tolerance is configurable; tune in the free tier-1 backtest |
| Bulk zip size / disk pressure | LOW | stream from the zip, never extract; verify size with HEAD first |
| DeepSeek rate limiting | LOW | the existing `llm_max_retries` pattern; tier-3 volume is tiny |

**Estimated complexity: HIGH** — driven by phases 1–3 (data engineering), not
by the LLM work.

---

## 15. Open Items

1. Exact `companyfacts.zip` size — verify with a HEAD request in phase 1.
2. Current DeepSeek per-token pricing — verify before phase 6.
3. Violation tolerance default (N=1 of 10) — settle empirically in phase 3.
4. Terminal P/E cap (15) and growth cap (15%) — settle empirically in phase 3.
5. Whether tier 3 justifies its cost — answered by phase 6.

---

## 16. Not Investment Advice

This module is research tooling. It produces screening output and alerts, never
orders. It does not constitute financial or investment advice, and the same
disclaimer that governs the rest of this repository applies.

---

## 17. Confirmation

**WAITING FOR CONFIRMATION.** No code will be written until this plan is
approved. Reply with:

- `yes` / `proceed` — begin phase 0
- `modify: ...` — request changes
- `skip phase N` — reorder
