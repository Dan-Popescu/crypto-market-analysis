# Versioned instrument research in 0.10.0

The `market-*` CLI and eleven additional read-only MCP tools implement a native
instrument path alongside the existing cryptocurrency workflows. This is an
experimental implementation with synthetic/import acceptance, not a declaration
of licensed live equity or futures coverage. No paid API is required or enabled.

## Capability boundaries

| Area | Implemented | Explicit limits |
| --- | --- | --- |
| Identity | Stable instruments and optional listings, provider aliases, currencies, asset class versus form, historical delisting/universe membership | Supplied identities and identifiers require source verification; ticker syntax is never proof |
| Calendars | Sourced/versioned sessions and publications, UTC/local dates, DST, holidays, early closes, lunch breaks, known halts, provider delay | Outside the declared schedule is unknown; generated exchange rules are not independent point-in-time historical attestation |
| Prices | Native intervals, publication points, raw/split-adjusted/total-return/continuous bases, receipt vintages | Adjusted history remains captured history unless independently available at the historical cutoff; bases never substitute for raw fills |
| Statistics | Price-only endpoint changes, admissible simple/log returns, drawdown and explicitly annualized volatility | Negative/zero prices disable positive-price methods; no universal 252/365 or implicit FX conversion |
| Cash plans | Hypothetical shares, listed funds and crypto spot; explicit deposits/costs; splits, dividend entitlement/payment and receivables | Raw listing prices only; no reinvestment/taxes/spread/slippage model. Incomplete action coverage and unknown costs stay labelled; simultaneous ambiguous actions, split-boundary fills and incompatible marks fail |
| Futures | Expiry/unit/multiplier/settlement identity, signed single-mark P&L, fixed cash-margin sensitivity, explicit unadjusted continuous derivation | No daily settlement survival, changing broker margins, liquidation, physical delivery, expiry settlement or roll execution. Continuous series cannot supply cash-plan fills |
| Prospective | Frozen hashed native protocols, actual-clock immutable issuance, session/calendar/elapsed horizons, read-only directional price outcomes and Brier score | Raw point-in-time live receipts or explicit demo only; imports cannot prove prospective performance. Split-affected outcomes are excluded. No fitted/calibrated edge claim |
| Storage | Atomic append-only native components in observer.sqlite, effective knowledge cutoffs, coherent read sessions, native-only/mixed backup verification | Local single-writer authority; existing crypto records/hashes stay unchanged. Input declarations are not independent witnesses |
| Analytical snapshots | Independent all-vintage market-v1 Parquet exports, verified DuckDB reads and one-query native fleet summaries | Explicit immutable exports; no automatic hot-store pruning, fresh-feed claim or conversion of legacy snapshot formats |

## Run an offline equity and commodity example

Every value and identity in the following fixtures is invented. No provider is
contacted. Use an ordinary data directory, separate from named Kraken archives.

```bash
uv sync --frozen
crypto-market-analysis market-import --request examples/market-bundle.json --data-dir data/market-demo
crypto-market-analysis market-import --request examples/market-bundle.json --data-dir data/market-demo --apply
crypto-market-analysis market-analysis --request examples/market-analysis.json --data-dir data/market-demo --as-of 2024-01-12T00:00:00Z
crypto-market-analysis market-plan --request examples/market-plan.json --data-dir data/market-demo --as-of 2024-01-12T00:00:00Z
crypto-market-analysis market-snapshot --name research --data-dir data/market-demo --apply
crypto-market-analysis market-fleet --name research --request examples/market-fleet.json --data-dir data/market-demo --as-of 2024-01-12T00:00:00Z
crypto-market-analysis market-futures-value --request examples/market-futures-value.json --as-of 30
crypto-market-analysis market-futures-curve --request examples/market-futures-curve.json --as-of 30
```

`market-import` defaults to validation only. `market-fetch`, `market-snapshot`
and `market-issue` likewise require `--apply` for mutation. Capture, calendar
generation and issuance use the actual clock and reject `--as-of`. Analytical
reads and all MCP tools never initialize, migrate or recover missing stores.
Named history-dataset writers reject these ordinary-store mutations.

All commands accept bounded JSON through `--request file.json` or `--request -`.
Requests contain explicit data modes and native currency; they take precedence
as selections rather than silently combining the CLI's default mode. Report
outputs cannot replace the authority, input request, protected dataset evidence
or a recovery/snapshot bundle. A failed/unsupported calculation returns a reason.

## Contract and revision ownership

`market-bundle.v1` contains `registry`, `calendars`, and `series`. The registry
is `market-registry.v1` with instruments, listings, aliases and optional universe
memberships. Independent subregistries accumulate without deleting omitted
instruments. Instrument metadata is immutable; listing/alias/membership revisions
carry effective and knowledge clocks. Ambiguous aliases require disambiguation.

Each `market-series.v1` holds an immutable `descriptor`, `bars` and optional
`actions`. The descriptor pins instrument/listing, provider/symbol, native
currency/unit, price basis, mode, calendar/version, interval and revision semantics.
Changing those contracts requires a distinct series ID. This includes a different
action-coverage declaration or calendar version; old forecasts remain bound to
their exact original contract. Native observations can append receipt versions.
Bar clocks are integer epoch seconds, including pre-1970 history; point bounds
are admitted only for reference observations. No currency conversion is implied.

