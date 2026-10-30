# Version 0.7.1 repair reliability and verification

The bounded follow-up in [REPAIR_RELIABILITY_PLAN.md](REPAIR_RELIABILITY_PLAN.md)
is implemented. [OPERATING_CAPACITY.md](OPERATING_CAPACITY.md) gives commands and
operating limits. No deployment, paid source, permanent worker or account key was
introduced.

## Delivered

| Area | Result |
| --- | --- |
| Repair HTTP accounting | Named-dataset REST operations measure supported transport attempts, catalogue traffic, observed retries, errors, decoded bytes and durations. Successful and failed operations atomically retain sanitized `history_rest` import runs and attempt records, with run references exposed by CLI/MCP. Capture identities and raw evidence remain unchanged. |
| Low-budget fairness | One durable market cursor advances atomically with each inflight checkpoint before transport. Eight separate one-request process cycles served all four native markets twice, while respecting manual ownership, unsupported pairs and retry deadlines. Dry runs preserve the cursor. |
| Writer ownership | Fail-fast cooperative process locking spans automatic recovery/planning/capture, existing-dataset history commands, configured maintenance/restoration and legacy CLI initialization. Explicitly quiescent named-dataset backup holds the same lock. Contention returns busy/exit 1 before work. Capacity inspection and MCP serving remain noninitializing and available under the lock. |
| Evidence integrity | Payloads and pending journals publish through flushed temporary files and atomic replacement. Existing files are verified before reuse. Recovery validates raw and normalized hashes, regular-file identity and byte/row bounds before materialization. Large raw archive verification streams bounded chunks. |
| CLI protection | Dataset reports cannot overwrite database sidecars, identity, configuration, archive manifests or source parts. Existing hardlinked report destinations are refused before construction/recovery. Public capture errors suppress arbitrary transport text. |
| Process-death checks | Real SIGKILL workers exercise interrupted blob creation, pending journals, complete/partial materialization, archive staging/publication, uncommitted deletion rollback and retention recovery. Repeated recovery preserves original receipt, hashes, exact missing hours and idempotency. |

The existing **42 read-only MCP tools** remain compatible. `get_http_accounting`
reads the additional import runs; `get_auto_repair_status` exposes the market cursor
and last accounting run references. No mutation tool was added. BTC/EUR, ETH/EUR,
BTC/USD and ETH/USD retain separate native identities and import/live/demo lineage.

## Exact application revision and checks

