# Gradual-purchase research

The workflow separates historical context, a possible purchase window, a plan's later outcome and exceptional vigilance. BTC and ETH are supported; choose the source and native quote explicitly. A lower average acquisition cost alone does not prove a better outcome. Compare holdings **plus remaining cash** at identical terminal dates. Flat markets, prolonged declines and missed rising-market exposure remain possible.

## Reproducible synthetic CLI workflow

Run from the repository root. This creates local synthetic data and output files only:

```bash
uv run crypto-market-analysis demo --data-dir data/demo-docs --days 800 --quotes EUR,USD --as-of 2026-10-28T00:00:00Z
uv run crypto-market-analysis context --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --lookbacks 7,30,90,180,365,730 --output output/demo-context.json
uv run crypto-market-analysis windows --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --horizons 1,3,7,14 --thresholds 0.02,0.05,0.10 --output output/demo-windows.json
uv run crypto-market-analysis compare-plans --data-dir data/demo-docs --mode demo --plan-config examples/accumulation-plan.json --as-of 2026-10-28T00:00:00Z --validation retrospective --output output/demo-plans.json
uv run crypto-market-analysis evaluate-plans --data-dir data/demo-docs --mode demo --plan-config examples/accumulation-plan.json --as-of 2026-10-28T00:00:00Z --validation retrospective --output output/demo-plan-evaluation.json
uv run crypto-market-analysis evaluate-plans --data-dir data/demo-docs --mode demo --plan-config examples/accumulation-plan.json --as-of 2026-10-28T00:00:00Z --validation known_at --output output/demo-strict-evaluation.json
uv run crypto-market-analysis vigilance --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --horizon-hours 4 --output output/demo-vigilance.json
```

The fixed fixture makes EUR and USD independent synthetic scenarios. Repeat asset-specific commands with `--asset ETH`, or use `--quote USD` for separate context/windows. For USD plan comparisons, explicitly create a separate local plan configuration with `quote` set to `USD`; never relabel an EUR result or imply conversion.

The example plan begins January 1, 2026, so 90/180-day outcomes fit within this fixture. Its retrospective evaluation can still report insufficient sample support: the number of independent completed origins matters. Strict evaluation excludes history first observed after an origin; a newly seeded synthetic history is not a historical availability archive. Empty strict results are expected evidence of that restriction.

Add `--format markdown` to accumulation commands for deterministic prose. To render the same fixed snapshot:

```bash
uv run crypto-market-analysis chart --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --kind context --output output/demo-context.png
uv run crypto-market-analysis chart --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --kind windows --output output/demo-windows.png
uv run crypto-market-analysis chart --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --kind plans --plan-config examples/accumulation-plan.json --validation retrospective --output output/demo-plans.png
```

## Context and event definitions

Context defaults to 7, 21, 30, 60, 90, 180, 365 and 730 days. Measures use complete UTC days with explicit required/observed coverage; missing history does not silently become a shorter lookback. Weeks run Monday–Sunday UTC. Completed months use calendar boundaries. Price ranks and historical drawdowns describe observed history; a low rank is not proof of undervaluation.

Purchase windows default to 1, 3, 7 and 14 days and declines of 2%, 5% and 10% from the recorded issuance anchor. Endpoint change, any below-anchor threshold touch and calendar-red close are distinct events. Resolution excludes pre-issuance extremes; partial bars, missing coverage and unknown barrier ordering remain pending, ambiguous or unavailable. A touch is not an executable fill, and hourly observations do not establish an exact minute of crossing.

Frequencies remain experimental and carry unconditional reference frequencies, support, uncertainty, freshness and expiry. Sparse or stale evidence must not become a confident purchase signal. Existing 90/180-day statistical frequencies would require roughly 1,862/3,662 complete days under the original 20 non-overlapping-window method; two years of context is not sufficient. Those horizons primarily assess purchase plans.

## Plan schema and accounting

[examples/accumulation-plan.json](../examples/accumulation-plan.json) is a shareable synthetic schema-1 configuration. Times are integer UTC epoch seconds. Amounts, quantities, fees, prices and policy fractions are decimal strings. Keep personal versions local.

| Field | Meaning |
| --- | --- |
| `asset`, `source`, `quote`, `data_mode` | Explicit market identity, with no currency conversion or mode fallback. |
| `start_time` | Origin for common 90/180-day terminal timestamps. |
| `contributions` | Dated quote cash actually available. Future deposits cannot fund earlier purchases. |
| `schedule` | Dated quote purchase notional, excluding quote fees. |
| `starting_holdings` | Optional quantity and known acquisition cost; `cost: null` keeps total historical cost unknown. |
| `fees` | Proportional `rate`, `fixed` charge and `asset` (`quote` or `base`). Third-asset fees are unsupported. |
| `execution` | `historical_configured_costs`, bar timeframe, spread/slippage assumptions, quantity/price steps and maximum acceptable price age. |
| `plans.split` | Immediate fraction plus below-anchor levels, bounded waiting time, deferred reserve cap and fallback. Fractions sum to one. |
| `plans.deferred` | Fraction delayed to one threshold, waiting deadline, reserve cap and fallback. |

