# Measured operation and coherent local workflows

Version 0.7.0 implements [the five accepted projects](OPERATING_CAPACITY_PLAN.md).
Version 0.7.1 strengthens their [repair reliability](REPAIR_RELIABILITY_PLAN.md).
Native markets, modes, source clocks, journal originals and OBSERVER settings
remain intact. No paid source, account key, deployment or permanent worker is added.

## Observed HTTP consumption

```bash
uv run crypto-market-analysis http-accounting --data-dir data/live --source kraken --format markdown
uv run crypto-market-analysis http-accounting --data-dir data/live --task market --limit 1000 --endpoint-limit 100
```

Public CCXT and context collection records observed transport attempts, responses,
errors, elapsed time and decoded response body bytes inside retained run payloads.
Catalogue traffic and observed retries/redirects count. Adapter calls never become
invented HTTP counts; unsupported injected clients retain unknown totals. Hidden
retries in customized transport adapters cause partial coverage.
An explicitly injected Kraken history client has an unverified transport contract:
measured Requests work is a partial subset, while an unused instrumented Session
does not turn other unknown traffic into observed zero. The owned default CCXT
adapter retains fully observed coverage for its supported public transport.

Endpoint IDs hash the path and omit URL, query, userinfo, headers and body. Each
run retains at most 32 IDs; overflow preserves totals and reports omitted endpoint
attribution. Operational transport errors keep class/status/deadlines without
URL/body details. Raw evidence archives retain their existing provenance contract.

Aggregation filters source, task, mode and receipt range before selecting at most
10,000 runs and 500 endpoints. Missing stores stay missing. Availability depends
on run retention and explicit archives; this is not a lifetime bill. Wire, request
and TLS byte totals remain unknown. Existing budgets still count logical adapter
operations, not a hard HTTP/bandwidth ceiling. Named-dataset REST captures and
repairs now retain separate `history_rest` runs in import mode, including failed
catalogue or fetch attempts. Their logical fetch budget remains separate from
observed transport counts. MCP `get_http_accounting` exposes the same bounded
configured-mode read. For a named dataset:

```bash
uv run crypto-market-analysis http-accounting --data-dir data/research --mode import --task history_rest --format markdown
```

Each explicit REST operation reserves 256 KiB of dataset admission headroom for
its two bounded accounting records and SQLite allocation before requesting data.
Accounting errors do not replace the original capture outcome: returned
`http_accounting_retention` and run IDs disclose successful or failed retention.
An abrupt process death may interrupt the final accounting commit; missing
measurement is unknown work, and reconciliation never invents a completed count.

## Finite automatic history repair

```bash
uv run crypto-market-analysis auto-repair-history --data-dir data/research --mode import --request examples/automatic-repair.json --format markdown
uv run crypto-market-analysis auto-repair-history --data-dir data/research --mode import --request examples/automatic-repair.json --apply
```

The directory must already contain a named Kraken import dataset. Inspection
neither initializes nor recovers it. `--apply` opts into one finite automatic
cycle: discover gaps, resume durable tasks, capture bounded recent ranges and
recompute remaining holes. Completed coverage does not trigger new capture on a
repeat. No daemon is started.

Automatic apply acquires a fail-fast cooperative OS writer lock before recovery
and authoritative planning. Existing-dataset `history-dataset` CLI actions share
that guard, including manual repair, staging and legacy status (whose constructor
can recover pending journals). Configured maintenance, archive restoration and
legacy CLI commands that can initialize a store also acquire it; named-dataset
backup additionally requires the existing explicit quiescent declaration. A
competing invocation returns `status: busy`,
`applied: false` and CLI exit 1, without capture or recovery. Read-only automatic
inspection and MCP status never acquire the lock or create its file. Lock release
on normal exit or process death permits the next writer; never delete the stable
`.history-writer.lock` file while a writer may be running. Direct programmatic
`HistoryDataset` writers must hold `dataset_lock.dataset_writer_lock(database)`
around construction/recovery and the complete operation. Arbitrary external
writers and simultaneous creation of a new dataset are outside this guarantee.
Create new datasets in their own isolated empty directory. Linux locking is
process-tested; the Windows fallback remains unverified in this environment.

Kraken's 720-entry recent window includes the current open hour. Only the preceding
719 closed hours are API recoverable; older gaps need archives, future hours stay
unclosed, and active manual repairs block automatic work for that market. Only
the task's range is materialized, with full raw-response hashes/evidence retained.
Import lineage and actual receipt times remain intact.

The example permits four OHLC calls and 24 total hours per cycle. Selected requests
share hours: four eligible long gaps receive six hours each; short ranges release
unused shares. Defaults are eight ranges, 720 hours, four calls and 60-second
initial backoff; dataset configuration also caps requests. Known Retry-After and
bounded exponential backoff apply between invocations without sleeps. A single
durable round-robin cursor rotates eligible native markets even with one call or
hour per cycle. Each attempted turn advances atomically with its inflight task
checkpoint before transport; interruption consumes that turn. Manual ownership,
unsupported markets and retry deadlines still exclude work. Inspection reports
the cursor without advancing it. `get_auto_repair_status` reads current mutable
checkpoints and scheduling state through MCP, without planning, applying,
recovering or reconstructing past state.

Dataset reports must be outside the dataset and must not overwrite their
configuration, archive manifest or source parts. Validation precedes recovery.
Hardlinked report destinations are rejected before opening them for writing.

