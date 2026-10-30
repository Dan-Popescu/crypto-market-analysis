# Version 0.7.0 delivery and operating evidence

The subsequent [version 0.7.1 reliability record](REPAIR_RELIABILITY_STATUS.md)
adds repair accounting, durable fairness, shared writer ownership, atomic evidence
publication and real process-death checks. This page preserves the 0.7.0 evidence.

All five projects in [OPERATING_CAPACITY_PLAN.md](OPERATING_CAPACITY_PLAN.md) are
implemented. [OPERATING_CAPACITY.md](OPERATING_CAPACITY.md) gives runnable commands,
request examples and limits. Real provider qualification remains a separate
execution step on a network permitting those providers; deployment is unnecessary.

| Project | Delivered |
| --- | --- |
| HTTP measurement | Actual public transport attempts, responses, failures, decoded response bytes and latency; bounded source/endpoint accounting retained with runs and exposed through CLI/MCP. Shared sessions/adapters preserve active-source attribution. Unsupported instrumentation remains unknown. |
| Endurance | Network-blocked, accelerated multi-day collection/scheduler/SQLite workload, concurrent MCP reads and full detail paging, two-process restart, archive/cache/quota checks, native-value and read-only oracles. |
| Automatic repairs | Read-only discovery and explicit finite apply cycles for named Kraken import datasets; fair bounded hour allocation, durable checkpoints, retention-aware ranges, manual-plan protection and persistent no-progress/retry backoff. |
| Coherent dossier | One read-only SQLite transaction, cutoff and watermark across up to four native contexts and requested stored venue, cost, derivative, quote and calendar sections. Compact MCP retains native summaries; frozen details remain retrievable. |
| Portable qualification | Four independent native-market results, read-only default, explicit bounded public refresh, software/selection hashes, chronology/resources, Markdown/JSON and meaningful exit codes. The installed wheel runs outside the checkout. |

## Exact revision and verification

