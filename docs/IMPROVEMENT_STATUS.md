# Version 0.3.0 implementation and verification

Recorded October 28, 2026 (Europe/Paris). Implemented from the selected
[improvement review](IMPROVEMENT_REVIEW.md), using independent bounded worktrees
and shared-interface integration. The [workflow guide](IMPROVEMENTS.md) and
[migration contract](MIGRATION.md) describe the public operations.

## Delivery boundary

| Lot | Delivered | Verification and remaining boundary |
| --- | --- | --- |
| C0 / F1 reports | Evaluation metrics, qualified/scenario counts, exclusions, protocol, waiting/funding exceptions, contiguous tables and nested assessment/outcome reports; distinct example policies. | Public render regressions and actual CLI workflows pass. Exact JSON/Decimal values remain unchanged by rendering. |
| C0 / F2 waiting and qualification | All funded waiting intents clipped at each terminal; quote-days, target/execution components and amount-weighted waiting; separate terminal valuation and execution evidence. | Hand-calculated cases pass. A EUR 100 seven-day reserve fallback reports 604,800 seconds and 700 quote-days. Three sparse valuation candles no longer count as two qualified performance samples. |
| C0 / F3 retention | Finalized liquidity medians/counts survive late arrivals; late raw versions are quarantined and coverage becomes explicitly partial. | Late 1/3/100 case, known-at versions, interrupted deletion and concurrent compaction checks pass. No invented combined median. |
| C0 / F4 capture | Requested market filters before its cap; candle, bundle and vigilance profiles; bounded native-series histories, family coverage, evidence sizing and actual availability. | Behavioral checks and a production-cap-sized synthetic SQLite experiment pass. Hard evidence limits remain. Broad history omissions are explicit. |
| C0 / F5 funding | Vigilance shares canonical seconds/hours/text interval interpretation. | Aliases agree; conflicting periods remain unknown. No new derivatives feed is claimed. |
| P1 data route | Isolated named Kraken import dataset, bounded ordered split-ZIP member staging, hashes, append-as-import REST captures, overlap revisions, crash recovery, bounded four-market manifests and manual resumable gap repair. | Synthetic split archives, injected responses and actual offline CLI operations pass. **Real archive/API access, intended use and four-market report acceptance remain blocked/unverified in this cloud.** No large download or source request was performed. |
| P2 next tranche | One pinned snapshot brief, current observations versus hypothetical policy and historical outcomes, UTC recurrence constructor and price-free funding preview. | Four BTC/ETH × EUR/USD synthetic workflows pass. Cap usage distinguishes dip targets from all waiting cash. No policy recommendation or actual account balance is inferred. |
| P3 costs | At most three named caller cost cases, identical policies/cashflows/inputs, paired terminal differences and qualified ranking reversals. | A fixed-fee hand case changes split advantage from +5 to -1.3 native quote units. This is a synthetic arithmetic check, not investment performance. |
| P4 early protocol | Separately named observable hourly endpoint, frozen new-issue convention, first-observed future prices, bounded journal cohorts and honest missing/excluded/overlap counts. | New/legacy protocols, future-plan issuance, repeated appends and actual read-only MCP checks pass. Full probability calibration and prospective performance aggregation remain deferred. |
| P5 and lower-priority additions | Deferred as selected. | Live amount-specific venue comparison, cross-venue disagreement, extra context, backup/replay product features and richer scorecards are not claimed implemented. |

The fifteen-venue global analytical registry remains; purchase eligibility is
still France/entity/service-specific. EUR/USD remain native separate analyses;
USDT/USDC are not renamed or treated as fiat. Ledgerly remains authoritative for
actual transactions/holdings. No paid API dependency or account key was added.

## Final code verification

Exact tested code revision: `c691cb30e74b547f9753ac8fef1b8244cef45e78`.
The subsequent verification/documentation commit changes no application code.

```bash
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/improvements/dist
```

- Offline lock coherent: 73 resolved packages. Dependency versions are unchanged;
  only the local editable project version moved to 0.3.0.
