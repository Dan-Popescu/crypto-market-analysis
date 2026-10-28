# Crypto Market Analysis: Cloud Implementation Brief

Prepared: 2026-10-28  
Repository: https://github.com/Dan-Popescu/crypto-market-analysis  
Reviewed baseline: `e81a4ed5e2a05a8c1328686314b067038e733b92`  
Status: implementation request; the roadmap below is not implemented yet.

## 1. Assignment and completion boundary

Implement the complete roadmap in this document. Start by inspecting the current
repository and reconciling changes since the reviewed baseline. Perform the
English-only migration first, then implement Phases 1 through 6 in their stated
order. Introduce verification with each phase; Phase 4 is not a reason to defer
tests for earlier work.

Continue through implementation, appropriate tests, documentation and reviewable
delivery. A plan, mockup, collection of stubs, or successful synthetic example
alone does not complete this request. If a genuine external dependency blocks a
feature, finish independent work and identify exactly what remains unsupported.
Do not describe blocked or unverified functionality as working.

This brief conveys the user's newer requirements. In particular, its language
policy supersedes earlier requests for French repository documentation or French
software output in `AGENTS.md`, `docs/SPEC.md`, prompts and tests.

## 2. Product goal

Build a useful analysis tool for gradual BTC and ETH spot purchases over weeks
and months. Help the user understand possible purchase windows and compare the
effect of purchase timing on average acquisition cost and outcomes over several
months. BTC and ETH are the confirmed initial asset scope.

The main objective is not minute-by-minute trading. A secondary, exceptional
4-hour or 8-hour vigilance mode may highlight unusual market fragility. It must
remain subordinate to the longer-term accumulation workflow.

Keep these time dimensions separate:

| Dimension | Initial scope | Purpose |
| --- | --- | --- |
| Historical context | Weeks, months, 1 year and 2 years; older history when available | Describe the current price in a broader market history. |
| Purchase window | 1, 3, 7 and 14 days | Examine declines that could change the conditions of a next purchase. |
| Outcome assessment | 90 and 180 days | Measure the subsequent outcome of an explicitly defined purchase plan. |
| Exceptional vigilance | 4 and 8 hours | Describe unusual observed fragility and expire the assessment promptly. |

A calendar day ending lower is not equivalent to a price becoming cheaper than
the price available when an analysis was issued. A lower average acquisition
cost is not, by itself, proof of a better overall result. Future price recovery
must never be assumed. Include flat markets and prolonged declines in evaluation.

Personal budgets, current positions and acquisition histories have not been
provided. Keep them configurable, optional and local. Use clearly synthetic
examples in the public repository.

## 3. English-only repository policy

Use English for all first-party repository content and software output:

- Documentation, headings, examples, setup instructions and agent instructions.
- Directory and file names, variables, functions, classes and test names.
- Comments, docstrings, type descriptions and authored fixture descriptions.
- CLI help, error messages, logs, report prose, chart titles, axes and legends.
- MCP descriptions, instructions, generated artifacts and authored prompt text.
- Public schema fields and enum values, commit messages and pull request text.

The commit-message and pull-request language policy applies to new work. Preserve
existing Git history; do not rewrite historical commits to translate their text.

Update `AGENTS.md` so future work maintains this policy. Remove conflicting
French-output instructions elsewhere in the repository. Do not mechanically
rename identifiers that are already clear English.

Preserve the meaning, dates, provenance and uncertainty of translated documents.
The original validation report and the independently executed import checks must
remain distinguishable after translation. Do not turn an author's reported test
result into a new validation claim.

Translate first-party text in generated synthetic examples or regenerate those
examples deterministically. Do not translate or rewrite raw external evidence,
provider identifiers, instrument symbols, official proper names or source URLs.
If externally supplied text contains another language, distinguish that raw
content from the application's English explanation.

Inventory externally visible and persisted strings before renaming them. Changes
to enum values, schema fields, database values, commands or import paths require
an explicit compatibility decision, versioning or migration, and focused tests.
English becomes the canonical output. Keep a documented legacy input alias only
where necessary to read an existing format without data loss.

