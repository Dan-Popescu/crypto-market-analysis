# Version 0.8.0 analytical workflows: verified delivery

The five projects in [ANALYSIS_WORKFLOWS_PLAN.md](ANALYSIS_WORKFLOWS_PLAN.md)
are implemented. [ANALYSIS_WORKFLOWS.md](ANALYSIS_WORKFLOWS.md) documents their
commands, schemas, evidence semantics and resource bounds. No deployment,
permanent worker, account connection or paid data dependency was introduced.

## Delivered capabilities

| Project | Result |
| --- | --- |
| Analysis changes | Two known-at dossiers share one physical read snapshot. Reports distinguish market observations, revisions, late arrivals, clock/window changes and source failures, preserving native identities, descriptive metric values and exact omissions. Analytical conclusions precede bulk vintage details. |
| Agent readiness | Actual dossier, qualification and optional window estimates determine individual claim states and ordered nonexecuting next actions. All database evidence shares one snapshot; pure window calculations run after its reader closes. Access, reuse rights and France entity/service eligibility remain separate. |
| HTTP budgets | Shared finite request/deadline policies cover supported Requests/HTTPX attempts, catalogues, redirects, observable retries and concurrent workers. Timeouts and owned retry waits use the remaining deadline. Refusals and incomplete enforcement remain explicit. |
| Resumable histories | Explicit finite Coinbase/Bitstamp import cycles checkpoint intent, recheck actual oldest missing hours and preserve receipt provenance. Catalogue/page retry deadlines are durable. Inspection and MCP status do not initialize or resume work. |
| Plan robustness | Fixed declared origins, costs and missing-hour cases reuse Decimal plan accounting. Qualified/excluded denominators, paired differences and sign stability remain descriptive; no winner, optimization or financial advantage is inferred. |

There are **46 read-only MCP tools**, including four new analytical/status tools.
Full evidence uses existing frozen, hash-bound result paging. Native BTC/EUR,
ETH/EUR, BTC/USD and ETH/USD remain separate; import/live/demo modes never merge.
The new modules use public owner interfaces rather than cross-module private
helpers. CLI and MCP dispatch share the same calculations.

## Exact application revisions

Baseline: `f5ab089aeff816a52685d057462a9ef71577fc0f`.
Final application revision: `195efbc396e22c1b595faa2b028100ce6eb23b30`.
Delivery documentation after that revision changes no application, tests, tools
or dependency manifest. Package version is 0.8.0; third-party dependency versions
remain unchanged (73 resolved packages). Generated data and reports stay outside Git.

Required final verification:

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
```

Lock validation and Ruff pass; **1,299 tests pass in 146.91 seconds**. Tests
include local HTTP/TLS and actual MCP subprocess transports. The final run had
loopback network permission; no required offline check remains unavailable.

Additional process-level checks:

- At `a28045f35830d5717a7e2cdb7c48eef063b012ea`, 54 established CLI commands
  passed: 25 accumulation workflows, 15 capacity/dossier operations and 14 repair
  and evidence-protection operations. The only subsequent application change
  closes readiness's reader before its detached calculations; those established
  commands do not call readiness.
- At the final application revision, 18 new actual CLI commands and five real
  STDIO MCP calls passed on four native synthetic 750-day histories. Checks cover
  730-day qualification, both output formats, stdin, missing-store noninitialization,
  unchanged two-snapshot evidence, robustness exclusions and frozen claim paging.
  Observed serialized MCP replies range from 1,929 to 31,484 bytes, below 32 KiB.
- The final 0.8.0 wheel contains 65 Python modules byte-identical to final source.
  Outside the checkout, missing-store qualification preserves the absent path
  and the server advertises 46 read-only tools. ZIP integrity and version pass.
  Wheel SHA256: `8ec32fc7082beb6b725a6ab2c72979a5d43e61a82ca88948568ed9b7e2fbdcae`.

Local evidence directory: `/workspace/work/analysis-next/verification`.
It contains `check-195efbc.log`, `analysis-cli-195efbc3/results.json`,
`cli-a28045f3/results.json`, `repair-cli-a28045f3/results.json` and
`wheel-195efbc3.json`. The 25-command report is at
`/workspace/work/improvements/verification/cli-smoke-results-a28045f3.json`.
These paths are local verification artifacts, not published market evidence.

## Independent review and measured efficiency

The initial Standards and Spec reviews were separate:

- **Standards:** no findings at the integrated `8225db9` revision.
- **Spec:** two P2 issues, both corrected and independently rechecked: missing
  descriptive metric deltas and HTTP deadline enforcement across retry/transport
  stages. The latter included owned retry waits and HTTPS proxy CONNECT behavior.
- **Financial correctness:** one P2 issue, corrected and independently rechecked:
  an explicitly requested missing calendar must affect overall readiness.

A focused review of the qualification/retry follow-up at `63b4d40` found no
concrete issue: 87 focused tests passed, and 200 randomized baseline/current
qualification comparisons matched exactly, including gaps, samples, cutoffs,
revision ordering and complete-stream hashes. The randomized probe ran through
stdin; its script was not retained. The final narrow review of `a28045f` and
`195efbc` found no issue in candle selection semantics or detached readiness
calculations; the reviewer independently passed all 11 readiness tests.

Dense synthetic tests identified and fixed excess repeated SQL work without
raising the Store's 50-million VM-step, 64 MiB returned-text or 15-second reader
bounds. On four native 800-day histories, a dossier fell from approximately
35.482M to 18.585M VM steps with exact result/hash equality. Two full snapshots
consume 41.932M steps, retain 153,600 candles and report no changes for identical
evidence one second apart. An actual CLI probe completed in 11.78 seconds and
left the database SHA256 unchanged. These are local measurements, not throughput
promises across machines or arbitrary database shapes.

## Remaining operating limits

- Public provider acceptance is **not verified here**: enforced cloud network
  policy excludes their hosts. Tests use synthetic data, injected adapters or
  loopback transports. No provider entitlement, full retention depth, native
  availability or intended-use licence is inferred from those checks.
- Readiness retains a complete dossier and has a 32 MiB full-result ceiling.
  The four-market example uses 750 days for 730-day context. The tested 800-day
  four-market dossier plus all window claims exceeds this ceiling and correctly
  fails explicitly; reduce the selection/history. Evidence is not silently cut.
- HTTP deadlines are cooperative supported-transport controls, not arbitrary-code
  interruption, OS quotas or a universal trickled-stream wall-clock guarantee.
  Normalized bytes and wire bytes are different measurements.
- Backfill exclusion is a cooperative local-process lock. SQLite and finite
  bounded calculations target a single host; distributed ingestion/locking and
  permanent operation require separate design and acceptance.
- Imported history has actual late receipt times. Retrospective completeness
  cannot establish original point-in-time knowledge. Real prospective outcomes,
  useful market signals and a financial advantage remain unproven.