Original JSON values and hashes are preserved in the journal. Live visibility
uses at least the actual local receipt time. Imported/demo history uses explicitly
self-declared historical clocks and remains hindsight/synthetic evidence even
if a supplied descriptor says point_in_time. Visible vintages rank by
`(effective_known_at, observed_at)` consistently across SQL, price selectors,
plans and prospective outcomes. Exact same-vintage conflicts fail atomically.

Price analysis selects latest eligible vintages. Plans retain all vintages so
the original purchase can use a price actually known at that event; later
corrections change eligible marks without rewriting the original fill. Action
corrections that would invent historical entitlement/execution fail explicitly.
Source-declared complete action coverage is never independent verification.

## Calendars and sources

`market-calendar-build` accepts `{mic,start_date,end_date}` and emits a bounded
schedule using locked exchange-calendars rules (for example XNYS, XPAR, XTKS).
Its provenance records the package version and actual knowledge clock. Explicit
`market-calendar.v1` schedules also support publication-only references, sourced
breaks/closures and declared halts. These exchange-session contracts are separate
from the legacy macro-release calendar.

`market-fetch` accepts `{registry,calendars,source_request}`. See
[source qualification](CROSS_ASSET_SOURCES.md) for the exact provider request.
The implemented providers are explicit normalized JSON/CSV imports and an opt-in,
bounded keyless FRED graph reference adapter. Imported sources require an origin,
rights declaration and price-basis contract. FRED reference prices are not fills.
Yahoo/Stooq candidates are explicitly unqualified and refused. A rights statement
does not verify a license; no free licensed real-time equity access is promised.

During implementation, this environment's enforced destination policy excludes
FRED and candidate equity hosts; the web research tool was also unavailable.
Public access, current terms and real-data acceptance remain unverified. This
limitation is exposed through capabilities/source reports, not replaced by a
successful synthetic HTTP fixture.

## Agent discovery

Start with `get_market_capabilities` and `get_request_schema` for an individual
tool or `list`. The original 53 tools retain their names; these eleven are added:

| Tool | Purpose |
| --- | --- |
| `get_market_capabilities` | Implemented semantics, unsupported forms, source qualification and limits |
| `get_instrument_registry` | Known/effective metadata, aliases and universe membership |
| `get_market_calendar` | Native expected grid, closures, delays and unknown coverage |
| `get_instrument_analysis` | Explicit native selection, descriptive statistics and quality/provenance |
| `simulate_instrument_plan` | Hypothetical supported cash accounting and its assumptions |
| `get_instrument_series` | At most 512 intervals/action versions, optional all_vintages and receipt evidence |
| `get_market_snapshot_status` | Manifest presence without claiming content verification |
| `get_market_fleet` | Verified native aggregation of up to 1,000 explicit series |
| `get_instrument_outcome` | Frozen assessment, pending/missing/excluded/resolved outcome |
| `calculate_futures_value` | Explicit isolated margin/mark model |
| `build_futures_research_series` | Sourced known-at unadjusted derived contract path |

The shared MCP presenter bounds replies and exposes frozen omitted details through
existing result paging. Metadata includes the native descriptor, selected
instrument/listing, calendar/version, registry, coverage and receipt evidence;
do not treat omitted records as neutral or unavailable data as a zero signal.

## Frozen native protocols

`market-freeze` accepts a `market-protocol.v1` declaration containing the exact
series descriptor/calendar, method directional-price-only.v1, horizon, eligibility
interval, price_only action handling and optional receipt-lag cap. It freezes
before eligibility, uses the actual clock and cannot replace a different existing
output. `market-issue --apply` accepts market-assessment-request.v1 with the frozen
protocol, probability_up and idempotency_key. No model fitting is performed.

Horizons explicitly count sessions, venue-local calendar days (including DST) or
elapsed seconds. Deadline rules are exact/next_session/previous_session. Issuance
pins an eligible completed native anchor; evaluation pins the same native lineage
and target. Receipt-limited outcome versions cannot be replaced by later
corrections. Pending, missing and excluded events remain distinct from scored
events. Raw split-boundary/crossing outcomes are excluded. Frozen local declarations
are not externally attested preregistration, and individual Brier scores do not
establish calibration or predictive value.

## Scale and verification

The explicit instrument registry supports 1,000 instruments. Listings, currencies,
providers, frequencies, futures expiries and receipt vintages multiply the workload;
one instrument need not mean one series. Each bundle/fleet/snapshot selection is
bounded independently. Multiple bounded selections can cover a larger retained
series catalogue. A complete long-history export may require slices.

SQLite remains authoritative for local writes and backup. Immutable market-v1
snapshots stream native all-vintage tables to Parquet. DuckDB shares the existing
256 MiB/two-thread query budget, no spill writes, 30-second SQL deadline and two
process-wide heavy-query slots. Snapshot file verification is bounded and file
identity is checked again after reads. Fleet summaries aggregate once in SQL;
they do not open 1,000 analytical connections or invent expected 24/7 coverage.

Reproduce the synthetic workload with:

```bash
uv run --frozen python tools/benchmark_cross_asset.py --instruments 100 1000 --output mixed-scale.json
uv run --frozen python tools/check.py
```

The workload uses shares, crypto spot, commodity references, listed funds and
individual futures, two native currencies, native daily/point grids and two
receipt vintages. SQLite and snapshot evidence must match exactly. Timing depends
on the environment and filesystem cache; this is neither long-history nor
multiwriter/provider cadence acceptance. See [delivery evidence](CROSS_ASSET_STATUS.md).