All families share exclusive cash allocation and the same contribution dates, fees, source, valuation times and quote. `regular` is the fixed-amount baseline. Unused reserve earns zero assumed interest; a stablecoin is not implicitly risk-free cash. The new-purchase all-in cost is `(spent + quote fees) / net acquired quantity`; base fees reduce net quantity. Total holdings cost remains unknown when starting acquisition cost is unknown.

Execution uses a conservative eligible full-bar open with configured cost uplift, upward price rounding and downward quantity rounding. A bar's later low cannot decide a fill at its open. A target touched only within the bar is recorded separately as `touch_only_not_fill`. Missing or stale execution prices can leave a target unfilled. Historical costs are assumptions, not measured execution; aggregated depth cannot reconstruct a full fill curve. Full order-book fill estimation is unavailable.

Outcomes use `start_time + 90 * 86400` and `start_time + 180 * 86400` for every plan. Missing future prices yield `pending`. Outputs reconcile contributions, spending, fees, reserve and acquired quantity and show purchases, waits, expired targets and combined terminal wealth. Maximum drawdown uses unitized NAV: deposits issue units at pre-deposit NAV, so external contributions are not investment gains or drawdown recovery. Unobserved intrabar paths remain outside that measure.

## Evaluation and prospective records

`evaluate-plans` shifts identical cashflow offsets across origins spaced at least 180 days. It reports dispersion and underperformance against regular purchases, reserve, waiting and contribution-adjusted drawdown. Use `--origins`, `--held-out-start` and `--policy-frozen-at` only with honestly declared UTC dates. The rule-freeze declaration is not independent evidence that the policy was fixed then. Retrospective results, synthetic correctness and prospective evidence remain separate; no improvement over regular purchases is established by this delivery.

Issuance and outcome observation are explicit local writes, available through the CLI only:

```bash
uv run crypto-market-analysis issue --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --plan-config examples/accumulation-plan.json --idempotency-key docs-demo-001 --output output/demo-issued.json
```

Read `assessment_id` from that JSON, then replace `ASSESSMENT_ID` below:

```bash
uv run crypto-market-analysis assessment --data-dir data/demo-docs --mode demo --id ASSESSMENT_ID
uv run crypto-market-analysis observe --data-dir data/demo-docs --mode demo --id ASSESSMENT_ID
```

The journal freezes consumed evidence, input/configuration hashes, rules, issuance time and provenance. The same key and request returns the original record; reusing a key for a different request fails. Later observations append outcomes without rewriting issuance. Historical `--as-of` calculations retain their actual issuance time and do not manufacture a past publication. The fixed demo stops before actual issuance, so later windows may remain pending or lack post-issuance evidence. Keep the database and journal local and outside version control.

Vigilance is observational, opt-in and secondary to accumulation. It returns reasons, contradictions, missing-source flags and expiration for 4 or 8 hours, without a numeric crash probability. The default command is read-only. Supplying `--state-key LOCAL_KEY` explicitly persists a local deduplicated transition/alert contract, with cooldown, recovery, expiry and bounded watch configuration; it does not start a collector or send a notification. Missing/stale inputs degrade the assessment. No verified complete liquidation feed or historical liquidation map is supplied.

## Local history and calendar inputs

Archive imports are explicit local operations, not automatic downloads:

```bash
uv run crypto-market-analysis import-history --data-dir data/import-docs --file /path/to/Kraken_OHLCVT.zip --member XBTEUR_60.csv --source kraken --asset BTC --quote EUR
uv run crypto-market-analysis import-history --data-dir data/import-docs --file /path/to/BTCUSDT-1h-2026-01.zip --member BTCUSDT-1h-2026-01.csv --source binance --asset BTC --quote USDT --expected-sha256 VERIFIED_OFFICIAL_SHA256
uv run crypto-market-analysis import-calendar --data-dir data/import-docs --file /path/to/bls.ics --kind bls
uv run crypto-market-analysis import-calendar --data-dir data/import-docs --file /path/to/fomc.html --kind fed
```

Select the exact hourly ZIP member. Binance Vision requires the official companion SHA-256 digest (or matching checksum line), not an invented value. Kraken checksums are optional when an expected value is available. The importer hashes the same bounded bytes it parses and records file/member hashes, gaps and retrospective availability. Limits are 256 MiB per file, 64 MiB per member, 2,000 members and 200,000 rows; multi-gigabyte archives need a permitted smaller pair/hourly file. Kraken `_60.csv` uses epoch seconds. Binance hourly spot archives use milliseconds before 2025 and microseconds from January 1, 2025; inconsistent units fail. Daily files are unsupported by the hourly import contract. Imported history belongs to `import` mode and does not prove historical `known_at` availability.

BLS CPI iCalendar times retain timezone and daylight-saving handling. FOMC meeting dates remain date-only unless the source row supplies a verified explicit timestamp; a meeting date does not imply a 14:00 Eastern decision. These records live in the calendar store, with observation time, source and version. Scheduled event time and actual publication time remain separate: unknown publication stays unknown. Unsupported dates/timezones/recurrences are explicit. Calendar inputs are bounded to 2 MB.

`uv run crypto-market-analysis collect-calendar --data-dir data/live-docs --mode live` makes a bounded one-shot public BLS/Federal Reserve request and reports source errors. It requires network access; no successful new live collection is claimed in this cloud environment. Manual ETF metrics remain a sourced fallback via `import-metrics`, with explicit units and net-flow definition, not an automated complete feed. See [the data catalogue](DATA_CATALOG.md).