Payloads and pending journals are staged, flushed and atomically renamed before
materialization. Existing content-addressed files are checked before reuse.
Recovery validates raw and normalized hashes, regular-file identity and byte/row
bounds before writing candles; raw archive verification uses bounded hash reads.
Corruption is an explicit failure and never causes silent evidence replacement.
Temporary files orphaned by a kill are not published evidence and still count
toward the configured disk budget; no automatic cleanup deletes unknown files.

## Process-death recovery verification

```bash
uv run --frozen python tools/verify_crash_recovery.py --output /tmp/crash-recovery.json --deadline-seconds 120
```

The bounded POSIX experiment kills real synthetic workers at observed durable
capture and archive boundaries, then checks exact receipt times/hashes, missing
hours, idempotency and SQLite rollback. It records source hashes, each scenario
and a hard process-group deadline outside Git. These checks establish behavior
after process death, not filesystem corruption or machine power-loss tolerance.

## One coherent analysis dossier

After creating the synthetic fixture in [README](../README.md):

```bash
uv run crypto-market-analysis analysis-dossier --data-dir data/demo-docs --request examples/analysis-dossier.json --as-of 2026-10-28T00:00:00Z --format markdown
cat examples/analysis-dossier.json | uv run crypto-market-analysis analysis-dossier --data-dir data/demo-docs --request - --as-of 2026-10-28T00:00:00Z
```

MCP `get_analysis_dossier(request, as_of)` uses the same calculation. Request
`get_request_schema('get_analysis_dossier')` for nested fields and an executable
synthetic example covering venue, derivatives and quote sections.

A dossier selects one to four explicit source/asset/native EUR or USD contexts.
Each can request stored venue disagreement, amount-specific costs with explicit
assumptions, derivatives in their separately declared native quote, and FX or
stablecoin context. Known-at upcoming calendar evidence is included. Missing
inputs and optional sections not requested remain distinct; USDT derivatives
never become an EUR price or a cross-quote aggregate.

Every section uses one isolated read-only SQLite transaction, cutoff and physical
watermark. Committed WAL data is visible at the start; a later write cannot change
only later sections. Existing tools retain their independent contracts. Frozen
hashes/references and complete details remain available through result paging;
four native market summaries stay visible in compact MCP replies.

Bounds: 64 KiB request, 100,000 total candles, 100 calendar rows, 16 sources per
selector, 5,000 derivative vintages per market and 32 MiB canonical full-result
JSON. Indented CLI JSON can be larger. The shared reader bounds returned SQLite
text to 64 MiB, individual values to 2 MiB, VM work to 50 million steps and real
lifetime to 15 seconds. Exceeding a bound fails explicitly; narrow the request.
Cache expiry and eviction still apply to large frozen results.

## Portable real-source qualification

```bash
uv sync --frozen
uv run crypto-market-analysis qualify-installation --data-dir data/qualified --request examples/installation-qualification.json --format markdown
uv run crypto-market-analysis qualify-installation --data-dir data/qualified --request examples/installation-qualification.json --refresh --output output/qualification.json
```

Inspection works against missing directories without creating a store. `--refresh`
or JSON `refresh: true` requests finite live capture using the actual clock.
Historical clocks, demo/import refresh and named-dataset refresh are rejected.
BTC/EUR, ETH/EUR, BTC/USD and ETH/USD independently report access, coverage, gaps,
revisions, freshness, rights and France entity/service evidence. Success or old
coverage for one pair cannot hide a failed refresh for another.

Defaults: public Kraken, seven recent days, two pages per native market and 64
logical operations. Maximums: four sources, 30 recent days, four pages and 256
logical operations. Query and storage admission ceilings also apply. The example
requests 730-day qualification: recent-only capture cannot satisfy that history
requirement. Reports still explain missing data and the archive route.

For older Kraken history use locally obtained, verified official parts through
`history-dataset init`, `stage` and `status`; inspect the named directory with
`--mode import`. `stage` materializes a selected bounded member without automatic
gigabyte downloads. Recent named gaps use the automatic repair route above.

JSON and Markdown reports include software/module hashes, selection identity,
clocks, measured HTTP coverage and separate adapter budgets. Exit codes: 0 for a
qualification pass, 1 for valid incomplete/failed acceptance, 2 for invalid input.
The report remains available on nonpass. MCP `get_installation_qualification`
permits inspection only and rejects refresh. Real execution needs a network
permitting provider hosts, without requiring deployment. This cloud still excludes
those hosts, so real provider acceptance remains unverified.

## Reproduce bounded endurance experiments

```bash
uv run --frozen python tools/benchmark_endurance.py --output /tmp/endurance-default.json
uv run --frozen python tools/benchmark_endurance.py --virtual-days 2 --cycles-per-day 24 --readers 4 --reads-per-cycle 2 --timeout 180 --output /tmp/endurance-scale.json
```

The network-blocked synthetic harness exercises actual collection/scheduling,
SQLite, concurrent MCP handler/SDK reads and paging, coherent dossiers, HTTP
coverage, archives, expiry and quotas. Two worker processes share durable state
across a real graceful restart. Independent assertions check native values,
clocks, modes, shared watermarks, hashes and read-only inventory; a mismatch fails
the run. Reports outside Git record revision, parameters, timings, RSS/storage
samples and limitations.

Default seven virtual days at four samples/day produce 29 endpoint samples, not
a real seven-day soak or every five-minute event. HTTP traffic is zero by
construction, distinct from uninstrumented stored counters. Resource deadlines
use real monotonic time. This harness does not measure MCP socket framing,
power-loss recovery, distributed scale or production SLOs; regular tests separately
exercise local STDIO/HTTP transports.