Existing identifiers such as `OBSERVER_*`, `observer.toml`, `observer.sqlite` and
`observer:read` are already English technical identifiers. Retain their current
compatibility unless a justified migration is implemented. The application and
package names remain `Crypto Market Analysis`, `crypto-market-analysis` and
`crypto_market_analysis`.

## 4. Verified starting point and known gaps

The repository is public. GitHub Actions was disabled at the last check on
2026-10-28. The branch prefix is `feat/`. Refresh these facts before acting.

The imported code already provides public-data collectors, versioned SQLite
storage, source coverage, experimental forecasts, charts, CLI access and local
STDIO/HTTP MCP access. Real, imported and synthetic data modes are separate.
The host assistant explains results; collection, calculations and charts do not
require an embedded language model.

At import, 71 tests passed, Ruff and the offline lock check passed, and a renamed
wheel was built. These are dated software checks, not proof of a predictive edge,
investment performance, continuous operation or a working user-account connection.
Rerun the relevant baseline checks in the Cloud environment.

Inspect these existing paths before designing replacements:

| Path | Relevant responsibility |
| --- | --- |
| `src/crypto_market_analysis/research.py` | Daily bars, descriptive analysis, historical features, barrier outcomes, forecasts and evaluation. |
| `src/crypto_market_analysis/engine.py` | Shared analysis interface, selected market, data bundles, freshness and charts. |
| `src/crypto_market_analysis/collectors.py` | Exchange candles, order-book summaries, funding and open interest. |
| `src/crypto_market_analysis/providers.py` | Options and macro/network/stablecoin context. |
| `src/crypto_market_analysis/storage.py` | Versioned observations, candles, runs and imported events. |
| `src/crypto_market_analysis/retention.py` | Order-book summary retention and hourly compaction. |
| `src/crypto_market_analysis/cli.py` and `server.py` | CLI and MCP contracts and horizon validation. |
| `src/crypto_market_analysis/charts.py` | Deterministic chart rendering. |
| `tests/` and `tools/check.py` | Existing behavioral tests and shared verification command. |
| `docs/SPEC.md`, `DATA_CATALOG.md`, `IMPORT.md`, `VALIDATION.md` | Existing scope, data limitations and dated evidence. |

Known gaps at the reviewed baseline:

1. Descriptive price returns cover 7, 21, 30 and 60 days; forecasting features use
   approximately 21- and 60-day price/volatility information.
2. Interface-level forecast/evaluation horizons stop at 60 days, even though the
   underlying Python calculation accepts positive horizons.
3. There is no complete budget, contribution schedule, purchase-plan accounting
   or acquisition-cost comparison engine.
4. Existing forecast evaluation measures event predictions, not investment-plan
   outcomes against regular purchases.
5. A liquidity observation collected every five minutes can remain marked fresh
   for three hours because individual metric freshness uses the hourly market
   cadence. This was reproduced with a 30-minute-old observation.
6. The forecast stale flag only changes after two days. That is unsuitable as a
   general permission to use an assessment for a short purchase window or alert.
7. Order books are reduced to spread, depth and imbalance summaries. Detailed
   persistent walls, executable order simulation and a validated liquidation
   cascade warning are not implemented.
8. The supplied strict `known_at` evaluation had no eligible historical origins
   for history first downloaded after those dates. Its retrospective counterpart
   does not establish a predictive edge.

Do not rebuild the application from scratch. Extend the shared analytical
interface and reuse the existing provenance, versioning and rendering behavior.

## 5. Phase 0: English migration and baseline verification

Complete the language migration described above before new product work.

1. Inventory authored French text, output strings and ambiguous public contracts.
2. Translate documentation and software output, including charts and MCP prompts.
3. Rename French identifiers or paths if found, updating imports and references.
4. Document and test any compatibility migration; preserve existing local data.
5. Establish and record the current dependency, lint and test baseline.