Tested, independently reviewed and measured application revision:
`6f6a4d322b349800bada5e4be988f1bc2d5de3c2`.
Baseline: `f2bf337eb7b3fd733f7b9cc3f84a06aae0b18d9f`.
The subsequent delivery documentation changes no application code.
Third-party dependency versions did not change; application version is 0.7.0.

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
MPLCONFIGDIR=/workspace/work/improvements/mpl-cache .venv/bin/python /workspace/work/improvements/verification/cli_smoke.py
MPLCONFIGDIR=/workspace/work/improvements/mpl-cache .venv/bin/python /workspace/work/capacity/verification/cli_capacity_smoke.py
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/capacity/dist
uv run --frozen python tools/benchmark_endurance.py --virtual-days 7 --cycles-per-day 4 --readers 2 --reads-per-cycle 1 --timeout 60 --output /tmp/endurance-default.json
uv run --frozen python tools/benchmark_endurance.py --virtual-days 2 --cycles-per-day 24 --readers 4 --reads-per-cycle 2 --timeout 180 --output /tmp/endurance-scale.json
```

- Lock coherent: 73 packages; Ruff clean; **1,079 tests passed in 89.23 seconds**.
  New tests include actual loopback transport, shared sessions/adapters and nested
  error provenance, partial/empty repair responses across moving retention and
  restarts, concurrent dossier writers, resource ceilings, CLI stdin/opt-in/output
  protection and actual local MCP transports. No public provider was contacted.
- Existing **25-command CLI smoke passed**, including four independent synthetic
  asset/quote contexts and briefs, costs, funding, immutable issuance/retry,
  outcomes and journals. Artifacts:
  `/workspace/work/improvements/verification/cli-6f6a4d32`.
- Additional **15 actual CLI invocations passed**: discoverable help, missing-store
  JSON/Markdown inspections, bounded stdin, four native market qualification and
  coherent dossier rendering, with unchanged stored inventory. Complete synthetic
  coverage correctly remains unqualified as real-source evidence. Artifacts:
  `/workspace/work/capacity/verification/cli-6f6a4d32`.
- **42 MCP tools** are registered read-only, including the four new surfaces.
  Missing-store inspection leaves its configured directory absent; MCP rejects
  refresh. Exact schema examples execute through the registered interface.
- Wheel ZIP/metadata integrity passed; all **57 packaged Python modules** match
  the tested source bytes. Qualification executed from an extracted wheel outside
  the checkout, with no Git repository and no database creation on inspection.
  Artifact: `/workspace/work/capacity/dist/crypto_market_analysis-0.7.0-py3-none-any.whl`.
  SHA256: `35d927e4dcf6f74254bb5de67ef589ca80810baa8bb2bce488e6d59938223b37`.

## Serial local endurance measurements

Both experiments ran successively from a clean worktree pinned to the exact
revision above, after other heavy verification finished. Runtime: Python 3.12.14,
Clang 22.1.3. Financial evidence was explicitly synthetic/demo.

| Measurement | Default profile | Increased reader load |
| --- | ---: | ---: |
| Virtual duration / endpoint samples | 7 days / 29 | 2 days / 49 |
| Readers / reads per cycle | 2 / 1 | 4 / 2 |
| Real elapsed | 11.688 s | 60.380 s |
| Adapter calls / returned candle rows | 480 / 5,400 | 790 / 9,120 |
| MCP handler calls | 1,211 | 8,055 |
| Coherent dossier / HTTP-accounting reads | 60 / 60 | 394 / 394 |
| Collection p50 / p95 | 190.89 / 258.29 ms | 299.28 / 603.74 ms |
| MCP handler p50 / p95 | 3.33 / 76.12 ms | 3.54 / 126.79 ms |
| Warm RSS median / maximum | 138.00 / 144.08 MB | 141.54 / 167.21 MB |
| Whole-process peak RSS | 171.70 MB | 196.61 MB |
| Sampled SQLite / archive bytes | 4,145,152 / 1,406,501 | 6,172,672 / 1,375,691 |
| Actual process restarts / correctness mismatches | 1 / 0 | 1 / 0 |

Storage figures are the last scheduled-cycle samples before lifecycle probes;
they are not peak storage guarantees. Sampled WAL was zero at those endpoints,
which does not establish peak WAL usage. Final checked archive inventories after
lifecycle probes contained 15 files/2,024 rows and 10 files/2,019 rows respectively.
RSS includes imports, setup and the bounded quota-probe allocation; warm samples
exclude the first two cycles in each worker.

Every lifecycle assertion passed: checked archives, dry-run inventory preservation,
hot-budget refusal, archive-budget refusal without hot loss, wrong-hash rejection,
cache expiry/byte limit and complete frozen-page reconstruction. Maximum serialized
MCP result was **32,000 bytes**. Read-only database hash/inventory checks passed.
Native values, modes, chronology and all dossier cutoffs/watermarks matched.

Two early concurrent dossier reads in the increased-load profile correctly
reported missing context before first capture; all 392 later dossiers had the
expected native prices. Injected partial-source failures remained visible.
Stored HTTP totals stayed uninstrumented/null; zero egress was established
separately by the network guard, not inferred from those unknown counters.

Raw reports:
`/workspace/work/capacity/verification/endurance-default-6f6a4d3.json` and
`/workspace/work/capacity/verification/endurance-scale-6f6a4d3.json`.

## Independent review

### Standards

One reported P2 correctness finding, fixed: a shared requests adapter retained
its first source label. Context-local per-call provenance now handles sequential,
concurrent and nested uses. The reviewer independently verified both sequential
and concurrent shared-session accounting. No substantiated maintainability-rule
violation or remaining actionable finding was reported.

### Spec

Two reported P2 findings, fixed: shared-session attribution and aged repair ranges
mistaking empty responses for progress. Progress now requires actual materialization
of previously missing requested hours. The independent original reproduction now
retains 713 missing hours/one archive-only hole while growing failures 1/2 and
retry delays 60/120 seconds. Concurrent calendar cancellation, four-way repair
allocation and read-only qualification probes also passed.

Review counts: Standards 1 P2 fixed; Spec 2 P2 fixed, including the shared finding.
Remaining actionable findings: zero on each axis at the final application revision.

## Remaining execution boundaries

These measurements establish bounded local behavior, not a real multi-day soak,
production SLO or distributed capacity. Virtual clock/cadence profiles differ;
latencies are not market-data endpoint timings or a standalone RPS guarantee.
The harness exercises real MCP handlers/SDK serialization, while separate tests
cover local STDIO/HTTP transport. The process restart is graceful, not power loss.
Archive/write admission limits are not OS filesystem quotas or hard total-RAM caps.

The portable capture/qualification route is ready, but this cloud's enforced
network policy excludes provider hosts. Real endpoint formats, access, actual
four-market history, current freshness, fees and source-use permission remain
unqualified until explicitly captured from an authorized reachable environment.
Recent Kraken API data cannot satisfy a 730-day requirement without suitable
archives. No deployment or account key is required for the public local route.
No paid API, order, transfer, external notification, permanent worker or pruning
of user data was activated. Prospective financial results still need real future
observations and elapsed time.
