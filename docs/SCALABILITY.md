# Operating 100 assets with a path to 1,000

Version 0.9.0 implements [the scaling plan](SCALABILITY_PLAN.md). An asset universe
is an explicit list of canonical CCXT base codes (1–1,000); BTC/ETH remain the
defaults. A ticker is not a verified token identity, provider entitlement or
investment recommendation. Native source, quote, mode, timeframe and receipt
versions remain separate. Generic prices, costs, plans and derivatives accept
these identifiers; source-specific ETF, option and network capabilities remain
limited to their actual supported assets. Existing named Kraken archive datasets
retain their four original native markets.

## Plan demand before collection

```bash
uv run crypto-market-analysis collection-capacity --config observer.toml
```

Set `assets` explicitly in TOML. The plan exposes native pair/source/task demand,
page ceilings, cadences and unknown actual HTTP work. Default
`collection_batch_size = 10` and `collection_work_limit = 256` bound dispatch.
For larger workloads, durable source/task/native-pair cursors reserve actual
attempts before I/O and rotate across failures and restarts. Every completed
pair is persisted before its rows are released. Pending rotations remain due
rather than pretending an entire venue has been refreshed. Catalogue indexes and
grouped latest-native-candle cursors avoid repeated full scans. Each source client
is serialized; the large-universe path currently uses serial dispatch to make
small-budget fairness deterministic. This is an explicit throughput trade-off.

Use `liquidity_assets` and `liquidity_venues` to limit book requests independently
of broad hourly market analysis. At 1,000 assets, collecting every quote on every
venue at five-minute cadence can exceed free provider limits regardless of the
database. Measured transport work, rather than asset count alone, determines a
feasible cadence. Bounded configuration does not promise every requested market
exists or meets a freshness target.

## Observe actual operation and cost

```bash
uv run crypto-market-analysis operating-acceptance --request examples/operating-acceptance.json
uv run crypto-market-analysis operating-acceptance --mode live --request examples/operating-acceptance.json --apply
uv run crypto-market-analysis operating-health --request examples/operating-health.json
uv run crypto-market-analysis operating-costs --request examples/operating-costs.json
```

Inspection does not initialize a store or collect data. Apply starts one finite
experiment, never a permanent worker. Real wall span, active duration, per-cycle
CPU, process peak RSS, physical storage/row deltas, collection limits and native
coverage are retained. CPU/RSS are process evidence, not invented per-source
allocation. Reports separate transport attempts, decoded bytes, logical operations
and unknown wire bytes. Optional declared unit prices produce estimates; actual
invoices remain unknown. Read-only reports page up to 100 groups and retain unknown
measurement coverage. Whole-store counters and aggregate reads have time budgets.

A session freezes its full workload, dependency/code identities and injected-versus-
owned provenance; changed configuration requires a new session ID. Durable due
clocks and exclusive session locking survive restart. Errors, exhausted budgets
and incomplete capture produce a nonzero apply exit even if a cycle completed.
Injected clients and accelerated fixtures never qualify real providers. Elapsed
wall span includes downtime; active duration and missing samples remain separate.
A weeks-long acceptance needs actual elapsed observations and sufficient coverage.

MCP: `get_collection_capacity`, `get_operating_health`, `get_operating_costs` and
`get_operating_acceptance` are read-only. Acceptance sessions are live-mode evidence;
a nonlive server reports selection mismatch. Refresh is rejected by MCP.

## Freeze evaluation before issuing an assessment

```bash
uv run crypto-market-analysis freeze-validation --request examples/validation-protocol.json --output protocol.json
uv run crypto-market-analysis issue --idempotency-key my-frozen-issue --asset BTC --source kraken --quote EUR --validation-protocol protocol.json
```

The example is a declaration, with an example-only name and 2027 issuance period;
its zero costs are explicit assumptions. Replace them with the intended event,
period, native market, rules, model version and cost policy. Freezing records the
actual clock and content hash; an existing artifact can only be reused identically.
The original issue binds that protocol hash and derives comparable predictions
only from actual supported window calculations, never caller-invented probabilities.
Retries return the same original, and changing the protocol conflicts with its
idempotency key. Prospective issuance uses the declared half-open time interval.