Acceptance:

- First-party documentation, code identifiers and generated software messages
  are English. Review runtime strings as well as Markdown files.
- CLI help, an offline report, an error response and a chart contain English
  authored text. Test fixtures no longer require French canonical output.
- Existing relevant behavior remains covered and dependency locking is coherent.
- `AGENTS.md` states the English-only policy and the `feat/` branch prefix.

## 6. Phase 1: Reliable data and long-horizon market context

### Implementation

Correct freshness before adding more interpretation. Define cadence-aware age
and expiration for each metric family: price, liquidity, derivatives, options,
macro and network data. Expose event time and ingestion time separately. A new
ingestion timestamp must not make an old market observation fresh. Handle
calendar-based sources according to their actual release schedule.

Represent missing, stale, partial, inaccessible and insufficient data explicitly.
An unavailable or stale input must not silently become a neutral market signal.
Keep access failures visible; bounded retries must not hide source restrictions.

Extend descriptive context to windows such as 7, 21, 30, 60, 90, 180, 365 and
730 days. Provide a small, explained set of measures:

- Price return over each available window.
- Decline from the observed high in a named lookback window.
- Position within an observed historical price range.
- Observed volatility and volume, with clearly defined comparisons.
- Daily, weekly and monthly context, using complete calendar periods where
  calendar comparisons are intended.

Every measure needs its formula, unit, source, quote currency, lookback period,
effective coverage and current observation time. A low historical rank is a
descriptive fact, not an automatic undervaluation or purchase label.

Support bounded historical collection or explicit imports for the required
history. Prefer a small set of usable reference markets over indiscriminate
collection from every configured exchange. Show the history actually obtained;
requested history is not obtained history. Preserve revisions and idempotence.

Keep the historical reference market distinct from a venue used to estimate
purchase conditions. Do not silently choose the longest history as if it were
the user's execution venue. Keep USD, EUR, USDT and USDC separate; any conversion
needs an explicit, dated rate and its provenance.

### Acceptance

- A recent complete fixture supports short windows; unavailable long windows
  are reported as unavailable, not calculated from a shorter substitute.
- A two-year fixture yields reproducible long-window measures. Gaps, incomplete
  periods, future observations and source/quote mismatches are tested.
- A 30-minute-old liquidity point is not presented as fresh under a five-minute
  collection policy. The configured cadence and expiration are observable.
- Reingesting old data does not refresh its event age. A failed collection does
  not replace missing observations with zero or synthetic data.
- Long-context charts show source, quote, time, observed coverage and data mode.

## 7. Phase 2: Purchase-window and useful-decline analysis

### Implementation

Expose distinct events rather than a single ambiguous red/green forecast:

| Event | Required definition |
| --- | --- |
| Calendar down day | Explicit calendar/timezone and comparison between that day's open and close. |
| Negative endpoint return | Price at a specified future horizon below the issuance anchor. |
| Below-anchor price threshold | Price falling a specified percentage below that anchor at any time after issuance within the window. |

Support configurable purchase windows initially covering 1, 3, 7 and 14 days,
and a short configurable set of decline thresholds. Values such as 2%, 5% and
10% may be engineering examples, not user-specific recommendations or optimized
defaults presented as proven.

Show the anchor price, its market timestamp, the actual issuance timestamp,
source, quote, evidence coverage, event definition and assessment expiration.
If the anchor is a completed hourly close, label it accordingly. Do not call it
a current executable purchase price.

Reuse or carefully extend the existing empirical historical comparison. Return
the conditional frequency, unconditional reference frequency, raw and effective
sample counts, period coverage and uncertainty where supported. Return an
insufficient-data result when the method lacks support. Do not invent a calibrated
probability or convert a descriptive indicator into one.

Where data support it, measure time to first threshold crossing at the available
resolution. Hourly or daily observations do not justify exact minute-level times.
Never count a threshold touch before the assessment was actually issued. The
existing first-period extreme can include a pre-issuance move and needs correction
or an explicitly excluded/ambiguous result.

