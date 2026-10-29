# Measured operation and coherent local workflows

Version 0.7.0 implements [the five accepted projects](OPERATING_CAPACITY_PLAN.md).
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

Endpoint IDs hash the path and omit URL, query, userinfo, headers and body. Each
run retains at most 32 IDs; overflow preserves totals and reports omitted endpoint
attribution. Operational transport errors keep class/status/deadlines without
URL/body details. Raw evidence archives retain their existing provenance contract.

Aggregation filters source, task, mode and receipt range before selecting at most
10,000 runs and 500 endpoints. Missing stores stay missing. Availability depends
on run retention and explicit archives; this is not a lifetime bill. Wire, request
and TLS byte totals remain unknown. Existing budgets still count logical adapter
operations, not a hard HTTP/bandwidth ceiling. Standalone named-dataset repairs
keep their separately declared OHLC-call budget and do not claim automatic
inclusion in collection-run accounting. MCP `get_http_accounting` exposes the same
bounded configured-mode read.

## Finite automatic history repair

```bash
uv run crypto-market-analysis auto-repair-history --data-dir data/research --mode import --request examples/automatic-repair.json --format markdown
uv run crypto-market-analysis auto-repair-history --data-dir data/research --mode import --request examples/automatic-repair.json --apply
```

The directory must already contain a named Kraken import dataset. Inspection
neither initializes nor recovers it. `--apply` opts into one finite automatic
cycle: discover gaps, resume durable tasks, capture bounded recent ranges and
recompute remaining holes. Completed coverage does not trigger new capture on a
repeat. No daemon is started; use one explicit dataset writer at a time.

Kraken's 720-entry recent window includes the current open hour. Only the preceding
719 closed hours are API recoverable; older gaps need archives, future hours stay
unclosed, and active manual repairs block automatic work for that market. Only
the task's range is materialized, with full raw-response hashes/evidence retained.
Import lineage and actual receipt times remain intact.

The example permits four OHLC calls and 24 total hours per cycle. Selected requests
share hours: four eligible long gaps receive six hours each; short ranges release
unused shares. Defaults are eight ranges, 720 hours, four calls and 60-second
initial backoff; dataset configuration also caps requests. Known Retry-After and
bounded exponential backoff apply between invocations without sleeps. Very small
request/hour budgets retain deterministic ordering rather than promising fair
concurrent progress. `get_auto_repair_status` reads current mutable checkpoints
through MCP, without planning, applying, recovering or reconstructing past state.

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