`prospective-validation` and MCP `get_prospective_validation` take a request with
`protocol` (the full frozen artifact), plus optional `cohort_page`, `page_size`,
`origins_per_cohort` and `record_budget`. Up to 1,000 declared asset cohorts are
paged, with at most 100 origins per cohort and 1,000 records per request. Earliest
origins are selected before their outcomes are examined. Missing, pending,
excluded and overlapping origins remain in denominators. Threshold, anchor price,
clock convention, native market, lineage and rules must match. Anchor-relative
estimates are not scored against an incompatible issuance-relative event.

Aligned forecasts can produce descriptive Brier/calibration results. Other forecasts
remain explicitly unscored. Plan comparisons reuse funded execution qualification,
complete paths and the matching regular-purchase baseline with identical cashflows
and declared fee/spread/slippage/rounding assumptions. Criterion support is not
calibration, statistical independence, causal evidence or a demonstrated advantage.
Real 90/180-day outcomes require future observations and elapsed time.

Journal reads own bounded cohort selection and immutable slim projections, indexed
by native market/issue time and assessment/outcome time. Explicit writer initialization
backfills a bounded batch; repeatable writer-owned migration handles remaining records.
Read-only validation can project valid legacy archives without creating a schema.
Original archived evidence and content hashes remain unchanged.

## Keep large evidence deliberate

Add `"evidence_mode": "references"` to a dossier or readiness request. The default
`full` mode stays compatible; reference mode retains the calculations, exact input
digests, consumed counts and explicit omissions while avoiding raw-record copies.
Omitted evidence is not silently promised by frozen MCP paging: use explicit full
mode where it fits to obtain those records. Daily aggregation and feature preparation
are reused within an exact request/snapshot, without a stale global result cache.
The existing SQL work, byte, reader-lifetime and full-result ceilings still apply.

## Use analytical snapshots for fleet research

```bash
uv run crypto-market-analysis analytics-snapshot --name research
uv run crypto-market-analysis analytics-snapshot --name research --request examples/analytics-export.json --apply
uv run crypto-market-analysis analytics-fleet --name research --request examples/analytics-fleet.json
```

Named snapshots live under the configured `data_dir/analytical_snapshots`; MCP
accepts a safe name, never an arbitrary path. Export is explicit and immutable,
retains every candle vintage and original tie-breaking row ID, and publishes an
all-vintage Parquet manifest with file/content checksums. The example enables
partitioned v2 export; legacy v1 snapshots remain readable. Explicit source, asset,
quote, mode and half-open time filters apply before counting/export. Per-file,
total row, canonical-byte, partition and deadline budgets prevent a single
monolithic artifact from defining total project capacity. Exported selection is
retained in the manifest. It is a candle analytical
export, not a backup or a coherent snapshot of books, calendars and journals.
Cold verification has a cooperative 30-second budget; DuckDB queries have a
separate 30-second/256MB budget with disk spill disabled. At most two fleet SQL
queries and one cache-miss verification run concurrently per process; excess work
fails fast for retry rather than building an unbounded queue. These are application
controls, not total process-RAM or OS quotas. A bounded four-entry cache reuses verified
snapshots only while directory/manifest/part identities remain unchanged. Changed
or corrupt files fail verification. Fleet queries aggregate in DuckDB before
returning bounded results, retaining native series and unknown missing assets.

MCP: `get_analytical_snapshot_status` inspects a manifest without asserting integrity;
`get_analytical_fleet` verifies and queries it. Frozen coverage is not live freshness.
SQLite remains authoritative for current ingestion and the journal. Exports do not
automatically prune it, redirect existing dossier reads or authorize data loss.
Storage admission limits remain explicit; monitor growth and choose narrow native
coverage, cadence and export/retention policies before scaling historical volume.
See [the storage decision](adr/0001-storage-for-scaled-research.md) for measured
engine trade-offs and triggers for revisiting PostgreSQL.