Maintain the distinction between a threshold touched in a candle and an order
that could actually have filled. If both an upper and lower barrier occur in a
single coarse bar, their order is unknown unless finer data resolve it.

### Acceptance

- A temporary decline followed by a higher close is distinguished from a down
  endpoint; a red calendar candle can still finish above the issuance anchor.
- Tests exclude pre-issuance touches, incomplete windows and future-data leakage.
- Unresolvable crossing times and barrier ordering remain explicitly unknown.
- Sparse evidence, stale prices and unsupported thresholds do not produce a
  confident purchase signal. Reference frequencies remain visible.

## 8. Phase 3: Gradual-purchase plan comparison

### Implementation

Build a deterministic, read-only purchase-plan engine with shared cash accounting.
It must accept a synthetic or local configuration containing:

- Asset, reference quote, assessment date and optional starting holdings.
- Dated contributions and funds actually available at each date.
- Purchase frequency, target amounts and an explicit fee model.
- A bounded purchase-delay policy, reserve limits and a maximum waiting time.
- Explicit price/execution assumptions, market source and data resolution.

Implement these initial plan families through the same accounting interface:

1. Fixed-amount regular purchases as the baseline.
2. Purchases split across explicitly configured below-anchor price levels.
3. A bounded deferral of part of a scheduled purchase, with a defined deadline
   and an explicit fallback when the target price is never reached.

Keep optional rules simple and documented. Do not add leverage, order execution
or a large strategy-optimization framework. Use exact decimal accounting for
cash, quantities and fees; floats may remain appropriate for statistical measures
and charts, not reconciliation identities.

Separate average price of a simulated order from the cost of all accumulated
holdings. Existing acquisition history may be unknown. Show a cost for the new
simulated purchases separately instead of inventing a complete portfolio cost.
Ledgerly remains the independent source of actual holdings and transaction costs.
An optional local input/export contract is sufficient; do not require Ledgerly
credentials, private records or a live connection to complete this phase.

Account for spread, fees and slippage according to available evidence. Only use
order-book-based fill estimates when sufficient levels are available; the existing
aggregated depth summaries cannot reconstruct a full fill curve. Historical
execution without a contemporaneous book needs an explicit, configurable cost
assumption. Do not label it measured execution.

For coarse bars, apply a documented conservative fill policy or expose bounds
when intrabar ordering is ambiguous. Never use a bar's future low to decide at
its open. Keep level touches distinct from simulated fills.

### Required outputs

- Contributions, amount spent, fees and remaining reserve.
- Quantity acquired and average acquisition cost with a documented fee convention.
- Simulated purchase history, waiting duration and expired purchase targets.
- Value of the combined crypto holdings and remaining reserve.
- Outcome at 90 and 180 days when that future history is available.

Define those terminal timestamps from the recorded assessment/plan-start time.
Compared plans must use identical terminal timestamps, contribution cutoffs,
valuation sources and quote currencies. If per-purchase cohort outcomes are also
reported, label their distinct start dates and keep them separate from the
whole-plan outcome.

Treat a non-interest-bearing reserve as the transparent base case. A different
reserve-return assumption must be explicit and sourced; a stablecoin is not
automatically equivalent to risk-free cash. For an unfinished future horizon,
report a pending outcome rather than an invented valuation.

### Acceptance

- Cash and quantity reconcile independently with deposits, purchases and fees.
- No plan spends future contributions, reuses spent cash or creates a negative
  reserve without an explicitly supported funding mechanism.
- At identical cashflows and costs, the regular baseline is reproducible.
- Tests include persistent rises, prolonged declines, flat prices, never-touched
  targets, insufficient book coverage, fees charged in different assets, missing
  starting cost and incomplete 90/180-day outcomes.

## 9. Phase 4: Validate usefulness and preserve issued analyses

### Implementation

