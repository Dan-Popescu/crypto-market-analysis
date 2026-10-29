# Operating the bounded local pipeline

Version 0.6.0 implements the [efficiency acceptance plan](EFFICIENCY_PLAN.md).
The calculations retain native quotes, modes, source evidence and unknowns.
This is a bounded local architecture; measurements do not establish a distributed
throughput guarantee.

## Incremental refresh and restart

`local-workflow` now resumes near each existing series' latest candle. Its
`history_days` setting controls bootstrap for missing series. To deliberately
refetch an older interval, add `backfill_days` (1–3650) to an explicitly live,
refresh-enabled request. Missing older ranges still have their explicit repair
workflow; an incremental cursor does not certify gap completeness.

The scheduler keeps durable clocks per source/task, separately tracking attempts,
successes, failures and next due time. Startup scans indexed grouped run state
rather than the newest 200 log rows. Successful sources retain their cadence;
failed sources back off independently. Scheduled market and liquidity tasks have
separate reports: a failed book request cannot reset a successful candle task.
Known `Retry-After` deadlines are respected
without a long blocking worker sleep. Context providers can be scheduled separately.
The scheduler remains a single local orchestrator, not a distributed job queue.

## Select history before paging

Use MCP `get_metric_series` to discover available source/quote/unit/dimension
identities. `get_metric_history` accepts those selectors plus `as_of`, `start`,
`end` and `cursor`. SQL applies filters before its limit. Do not omit a native
selector when the distinction matters.

Pages pin their cutoff and row watermark and advance from the last emitted
point, including when the 10,000-byte history response budget returns fewer rows
than requested. `truncated`, `next_cursor` and truncation reasons describe that
actual page. Detectable pruning/revision changes invalidate a cursor explicitly.
Cursors are not durable evidence archives. An individually oversized point fails
explicitly without advancing; narrowing a series cannot manufacture missing data.

## Compact MCP and complete frozen details

Dictionary results fit 32 KiB, including MCP text and structured copies, with
space reserved for framing. Native chart images remain separate image content.
CLI and Python calculation results remain full JSON.

Read `_mcp_result`: it identifies the frozen handle, content and selection hashes,
expiry and any omitted paths. `get_result_page` retrieves bounded structured
pages from that same frozen result, without a new database read. Large individual
values have an explicit Unicode-safe JSON fragment protocol and reconstruction
metadata. Returned financial values retain their exact calculation values;
omissions are marked rather than replaced with a successful empty result.
History points remain a list and only emitted points advance the cursor.

The per-server cache stores canonical JSON bytes: at most 64 MiB and 64 entries,
with a five-minute TTL and LRU eviction. Handles are opaque and hash-bound. Expired
or evicted handles fail clearly; repeat the original calculation, preferably with
a fixed `as_of`, instead of treating an empty page as evidence. Cache limits do
not bound total Python process memory or the cost of the underlying calculation.

`get_request_schema('list')` lists the on-demand contracts. Request one tool name
for required/nested fields, enums, defaults and a synthetic executable example.
Runtime validators still enforce chronology, cashflow and cross-field rules.

## Conservation policy and explicit archives

```bash
uv run crypto-market-analysis storage-policy --data-dir data/live
uv run crypto-market-analysis maintain --data-dir data/live
uv run crypto-market-analysis maintain --data-dir data/live --apply
uv run crypto-market-analysis verify-archive --archive /path/to/archive.json
uv run crypto-market-analysis restore-archive --data-dir data/restored --archive /path/to/archive.json
uv run crypto-market-analysis restore-archive --data-dir data/restored --archive /path/to/archive.json --apply
```

Inspection, maintenance and restore default to no pruning/restoration. `--apply`
is explicit. Maintenance creates and verifies lossless bounded archives before
deleting only the captured, still-matching hot rows. Retries are idempotent;
concurrent new captures are preserved. Backups now include checked retention
archives. Original journal records/outcomes, reference candles and required named
dataset payloads are protected from automatic pruning.

Archive publication syncs the file, archive directory and its parent before hot
deletion can commit. A sync/verification failure preserves hot evidence. These
checks exercise publication ordering and failure handling; they are not a
physical power-loss certification for every storage device.

Defaults: hot SQLite/WAL/SHM admission 2 GiB; archives 4 GiB/4,096 files; raw-book
payload budget 512 MiB; raw books seven days; operational runs 30 days; at most
256 archived rows per family/batch; write reserve 8 MiB. Latest source/task/mode
run clocks remain available even after archiving. Raw liquidity evidence is
archived before existing hourly compaction; a dense hour exceeding its bounded
4,096-row/16-MiB work allowance is refused rather than partially summarized.

These are admission and archive budgets, not absolute filesystem quotas. SQLite
page/index/WAL amplification is estimated conservatively; DELETE reuses pages
without shrinking the file. External processes and long-held WAL readers remain
outside an application-level hard quota. Archive exhaustion refuses new work;
archives are never silently deleted to make room. An administrator can explicitly
move verified archives or adjust policy after inspecting actual usage.

The optional TOML fields use the `storage_` prefix: `hot_max_bytes`,
`archive_max_bytes`, `archive_max_files`, `book_hot_max_bytes`, `book_keep_days`,
`run_keep_days`, `batch_rows` and `write_reserve_bytes`. Scheduler book/run
maintenance is opt-in through `storage_maintenance_enabled = true`; the existing
liquidity compaction cadence remains. No worker is started by this update.

## Operational reads available to the agent

MCP exposes `get_dataset_status`, `get_collection_history`, `get_storage_status`
and `get_upcoming_calendar`, along with schemas and native-series discovery.
Dataset status is limited to the configured location; it never constructs or
recovers a dataset. Current repair/scheduler state is explicitly unversioned;
physical byte counts are current, not historical reconstructions. Legacy runs
without mode evidence are separate from classified live/import/demo runs.
No arbitrary file path, SQL, collection or mutation input is exposed.

MCP startup, including CLI `serve` on either transport, opens the store without
initializing or migrating it. Missing-store operational inspection and pure
request calculations remain available. Analytical database reads require an
existing store and otherwise return an explicit error. Read-only SQLite
connections see committed WAL data and cannot change schema or rows; SQLite may
still manage its WAL/SHM sidecars. Explicit local collection/import or another
writable CLI operation initializes/upgrades a store separately.

## Reproduce local efficiency evidence

```bash
uv run --frozen python tools/benchmark_efficiency.py --output /tmp/crypto-efficiency.json
```

The harness compares immutable baseline `bca6a39` against the current tree using
isolated synthetic fixtures and no public sources. It measures adapter cursors,
serialized MCP replies, lossless paging/hash checks, filtered SQL history,
representative memory/storage, scheduler restart and archive/quota behavior.
The report records rows, timings, source revisions and scope; it is not a live
market qualification or a production SLO.