Tested and independently reviewed application revision:
`8bd36bb54ab6265a57150c48d1bda4aef7891ee0`.
Baseline: `ca5fc0e98eb7a4f98e1ef48dcb3767e7b113b176`.
Subsequent delivery documentation changes no application, test, tool or dependency
manifest. Third-party dependency versions remain unchanged; package version is
0.7.1. All generated evidence, databases and artifacts remain outside Git.

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
MPLCONFIGDIR=/workspace/work/improvements/mpl-cache .venv/bin/python /workspace/work/improvements/verification/cli_smoke.py
MPLCONFIGDIR=/workspace/work/improvements/mpl-cache .venv/bin/python /workspace/work/repair-next/verification/cli_capacity_smoke.py
.venv/bin/python /workspace/work/repair-next/verification/cli_repair_smoke.py
UV_CACHE_DIR=/workspace/.cache/uv uv run --frozen python tools/verify_crash_recovery.py --output /workspace/work/repair-next/verification/process-crash-8bd36bb5.json --deadline-seconds 60
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/repair-next/dist
.venv/bin/python /workspace/work/repair-next/verification/verify_wheel.py
```

- Required check: lock coherent, **73 packages**, Ruff clean, **1,150 tests passed
  in 106.24 seconds**. Actual local STDIO/HTTP transports were included. No required
  offline check remains unavailable.
- **54 actual CLI invocations passed**: 25 established native-analysis/plan/journal
  commands, 15 operating-capacity commands and 14 named-dataset operations. The
  latter cover actual init/status/manual planning, import accounting in JSON and
  Markdown, dry-run planning, five busy writer paths, hardlink/evidence protection
  and normal operation after release. Injected synthetic data stayed unqualified.
- The clean-revision process experiment passed **18 recovery/integrity cases**
  in 4.700 seconds with a 60-second process-group deadline. Fifteen cases include
  actual SIGKILL; three check preexisting damaged/nonregular blobs. All child
  processes have bounded rendezvous and cleanup. Network use is denied in workers.
  These are descriptive local checks, not a latency SLO or power-loss simulation.
- Wheel ZIP/metadata checks passed and all **58 packaged Python modules** match
  the tested source bytes. Extracted-wheel qualification and 42-tool discovery
  ran outside the checkout, with unavailable Git provenance reported honestly and
  the missing configured store left absent.

Wheel: `/workspace/work/repair-next/dist/crypto_market_analysis-0.7.1-py3-none-any.whl`.
SHA256: `6e7cd3e3a5cc1284946a6bc7bcbac4d98b0d0d7cda33e04679faf8170f0decdc`.
It was built and checked at the application revision above.

Evidence:

- `/workspace/work/improvements/verification/cli-smoke-results-8bd36bb5.json`
- `/workspace/work/repair-next/verification/cli-8bd36bb5/results.json`
- `/workspace/work/repair-next/verification/repair-cli-8bd36bb5/results.json`
- `/workspace/work/repair-next/verification/process-crash-8bd36bb5.json`
- `/workspace/work/repair-next/verification/wheel-8bd36bb5.json`

## Standards

Independent review found one P2 correctness issue: a hardlinked report destination
could truncate protected evidence despite a different resolved pathname. Fixed
by rejecting existing hardlinked output files before recovery or writes. The
reviewer reran the original raw-payload reproduction: exit 2, byte-identical dataset
and valid raw hash. Held-lock maintenance returned busy while noninitializing
accounting/planning still succeeded. No remaining documented-standard or
actionable maintainability finding was substantiated.

Production fixes were reviewed at `3509d8e38f0802f53340a15608e850e98dfe4053`,
followed by the canonical backup-path correction at the final revision above.
Both direct and symlink backup paths were independently tested under the writer
lock. The final verification record replaces the earlier intermediate revision.
Standards: **1 P2 fixed; 0 remaining**.

## Spec

Independent review found three P2 issues, all fixed and independently re-probed:

- Maintenance bypassed the writer guard. The original locked command now exits 1
  with busy/applied-false, preserves both retained runs and publishes no archive.
- A hardlinked configuration output passed pathname checks. It now exits 2 and
  preserves the original configuration bytes.
- An injected mixed Requests/HTTPX client falsely claimed full coverage. A real
  loopback server receiving two requests now produces a partial measurement of
  the one supported attempt. An unused Requests Session with only HTTPX traffic
  yields uninstrumented/null totals. The owned default CCXT path stays observed.

The reviewer inspected wider guard placement and ran 28 focused tests before the
last backup-path correction, then independently re-probed that correction and ran
both backup-path cases on the final revision. An outside symlink to the dataset
database now returns busy/exit 1 with no destination bundle while locked. No
additional demonstrated bypass or scope creep remained.
Spec: **3 P2 fixed; 0 remaining**, including the shared hardlink finding.

## Remaining limits

Public provider acceptance remains unverified: the enforced cloud network policy
excludes market and ECB hosts. The portable qualification command can run on an
authorized reachable network without deployment. Genuine future 90/180-day
observations still require elapsed time and real captures.

Locks coordinate participating writers on the configured local dataset; direct
API clients must acquire the same public guard. Arbitrary external writers,
distributed locking and simultaneous new-dataset creation are not covered. Linux
locking and directory fsync are exercised; Windows fallback behavior is unverified
and POSIX directory fsync is unavailable there. Legacy CLI commands that initialize
schema may require writer ownership even when their calculation is analytical.

Dataset REST capture reserves 256 KiB of admission headroom before network work.
Limits remain admission controls, not filesystem quotas or hard HTTP ceilings.
Partial transport totals describe the observed subset. Abrupt termination before
accounting finalization can lose in-memory measurements; missing work stays
unknown. Unexpected retention failures surface separately and do not replace the
original capture outcome. Temporary files orphaned by a kill remain counted in
disk usage and are not automatically deleted or treated as published evidence.