Evaluate event prediction quality and purchase-plan outcomes separately.
Preserve the existing chronological checks and compare each method with an
appropriate simple reference. Fix rules before evaluating a held-out period;
do not choose parameters on the same outcomes later reported as validation.

Compare purchase plans with identical assets, quote currency, contributions,
fund-availability dates, assessment dates, fees and execution assumptions.
Distinguish periodic new income from a lump sum already available at the start.
Use several market periods rather than selecting only successful examples.

Report:

- Quantity acquired and average acquisition cost net of stated costs.
- Total terminal value of holdings plus reserve at 90 and 180 days.
- Outcome difference against regular purchases, including its dispersion and
  cases where the proposed method performs worse.
- Reserve left uninvested, its waiting duration and missed rising-market exposure.
- Maximum drawdown using a documented method that handles external contributions
  correctly. Cash deposits are not investment gains or recovery from drawdown.
- Forecast quality against base frequencies, with calibration and sample support
  reported honestly rather than one directional-accuracy percentage.

Respect the distinction between `known_at` and retrospective data. Historical
observations downloaded today are not proof of what this system knew then.
Macro revisions and unavailable historical publication times cannot silently
enter a point-in-time backtest. Empty strict validation must remain empty.

Do not merely raise forecast horizons to 90/180 days and claim adequate support.
At the reviewed baseline, 20 non-overlapping outcome windows are required. The
current selection needs approximately `62 + 20 * horizon_days` complete days:
1,862 days for 90-day frequencies and 3,662 for 180-day frequencies. Two years of
context therefore cannot support those frequencies with the existing method.
Treat 90/180 days primarily as plan-assessment horizons. If the statistical
method changes, justify its dependence assumptions and effective sample size;
do not silently weaken the evidence requirement.

Add an append-only record of analyses actually issued: issuance time, input
watermark/fingerprint, selected source and quote, rule/configuration version,
event definitions, forecasts or insufficient-data result, and expiration.
Attach later observed outcomes without rewriting the original assessment.
Keep these runtime records local and excluded from the public repository.

Make issuance/persistence an explicit operation with an idempotency key. Preserve
honest read-only behavior for existing MCP lookup/calculation tools; a tool that
saves an assessment must declare its local write behavior correctly. Retrying an
issuance must not create duplicate records or alter an already issued assessment.

### Acceptance

- Changing future data cannot change an earlier issued assessment or its inputs.
- A backfilled series does not manufacture strict historical availability.
- Comparisons account for uninvested funds and expose both underperformance and
  improvement. Lower acquisition cost alone cannot label a plan superior.
- Overlapping samples are not counted as independent observations without a
  justified treatment. Rare events retain visible uncertainty.
- A saved assessment and plan can be reproduced from its recorded version,
  configuration and input reference. Pending outcomes stay pending.

## 10. Phase 5: Decision-relevant market context

### Implementation

Extend the existing providers only where the information changes a useful
analysis. Reuse existing collectors before introducing additional integrations.
Prioritize:

1. A sourced calendar of major scheduled releases, including inflation releases
   and central-bank decisions, with explicit timezone and publication time.
2. Changes in open interest and funding, with native units, contract conventions,
   funding intervals and actual/projected values kept distinct.
3. Observed aggressive spot buying/selling where the venue publishes the required
   executed-volume data. Open interest is not spot buying pressure.
4. ETF flow context when its methodology and source are verifiable. Distinguish
   net flows from changes in assets under management caused by price changes.
5. A small number of available network metrics with documented interpretation.
   Global stablecoin supply is not an attributed BTC or ETH capital inflow.

Keep the initial implementation on public, bounded, unpaid data endpoints.
Verify current primary documentation, access and redistribution conditions.
Do not commit licensed data dumps merely because the endpoint is accessible.
Use deterministic provider fixtures that are synthetic or demonstrably shareable.

Manual imports may remain a clearly labelled fallback when no adequate automatic
source is verified. Their existence must not be described as an automated ETF
feed or a complete event calendar. Source failures and coverage limits belong in
the analysis, not only in hidden logs.

