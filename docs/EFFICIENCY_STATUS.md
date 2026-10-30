# Version 0.6.0 delivery and measured efficiency

The subsequent [version 0.7.0 operating-capacity record](OPERATING_CAPACITY_STATUS.md)
covers HTTP measurement, endurance, automatic repair, coherent dossiers and
portable qualification. This page preserves the original version 0.6.0 evidence.

All six priority projects in [EFFICIENCY_PLAN.md](EFFICIENCY_PLAN.md) are
implemented and verified locally. [EFFICIENCY.md](EFFICIENCY.md) documents
operation, limits and migration. No deployment is required for these changes.

| Area | Delivered |
| --- | --- |
| History correctness | Native selectors and clocks applied before SQL ranking/limits; byte-bounded pages advance only from emitted points; detectable retention changes invalidate cursors explicitly. |
| Collection cost | Incremental default refresh, explicit backfill, shared clients/catalogues, durable independent source/task clocks and bounded retry backoff respecting known Retry-After deadlines. |
| MCP efficiency | Bounded summaries, explicit omissions, immutable hash-bound detail paging, TTL/LRU cache limits and preserved useful plan summaries with detailed trajectories paged. |
| Storage conservation | Configured admission/archive budgets; dry-run maintenance; checked, synced archives before bounded pruning; interruption/retry/restore handling and immutable evidence protection. |
| Agent discovery | Native-series discovery, nested request schemas/examples, dataset/repair state, collection history, storage status and known-at upcoming calendar reads. |
| Measurement | An offline baseline/treatment harness records adapter calls, wire-result bytes, hashes, selected history, representative memory/storage, scheduler restart and archive/quota behavior. |

One-shot collection retains its combined public report while persisting precise
task results. A book failure cannot poison candle scheduling on the next startup.
Legacy compound failures remain ambiguous; the scheduler does not invent success
for a component without evidence. Precise successes reconcile same-second legacy
state without increasing failure counts on each restart.

MCP startup and operational inspection do not initialize, migrate or recover the
configured store. Analytical reads of a missing database fail explicitly.
Committed WAL data remains visible through read-only SQLite connections.

## Exact revisions and checks

Final full verification: `df793f5965baf005fbff6a017a1fdf498ad4f415`.
Benchmark, CLI smoke, wheel build and independent two-axis review:
`050bbdefd43a88345a696cf624849bf4b3f92525`.
The later commit only updates an existing test spy to retain all task requests.
Application sources, tools and dependency manifests match between these commits;
the wheel's source bytes were checked again at the final verification revision.
The subsequent delivery documentation changes no application code.

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
UV_CACHE_DIR=/workspace/.cache/uv uv run --frozen python tools/benchmark_efficiency.py --output /workspace/work/efficiency/verification/efficiency-050bbde.json
MPLCONFIGDIR=/workspace/work/improvements/mpl-cache .venv/bin/python /workspace/work/improvements/verification/cli_smoke.py
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/efficiency/dist
```

- Lock coherent: 73 packages; Ruff clean; **947 tests passed in 69.55 seconds**.
  Tests include actual local MCP transports, sparse history beyond 200,000 noise
  rows, paging/hash/expiry limits, ordinary and scheduled partial failures,
  restart state, read-only lifecycle, WAL visibility, archive publication ordering,
  interrupted archives, quotas and immutable evidence safeguards.
- All **13 benchmark checks passed**, with no public network request. The report
  records a clean treatment source revision and the immutable baseline
  `bca6a39091e4b9f0d5bebbf6519b8b14bd208607`.
- The existing **25-command actual CLI smoke passed**, covering separate
  synthetic BTC/ETH and EUR/USD contexts, funding, costs, issuance, retries,
  outcomes and journal reports. Artifacts:
  `/workspace/work/improvements/verification/cli-050bbdef`.
- All **38 MCP tools** are registered with read-only annotations. An actual
  missing-directory construction check created no configured store.
- Wheel ZIP integrity passed and all **51 Python modules** matched the tested
  source bytes. Artifact:
  `/workspace/work/efficiency/dist/crypto_market_analysis-0.6.0-py3-none-any.whl`.
  SHA256: `45ce6f1ea2c24f0cb81078995979d4a3c8ea1f1b6ebbb4477eca913b6601d26d`.

## Synthetic before/after evidence

| Measured fixture | Baseline | Version 0.6.0 |
| --- | ---: | ---: |
| Returned candle rows on a second refresh | 719 | 2 |
| OHLCV adapter fetches on that refresh | 3 | 2 |
| Serialized derivative MCP reply, including SDK text/structured copies | 1,239,754 bytes | 11,518 bytes |
| Selected points recovered behind 210,000 unrelated observations | 0 / 10,000 | 10,000 / 10,000 |
| Selected-history first-page query p50 / p95, five samples | 1,642 / 1,750 ms | 56 / 64 ms |
| Unnecessary source calls after restart behind 250 newer unrelated runs | 4 | 0 |
| Selected-history process peak RSS, including setup/imports | 224,358,400 bytes | 66,342,912 bytes |
| Representative history SQLite file | 116,154,368 bytes | 126,107,648 bytes |

Full derivative details reconstructed through 39 pages with matching hashes and
unchanged tested financial values; the largest serialized detail page was 32,000
bytes. History returned all selected points through 257 pages without gaps or
duplicates. Its baseline incorrectly returned none: first-page timings do not
establish an end-to-end speedup for retrieving the entire history. The additional
SQLite index costs about 10 MB in this fixture.

The storage fixture archived, checked and restored 39 rows in 24,215 bytes.
Dry-run preserved inventory; hot-budget exhaustion refused writes; archive-budget
exhaustion refused pruning while preserving hot evidence. Sync-failure tests
also preserved hot rows, including existing-archive retry paths.

## Independent review and remaining limits

Standards and Spec reviewers reported no remaining actionable findings on the
final application revision. Independent temporary-store/adapter probes verified
archive sync ordering, missing-store MCP behavior, WAL reads and both scheduled
and one-shot collection followed by restart. Findings discovered during review
were fixed and rechecked before delivery.

This remains a bounded single-host pipeline. Adapter counts are not wire HTTP
counts, cache byte limits do not bound total process RAM, and SQLite admission
estimates are not filesystem quotas. DELETE reuses pages without shrinking the
database; external writers and long-lived WAL readers remain outside a hard
application quota. Dense compaction batches and exhausted archive budgets fail
explicitly. Distributed throughput and production SLOs have not been measured.

Real-source access and raw provider-format acceptance remain separately
unqualified in this cloud, whose enforced allowlist excludes market/ECB hosts.
See the historical [data connection evidence](DATA_CONNECTION_STATUS.md). These
fixes add no paid API, account key, order, external notification, permanent worker,
deployment or automatic pruning of user data. Prospective investment results
still require actual subsequent observations and elapsed time.
