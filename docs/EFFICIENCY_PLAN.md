# Reliability, operating cost and MCP efficiency

Baseline: `bca6a39091e4b9f0d5bebbf6519b8b14bd208607`.
The user authorized correction of all priority findings from the pipeline/MCP
audit, with ordering chosen by implementation dependencies and correctness.

## Acceptance and ownership

1. **History correctness first.** Apply metric/source/native quote/dimension and
   clock filters in SQL before ranking and limits. Preserve the existing MCP
   points/truncated contract while adding bounded stable pagination, explicit
   selection references and honest stale/omission behavior. Sparse series must
   not disappear behind an unrelated asset-wide cap.
2. **Incremental collection and durable scheduling.** Local refresh resumes near
   the latest known native series by default; history_days controls bootstrap.
   An explicitly requested backfill remains available. Scheduler restart derives
   per-source/task attempt and success state independently of the last 200 log
   rows. Failures use bounded backoff without violating a known Retry-After;
   persistent sources cannot keep causing healthy sources to repeat needlessly.
3. **Compact MCP, complete evidence.** Return useful analytical values, uncertainty,
   missingness and provenance within a strict response budget. Do not embed whole
   consumed observation trees by default. Opaque bounded read-only evidence/detail
   paging retains hashes, selection identity and truthful expiry. Missing cache
   state is an explicit error, not an empty successful result. CLI/calculation
   JSON remains lossless and existing financial values remain unchanged.
4. **Bound growth without hidden loss.** Expose database/WAL/family/archive usage,
   conservation policies and write headroom. Archive bounded eligible raw books
   and operational history losslessly before explicitly applying hot-store pruning;
   verify checksums and preserve recovery/concurrent captures. Immutable original
   assessments/outcomes, reference candles, known-at vintages and required dataset
   payloads are not silently deleted. Archive quota exhaustion causes refusal,
   not deletion. Inspection/dry-run is the default; real user data is not purged
   during this implementation. Report SQLite/WAL allocation limitations honestly.
5. **Make operational evidence discoverable.** Expose bounded machine-readable
   request contracts/examples, configured dataset status and repair/conflict
   progress, historical operations, calendars and storage/retention state.
   Reads neither initialize/recover a dataset nor expose arbitrary filesystem
   paths, SQL, network calls or mutation controls through MCP.
6. **Measure rather than assert scale.** A reproducible bounded synthetic harness
   compares immutable baseline and treatment: adapter cursors/calls, serialized
   MCP bytes, filtered history/pagination, representative SQL volume and scheduler
   restart. Report rows, timings, bytes, environment and limitations. Correctness
   tests and single-host measurements are not production-throughput guarantees.

History and storage changes have isolated owners; MCP presentation integrates
operational tool registration. Collection owns scheduling and provider retries;
conservation coordinates with its maintenance hooks. Independent changes use
isolated worktrees, followed by integration and two-axis review.

## Preserved contracts

Source identity, native quote, data mode, UTC/availability clocks, immutable
journals and original replay remain load-bearing. No paid API dependency,
exchange account, order, permanent worker or deployment is added. Runtime source
access remains separately qualified. Existing public settings and commands stay
compatible except deliberate documented safer defaults and additive parameters.
All new repository content and software output use English.