Explain supportive observations, adverse observations and contradictions. Avoid
a composite buy/sell score with unexplained weights. Additional context remains
descriptive until its incremental predictive value has been validated against
the same baseline and point-in-time constraints as other inputs.

### Acceptance

- Tests cover release-time boundaries, timezone conversion, revised macro data,
  funding-period mismatches, unknown open-interest units and partial providers.
- Imported, projected, measured and missing values remain distinguishable.
- ETF price revaluation is not treated as net inflow. Global supply changes are
  not labelled an asset-specific flow.
- Every included observation has provenance, age, unit and interpretation limits.
  An unavailable integration is explicitly identified as unavailable.

## 11. Phase 6: Exceptional 4-hour/8-hour vigilance

### Implementation

Add a lightweight, opt-in observational mode for unusual fragility. It is not a
promise to predict a liquidation cascade or a recommendation to open a position.

Combine fresh observations such as rapid price changes, withdrawal of displayed
depth, wider spreads, derivatives positioning and observed liquidations where
a suitable feed exists. Open interest and funding alone do not reveal all traders'
entry prices, leverage or liquidation levels. A partial feed is not a complete
liquidation map.

Define baseline windows and configurable thresholds before evaluation. Each
assessment must return the reasons, supporting observations, contradictory
evidence, missing-source flags, issuance time and expiry. Avoid numeric crash
probabilities unless separately validated.

Use explicit normal, elevated-vigilance and expired states, or an equivalently
small state model. Include cooldown, deduplication, recovery and automatic expiry.
An unchanged or non-actionable state should remain quiet. If tighter collection
is needed during a watch period, bound its duration, frequency and storage.

Tracking large displayed levels over successive snapshots may be added when it
helps the explanation. Do not claim to identify the same trader or prove spoofing
from aggregate levels. Do not build a full tick archive for this task.

Implement and test the optional alert output contract, but do not activate a
permanent background collector, external messaging service, scheduled automation
or public deployment as a side effect of a code change. Those operational actions
need their own deployment configuration and authorized execution context.

### Acceptance

- Synthetic stress cases show the actual reasons for elevated vigilance.
- Stale or missing inputs degrade or invalidate the assessment rather than
  fabricating a risk-free or high-confidence result.
- Repeated identical observations do not generate repeated alerts; recovery,
  expiration and bounded watch periods are tested.
- No code path executes orders, signs wallet operations or transfers funds.

## 12. Shared CLI, MCP, reports and charts

Keep the application convenient to use from ChatGPT, Claude or Codex through
existing CLI/MCP access. Extend the existing shared engine rather than embedding
financial calculations in assistant prompts or duplicating them across clients.

Expose a small coherent interface for long-context analysis, purchase-window
analysis, plan comparison, evaluation and optional vigilance. Prefer extending
existing tools where their meaning remains clear; add a focused new tool only
when the operation is genuinely distinct. Return bounded structured results.
Do not expose arbitrary SQL, code execution or unrestricted file access.

Reports and charts must share the same as-of time and input reference. Provide:

- Long-history price context with explicit observed lookbacks.
- Purchase thresholds and observed scenario outcomes, with uncertainty and sample
  size; no invented future price path.
- Plan comparison showing holdings plus reserve, acquisition cost and purchases.
- A concise explanation of contradictory observations and data limitations.

All authored report and chart text is English. Label synthetic examples visibly.
Document formulas, units, calendar conventions, source and quote choices. Avoid
combining incompatible units on an unexplained axis. Missing evidence must yield
an explicit unavailable panel or error, not a simulated chart presented as live.

No large graphical application is requested. Preserve headless use and reusable
report templates. Spreadsheet output may be an optional view over the same
validated calculations; it is not a reason to create a second calculation engine.

## 13. Engineering workflow and verification

