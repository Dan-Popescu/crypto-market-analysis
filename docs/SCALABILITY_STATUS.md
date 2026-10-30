# Version 0.9.0 scaling delivery and acceptance evidence

The software in [SCALABILITY_PLAN.md](SCALABILITY_PLAN.md) is implemented and
reviewed. [SCALABILITY.md](SCALABILITY.md) gives commands and operating limits.
The target is 100 explicitly configured assets, extensible to 1,000. Asset count
is not a claim about exchange support, every source/quote combination, continuous
freshness or financial advantage. No permanent worker or public deployment is started.

## Delivered and still empirical

| Project | Delivered software | Empirical acceptance still needed |
| --- | --- | --- |
| Real operation | Finite resumable public collection, full workload fingerprints, per-cycle native quality/process evidence, durable due clocks and explicit errors | Reachable real providers and actual elapsed weeks; current destination policy blocks the selected hosts |
| Cost control | Per-source/task measured HTTP reports, unknown coverage, declared-price estimates, process CPU/RSS and storage/row deltas, explicit collection-demand planning | Real monthly traffic/storage and actual infrastructure invoices; synthetic counters are not charges |
| Prospective evaluation | Frozen hashed protocols bound to immutable issuance, aligned prediction/observation matching, calibration and funded-plan comparison, indexed bounded cohort projections | Actual eligible future observations and sufficient disjoint origins; 90/180-day results require elapsed time |
| Recalculation/evidence | Shared request preparation, public snapshot capture, explicit compact reference mode, verified cached analytical fleet reads | Workload-specific capacity monitoring; full mode retains its existing bounds |
| Asset scaling | Generic explicit asset validation/calculations, fair bounded collection continuation, 1,000-asset fleet aggregation and cohort pages | Provider capability/identity verification and feasible source/quote cadence for the chosen universe |

No paid API or new third-party dependency was introduced. BTC/ETH remain defaults.
Generic mathematical support never invents provider-specific option/ETF/network
coverage. Native quotes and live/import/demo evidence stay separate. Named archive
datasets retain their original identities.

## Exact verification