- Ruff clean; **357 tests passed in 39.90 seconds**. Permission for local network
  sockets enabled the actual local STDIO/HTTP MCP tests; no public API collection
  is inferred from that permission or those tests.
- Wheel built: `crypto_market_analysis-0.3.0-py3-none-any.whl`. ZIP integrity,
  metadata version and all five new calculation/data modules were checked.
- Actual local STDIO exercised all five new MCP tools. They retain read-only
  annotations and created no journal table during calculation-only tests.
- A 25-command CLI smoke exercised four native synthetic contexts/briefs through
  730 days, complete 90-day versus pending 180-day outcomes, constructor, funding,
  three cost cases, issuance/retry, appended observation, assessment rendering and
  bounded excluded demonstration cohorts. The final rerun uses its own revision
  directory; raw databases/reports are outside Git.
- The documented workflow, offline dataset initialization/status/repair planning
  and staging argument parsing were exercised with temporary files. Real refresh/repair network
  calls and official large archive downloads were not performed.

## Capture volume experiment

A separate synthetic SQLite experiment for the capture implementation used
517,521 candles and 120,961 observations, including 500,001 unrelated market rows,
17,520 selected hourly candles and 120,960 book metrics. The selected history
survived, an older macro point remained visible with its real age, and candle-only
issuance stored a 4,466,099-byte uncompressed evidence envelope, below 32 MiB.
This verifies storage/capture behavior, not actual venue coverage or 730-day
return completeness (730 daily returns need 731 daily closes).

Observed component timings on this Linux workspace with five logical CPUs:
candle capture 0.406 s, issuance 5.804 s, archive restoration 0.334 s and broad
capture 4.141 s; peak process RSS 146,768 KiB. Timings are illustrative, not service
targets. This component experiment preceded final integration; the final suite
and CLI/MCP checks above are the release-code checks. Synthetic data and raw
experiment artifacts remain outside Git.

## Standards

Independent frozen review compared `9c1454c49291481ec36752514bfb7e2e5f82aeb2` to
`b1484d7f3d0b0e5eef13eb132cae3b8989c1dc5d`. One P2 finding: an outcome reference
retained the original training cutoff/availability after consuming new prices;
plan replay references also omitted availability and lineage. Fix `96af21c`
records actual outcome clocks and keeps `original_input_reference` separately.
The reviewer verified it at `c691cb3`, including six focused passing checks. No
remaining documented violation or additional actionable heuristic was reported.

## Spec

The same frozen range received an independent requirements review. One P2
finding: waiting immediate cash incorrectly consumed the dip-target reserve cap
in next-tranche previews. Fix `c691cb3` separates dip-target usage and all committed
cash; brief historical qualification also reuses the shared simulator contract.
The original reproduction now shows 50 deferred, zero overflow, 200 committed
and zero free cash. The reviewer verified 27 focused checks and reported no
remaining focused finding. Source qualification stayed explicitly unverified.

Counts by review axis: Standards 1 finding, fixed; Spec 1 finding, fixed. Neither
review nor passing software checks establishes a financial advantage.

## Remaining operational and analytical limits

Public source hosts are outside the managed cloud's enforced network policy;
document access is not runtime API access. Use a normally authorized reachable
environment for actual source qualification. A paid subscription is unnecessary
for core local calculations, but bandwidth/storage/hosting and source-use terms
remain separate. No user-specific data licence or fee tier was established.

The archive route stages already obtained official local parts; it does not
automatically download gigabytes. Manual repair respects Kraken's recent-hour
retention; older gaps require appropriate archives and remain explicitly limited.
Named datasets preserve real ingestion/materialization time and never manufacture
past `known_at` origins. A genuine future issue after import availability can be
prospective in its own lineage cohort.

Costs/fills remain configured assumptions. No complete historical order books,
complete liquidation map, automated comprehensive ETF feed, calibrated crash
probability or improvement over regular purchases is established. The journal
overview exposes denominators but does not replace a prospective performance
study. No permanent collector, order, fund transfer, notification, account
connection, public deployment, push or pull request was activated by this work.
