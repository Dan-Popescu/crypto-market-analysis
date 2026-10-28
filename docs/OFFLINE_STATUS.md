# Version 0.4.0 offline completion and verification

This delivery follows [the offline completion plan](OFFLINE_COMPLETION_PLAN.md).
The preceding four architecture projects were complete, but the broader offline
backlog was not. This record distinguishes the additional implementation from
external data qualification and genuine future performance.

## Delivered

| Work | Implemented scope |
| --- | --- |
| Backup and restoration | Consistent online SQLite backup; checked schema, integrity, hashes, journal originals/outcomes and actual code/lock provenance; atomic restoration into a new or empty directory. Named datasets retain required raw/normalized payloads, pending recovery and rebased metadata with original provenance. Their writers must be stopped and explicitly declared quiescent. Defaults bound dataset evidence to 10,000 files and 512 MiB. |
| Original replay | Checked, storage-independent context/window/plan recomputation from frozen original evidence at original clocks. Supported future-plan snapshots remain not-started. Unknown rules fail explicitly; future outcomes are not invented or appended. |
| Prospective scorecards | Bounded event frequencies/uncertainty and paired 90/180-day plan wealth including reserve, acquisition/waiting/drawdown metrics and regular-plan differences. Fixed time-only overlap selection, first eligible recorded outcomes, execution qualification and native temporal/lineage/policy cohorts keep scenarios, missingness and eligible samples separate. Incompatible forecast probabilities are not scored. |
| Shared BTC/ETH budget | One explicit dated cash envelope funds declared plan families in stable order. Embedded plan contributions are not counted twice. Fees, caps and deadline releases reuse plan accounting, with no borrowing, assumed fills, FX conversion or actual portfolio ledger. |
| Venue costs and disagreement | Hypothetical amount-specific spot costs from explicit ask levels and fee/product/rule assumptions, plus aligned same-native-quote dispersion. Unknown assumptions, stale sources, missing depth and timing skew remain visible. No live book acquisition, unconditional ranking or measured fill is claimed. |
| EUR/stablecoin context | Explicit indicative FX orientation and stablecoin basis with original native observations, known availability, freshness and transformation provenance. Derived values are suppressed when necessary inputs are missing or skewed. |
| Manual ECB calendar | Sourced manual JSON schedule import, with date-only versus timed entries, European DST, revisions, cancellations and unknown publication. A later receipt of an older revision cannot undo a known cancellation. This is not an automatic official feed. |
| Native derivatives context | Separate native funding/OI 4h/24h endpoint comparisons, preserving actual/projected rates, funding periods, contract sizes, units, settlement, clocks and missingness. No pooled OI or annualized return is inferred. |
| CLI/MCP and reports | Ten new CLI commands plus the explicit `ecb-manual` import kind; seven bounded read-only MCP tools. Recovery filesystem writes stay CLI-only. English Markdown reports and synthetic examples expose assumptions and exclusions. |

[Offline workflows](OFFLINE_WORKFLOWS.md) gives executable examples.
Existing configuration, modes, native quotes and `accumulation-v2` remain
compatible. The package version is 0.4.0; third-party dependencies are unchanged.

## Verified code

Exact final tested code and focused-review revision:
`9ad0a00788fa0966a2d5aed90b1bc7723214f50f`.
Review baseline: `2f3d621e9424bbb067071ba86f72f494a13f01f1`.
The subsequent documentation commit changes no application code.

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/offline/dist
```

- Offline lock coherent: 73 packages; Ruff clean; **699 tests passed in
  47.91 seconds**. Actual local MCP transport checks exercised all seven new tools;
  local socket permission is not public-source acceptance.
- A fresh 19-command actual CLI smoke exercised all five request calculations
  in JSON/Markdown, manual calendar import, scorecard, frozen future-plan replay,
  backup/verification/restoration and replay from the restored journal. Originals
  stayed unchanged and no outcome was appended. Local artifacts:
  `/workspace/work/offline/verification/cli-9ad0a007`.
- The existing 25-command CLI smoke also passed on that final revision, covering
  independent synthetic BTC/ETH × EUR/USD contexts/briefs through 730 days,
  constructor, sensitivity, funding, immutable issuance/retry, appended outcomes
  and journal reports. Local artifacts:
  `/workspace/work/improvements/verification/cli-9ad0a007`.
- Differential evidence preserved 768 complete simulation outputs, 384 funding
  previews, 48 existing forecast projections, 48 complete purchase-window outputs
  and three evaluation outputs against the immutable earlier baseline. The shared
  budget refactor also matched 720 independently varied full previews.
- A Fraction-based exhaustive lot enumeration matched 1,000 generated venue-cost
  cases across fees, rounding and multiple ask levels.
- Static relative-import inspection found 40 modules, 122 edges and no cycle.
  It does not establish the absence of dynamic dependencies or all future debt.
- The 0.4.0 wheel passed ZIP/metadata integrity checks and every packaged Python
  module matched the tested source. SHA256:
  `be20fa4903a992a44df146324ba0490fc85da09d58dce3cc6875cea9729f9416`.

## Standards

Independent review: **0 findings**, including the final focused correction.
The not-started plan snapshot has one owner in `plans`; replay imports no private
application helper.

## Spec

Independent review initially found **1 P2 defect**, fixed before final checks:
an older manual ECB revision received later could resurrect a cancelled event.
Both calendar read paths now share the highest-known-revision rule. Public tests
cover earlier cutoffs, stale imports, cancellation retention and later legitimate
revisions. The reviewer independently reproduced and verified the fix; **0
remaining findings**.

## Still dependent on real inputs or time

- Native BTC/ETH EUR/USD archive/refresh qualification and measured gaps remain
  unverified. Public source hosts are outside this managed cloud's network policy;
  a normally authorized reachable environment can run qualification without
  deployment. Core calculations need no paid API subscription.
- Venue calculations need real full ask snapshots and actual product/fee/rule
  evidence. The supplied examples are synthetic, and global analytical usefulness
  does not establish France entity/service eligibility. No automatic live-book
  acquisition or official ECB feed integration is included in this offline lot.
- The ECB route requires verified transcription. FX, stablecoin and derivatives
  context require actual provenance-bearing observations; the software does not
  attest a caller's assertions.
- True prospective 90/180-day performance needs genuine subsequent elapsed time
  and captures. Passing tests establishes neither calibration nor investment edge.

No permanent collector, account connection, external notification, order, transfer,
deployment, push or pull request was activated. Previously rejected model/score,
complete historical book/liquidation and comprehensive ETF work stays outside
this selected scope; it is not silently included as unfinished implementation.