Baseline: `16ed91cdf0821bc5239548151bda14532e169285`.
Tested application/tests/tools revision:
`3a785d8ac954f74bd540f1b36c8391e880724c14`.
Subsequent delivery documentation changes no source, tests, tools or dependencies.
Version is 0.9.0; the lock still resolves 73 packages.

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
```

Required final check: coherent lock, Ruff clean, **1,422 tests passed in 157.29s**.
Local HTTP/TLS and actual MCP subprocess transports were included with loopback
permission. No required offline check was unavailable. An earlier integrated run
had five failures: obsolete asset/help/tool-count expectations, a missing-snapshot
inspection contract, and corruption now detected first by projection foreign keys.
All were corrected and rechecked; archive checksum verification remains independently
covered for legacy journals as well as the newer projection guard.

At the exact tested revision, actual process workflows passed:

- **97 CLI commands**: 25 accumulation, 15 capacity/dossier, 14 repair, 18 earlier
  analytical workflows and 25 new scaling workflows.
- **17 actual STDIO MCP calls** across the earlier analytical and new scaling
  harnesses, including seven new tools, frozen page reconstruction, wrong-hash,
  path-escape and refresh refusal. All **53 tools** advertise read-only annotations.
- The new harness checks 1,000 synthetic assets including SOL, 48,000 hourly candles
  and five exported partitions. Fleet counts/native values agree, and database plus
  complete snapshot-file hashes are unchanged after read-only CLI/MCP operations.
- The wheel contains **74 Python modules** byte-identical to tested source. Outside
  the checkout, missing-store qualification stays noninitializing and the server
  advertises 53 read-only tools. ZIP integrity and version metadata pass.
  SHA256: `bfc837ab4553fbfeb883140abcf647c21f3a884212a9503c874ed23391d71b58`.

## Storage measurements on the final revision

The engines read matching native/versioned fixtures: two sources, two quotes,
48 hours, two vintages, 100/1,000 assets and three timing repeats. Every engine's
ordered values have the same expected digest. Cached DuckDB timings include its
public fleet interface and file-identity checks; first-opening timings include
full file/content verification. These are first-verification timings, not a
claim that OS filesystem caches were cold.

| Assets | Vintage rows | SQLite fleet | DuckDB verified/cache warm | DuckDB first verified fleet | PostgreSQL server fleet |
| --- | ---: | ---: | ---: | ---: | ---: |
| 100 | 38,400 | 75.2 ms | 44.6 ms | 523.2 ms | 107.0 ms |
| 1000 | 384,000 | 758.0 ms | 278.9 ms | 4796.4 ms | 1075.5 ms |

PostgreSQL 17.11 ran in an isolated temporary Docker container, no network, two
CPUs and 512MiB. Server execution, client startup/round trips and Python decoding
have different scope; no concurrent-writer or production-throughput conclusion is
claimed. Only the explicitly created container ID with its matching ownership
label was removed, including its temporary volume. The initial generic cleanup
command was rejected by automatic review and was not executed; verified ownership
resolved that risk before cleanup. No existing container was modified or removed.

The measurements support the [storage decision](adr/0001-storage-for-scaled-research.md):
retain SQLite for selective operational/journal work; use explicitly exported,
immutable Parquet/DuckDB for repeated fleet research. The first verification cost
is visible, not omitted to advertise raw SQL timing as end-to-end latency.
PostgreSQL remains a candidate for proven multi-writer/service requirements with
transactional migration parity, not a current application dependency.

## Operating-read measurements

A separate isolated synthetic fixture per scale contained 20,000 retained run
measurements and 24 hours for each asset. These are stored synthetic counters,
not 20,000 actual HTTP requests. At 1,000 assets, all 1,000 native health groups were
retrieved through 10 pages in ~87ms; per-source cost aggregation took ~51ms and the
process peak RSS was ~45MB. They establish indexed bounded reporting on this
fixture, not real API latency or months of live storage growth.

The final exact revision/harness SHA256 are embedded in both benchmark reports.
Commands:

```bash
.venv/bin/python tools/benchmark_operating_scale.py --assets 100 1000 --runs 20000 --output /workspace/work/scalability/verification/operating-scale-3a785d8a.json
.venv/bin/python tools/benchmark_storage_scaling.py --assets 100 1000 --hours 48 --repetitions 3 --rows-per-file 200000 --postgres-container <owned-disposable-observer-storage-bench-container> --output /workspace/work/scalability/verification/storage-scale-3a785d8a.json
```

## Independent review

**Standards:** the initial two documented P2 owner violations (private dossier
composition and direct journal selection) and the private read-helper coupling
were fixed. Public DossierCapture, journal-owned cohort projection/selection and
shared bounded read interfaces now own their invariants. Canonical serialization
and the shared clock parser use public owners. Final narrow review found no
outstanding finding and independently verified error-path admission release and
cached reads during a concurrent verification.

**Spec:** the decimal-string threshold mismatch was reproduced and fixed before
hashing. Canonical immutable freeze retries and the half-open issuance interval
were also verified. Final review found no unresolved validated finding in native
identity, fair continuation, operating clocks/costs, frozen cohort denominators or
partition/vintage selection. Eleven final targeted integration regressions passed.

Both axes preserved these boundaries: legacy/unbound issues remain excluded,
off-anchor forecasts remain unscored, elapsed market acceptance is not synthetic,
and source/deployment/investment claims require their own evidence.

## Remaining operating boundaries

- The current enforced host policy excludes Kraken/Coinbase/Bitstamp endpoints.
  Real-source acceptance is blocked; no key or deployment is needed for the public
  route once executed from an environment permitting those destinations.
- HTTP, SQLite, export, verification and DuckDB limits are cooperative application
  bounds, not OS CPU/RAM/disk quotas. DuckDB disables spill; oversized requests
  fail explicitly. Two SQL readers and one cache-miss verification are admitted
  per process, and excess work fails fast for retry.
- The large-universe collector uses serial fair dispatch; simultaneous monitoring
  does not mean 1,000 simultaneous HTTP connections. Rate limits, coverage, selected
  venues/quotes and order-book cadence determine actual freshness. No production
  SLO or distributed multi-writer capacity has been demonstrated.
- Partitioned snapshots preserve one exported selection and all its vintages.
  They do not automatically prune hot SQLite, route historical dossier reads,
  replace backups or establish current book/calendar/journal consistency. Large
  histories need explicit slices/storage budgets and measured retention policy.
- Dataset/collector coordination remains local; remote multi-writer operation
  requires the adapter/lease/replication work described in the storage decision.
- Statistical support is not calibration or financial edge. Actual invoices,
  multiweek uptime, API retention and future investment outcomes remain unknown.

Local evidence root: `/workspace/work/scalability/verification`, including
`check-integrated-final.log`, `scalability-cli-3a785d8a/results.json`,
`analysis-cli-3a785d8a/results.json`, `cli-3a785d8a/results.json`,
`repair-cli-3a785d8a/results.json`, `wheel-3a785d8a.json`,
`provider-policy-preflight.json` and the two final benchmark reports. The existing
25-command report is at
`/workspace/work/improvements/verification/cli-smoke-results-3a785d8a.json`.
Generated data, reports, containers and distribution artifacts stay outside Git.