Use `feat/` branches. Suggested slices are `feat/english-only-repository`,
`feat/long-horizon-context`, `feat/purchase-window-analysis`,
`feat/accumulation-plans`, `feat/accumulation-validation`,
`feat/market-context` and `feat/short-horizon-vigilance`.
A different small-slice structure is acceptable if the phase order and evidence
remain clear. Follow the session's merge authorization and repository rules.

Keep these commands as the shared entry point, adapting the helper only when
necessary to include justified new checks:

```bash
uv sync --frozen --extra dev
uv run --frozen python tools/check.py
uv build --wheel --offline
```

Run checks in the working environment, whether local or Cloud. Refresh the
repository's Actions state; no hosted CI execution is assumed by this brief.
If a GitHub workflow is useful, make it a dedicated reviewed change using the
same verification command, with bounded jobs and duplicate-run cancellation.
Keep ordinary tests independent of public APIs, paid services and personal data.
Separate any explicitly permitted live smoke check from the offline test suite.

Use meaningful behavioral tests for cash conservation, temporal isolation,
source/quote separation, missing inputs, statistical support, English output and
actual CLI/MCP contracts. Snapshot updates alone do not validate the calculations.
Use independent hand-calculated accounting cases where appropriate.

Run targeted checks while implementing, and the complete appropriate suite for
the final code. Repeat broad checks when new changes or failures justify it,
not merely to accumulate test output. Record the exact tested commit and any
environment limitations. A blocked network smoke check is not a successful one.

If delegation materially helps, use `gpt-6.1-sol` with High reasoning unless the
user explicitly selects another model. Give independent bounded scopes; avoid
concurrent edits to the same calculation or schema. Use at most Extra High when
justified, never Max or Ultra. Do not create a separate user-facing chat unless
the user asks for one.

Suggested skills, if available in the receiving session:

- `writing-for-agents` for the English-only agent instructions.
- `codebase-design` for a small shared analytical interface.
- `code-review` for a bounded final correctness and requirements review.

Skill availability is not a prerequisite. Preserve the concrete requirements
and verification evidence if those helpers are unavailable.

## 14. Final delivery requirements

Deliver the implemented code and English documentation, with:

1. A phase-by-phase completion table: implemented, verified, blocked or deferred,
   and a concrete reason for any unfinished requirement.
2. Reproducible synthetic CLI and MCP examples for long context, purchase windows,
   purchase-plan comparison, evaluation and vigilance.
3. A documented configuration/schema migration and updated data catalogue.
4. Tests and review findings with the exact code version, commands and results.
5. Clear separation between software correctness, historical evaluation and
   prospective evidence. State when no improvement over regular purchases exists.
6. Reviewable pull requests or commits under the session's delivery policy.
7. A short remaining-limitations list covering missing sources, statistical
   uncertainty, execution assumptions and operational setup not performed.

Do not publish private portfolio records, personal budgets, secrets, databases,
runtime journals or real user analysis history. Keep this repository focused on
the application code, documentation and shareable examples.

## 15. Reference material

Use repository content at the current checked-out commit as the implementation
source of truth. The following links provide the reviewed starting point and
primary definitions; verify external documentation again when implementing an
integration:

- Baseline tree: https://github.com/Dan-Popescu/crypto-market-analysis/tree/e81a4ed5e2a05a8c1328686314b067038e733b92
- Existing analysis and evaluation: https://github.com/Dan-Popescu/crypto-market-analysis/blob/e81a4ed5e2a05a8c1328686314b067038e733b92/src/crypto_market_analysis/research.py
- Import evidence and known freshness gap: https://github.com/Dan-Popescu/crypto-market-analysis/blob/e81a4ed5e2a05a8c1328686314b067038e733b92/docs/IMPORT.md
- Regular purchase baseline definition: https://www.investor.gov/introduction-investing/investing-basics/glossary/dollar-cost-averaging
- Waiting-cost discussion, not evidence of a crypto strategy's performance: https://syndication.finra.org/content/benefits-and-limitations-dollar-cost-averaging
- Official central-bank calendar: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- Official inflation-release calendar: https://www.bls.gov/schedule/news_release/cpi.htm
