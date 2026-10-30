# Accumulation implementation plan

The adopted specification is [the cloud brief](CLOUD_IMPLEMENTATION_BRIEF.md),
refined by Dan's France, global coverage and separate EUR/USD requirements.
This plan extends the existing shared Python engine without trade execution.

## Delivery sequence

1. English migration and compatibility inventory, retaining dated validation provenance.
2. Complete market identity, coherent input capture, event-time freshness, bounded
   history and long context through 730 days; matching CLI, MCP, reports and charts.
3. Purchase windows at 1, 3, 7 and 14 days, with explicit issuance-relative events,
   reference frequencies, uncertainty, expiry and insufficient-data states.
4. Decimal cash/inventory accounting and regular, split-level and bounded-deferral
   plans; equal contributions, execution assumptions and 90/180-day terminal dates.
5. Immutable issued assessments and appended outcomes; held-out event and plan
   evaluation, retention-safe replay, contribution-adjusted drawdown and reserve.
6. Qualified calendar, derivatives, options, network and ETF context with honest
   manual/unavailable fallbacks and per-source rights and release-time records.
7. Optional 4/8-hour vigilance with bounded watch configuration, deduplication,
   recovery and expiry. Operational activation remains a separate task.

Shared contracts are designed before parallel implementation. Integrate deliveries
in this order, with targeted verification in every lot and final integrated checks.

## Product constraints

- Purchase jurisdiction: France. Initial candidates: Kraken, Bitvavo, Coinbase.
  Record actual legal entity, authorized services, official evidence and checked date.
- Global analytical relevance is independent of purchase eligibility. Retain major
  spot/derivatives venues according to credible activity, liquidity and data quality.
- BTC/EUR, ETH/EUR, BTC/USD and ETH/USD are separate analyses. USDT and USDC stay
  native stablecoin quotes; no implicit parity, conversion or cross-venue averaging.
- No paid API is required for core calculations. Optional source failures cannot
  block deterministic plans. Account keys, budgets and holdings are optional/local.
- A historical reference market is distinct from simulated purchase conditions.
- A lower acquisition cost alone does not establish better total wealth.
- Historical backfills remain retrospective unless actual availability is established.

## Shared module interfaces

Existing `Candle`, `Observation`, `Store` and `Engine` remain the stable entry points.
Keep pure calculations independent of CLI/MCP, network calls and persistence.

- `evidence.py`: canonical JSON hashes, complete metric/market identity, captured
  inputs and bounded evidence envelopes. Identity includes quote, mode and rules.
- `freshness.py`: event age, ingestion age, cadence, allowed lateness, expiry and
  quality. Collection success never refreshes old market evidence.
- `context.py`: `market_context(candles, as_of, lookbacks=...) -> dict`, with formulas,
  units, coverage, complete UTC calendar periods and named historical ranges.
- `purchase_windows.py`: `analyze_windows(candles, as_of, ...) -> dict` and explicit
  observed event resolution; separate anchor, issuance, endpoint and crossing interval.
- `plans.py`: `compare_plans(config, candles, as_of, ...) -> dict`. Validated local
  configuration uses decimal strings for money, quantities and fees. One accounting
  model serves all policies. Return finite JSON with decimal strings.
- `plan_evaluation.py`: paired multi-origin comparisons over frozen policies,
  separating retrospective and strict availability and held-out periods.
- `journal.py`: explicit issuance, idempotent retries and append-only outcomes;
  preserve consumed evidence against later revisions and retention.
- `vigilance.py`: pure assessment and transition functions; persisted alerts are
  explicit local operations, never side effects of read-only MCP calculation.

Engine methods and CLI/MCP adapters integrate these modules once their result
contracts are fixed. Schema changes have an explicit version and migration note.

## Parallel ownership

Use isolated worktrees and `feat/` branches. One owner changes each shared file per
wave, especially `engine.py`, `storage.py`, `research.py`, `config.py`, `cli.py` and
`server.py`. Independent new calculation modules, provider adapters and behavioral
fixtures may proceed in parallel against frozen contracts. Root owns integration.

## Verification

Use public interfaces and independent hand-calculated expectations. Important
cases include quote identity, old reingested data, gaps, partial calendar periods,
post-issuance touches, fee currencies, future deposits, target expiry, unknown
starting cost, pending outcomes, underperformance, journal retry conflicts and
replay after retention. Validate actual CLI/local MCP protocols and English output.

Run `uv run --frozen python tools/check.py` and build the wheel offline on the
integrated version. Do not treat synthetic correctness as investment performance.
Record exact commits, commands and unavailable live checks in delivery evidence.
