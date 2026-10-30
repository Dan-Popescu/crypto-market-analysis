# Cross-asset delivery and empirical acceptance

Version 0.10.0 implements the experimental instrument path described in
[CROSS_ASSET_RUNTIME.md](CROSS_ASSET_RUNTIME.md). The original design remains in
[CROSS_ASSET_DESIGN.md](CROSS_ASSET_DESIGN.md); implementation ownership and gates
are tracked in [CROSS_ASSET_EXECUTION.md](CROSS_ASSET_EXECUTION.md).

## Delivered software

The foundational domain lot is merged through
[PR 5](https://github.com/Dan-Popescu/crypto-market-analysis/pull/5), main revision
`d3b06cfba13d2c248e2aa57e75f912c182548f98`. Its final branch and merged main each
passed the frozen-lock/Ruff/full offline command with 1,508 tests. Sixteen warnings
come from upstream pandas/exchange-calendars timezone access.

The dependent delivery adds append-only native storage, coherent receipt-time
reads, all-vintage historical fills, source adapters, market-v1 snapshots,
one-query mixed-class fleet summaries, explicit futures helpers, frozen native
prospective workflows, native backup verification, 16 CLI commands and 11 MCP
tools. Full delivery verification and immutable tested/merged revisions are
recorded in its pull request. All 64 MCP tools remain read-only.

Independent reviews reproduced and prompted financial regressions covering:

- Simultaneous split/dividend ambiguity, split-effective fills and stale
  pre-action valuation marks.
- Publication-point bounds versus native traded intervals.
- Historical fill vintages retained despite later revisions.
- Consistent actual-receipt precedence across SQLite, snapshots and kernels.
- Split-boundary exclusion in directional price outcomes.
- One physical SQLite snapshot for grouped metadata/price/forecast reads, including
  concurrent WAL commits and portable restored DELETE-journal databases.

Current and original native evidence are preserved; no legacy crypto archive or
assessment is reinterpreted. Native-only and mixed databases can use verified
backup/restore. Local declarations of source rights, calendar knowledge or action
completeness do not become independent attestation.

## Reproducible acceptance

`tools/benchmark_cross_asset.py` generates shares, crypto spot, commodity reference
points, listed funds and individual futures. At 100/1,000 instruments it includes
80/800 listings, 100/1,000 series and 1,680/16,800 receipt-vintage bars. Native EUR
and USD selections remain separate. The benchmark verifies exact SQLite/snapshot
evidence equality and measures validation, ingestion, selective reads, export,
first verification and one-query fleet summaries. Output artifacts stay outside
version control. No source HTTP request is made.

The workload is a small daily/point research slice with two vintages, not years
of full intraday mixed-market history. Instrument counts multiply by listings,
providers, currencies, expiries, frequencies and revisions. Explicit query/export
budgets and separate bounded selections remain necessary. These results cannot
be extrapolated to a distributed service, continuous live cadence or unrestricted
multiwriter workload. The measured SQLite/Parquet/DuckDB roles are retained;
PostgreSQL remains a workload-driven future decision.

## Remaining empirical gates

1. Verify current provider endpoint access, source semantics, permissions and
   sustainable request limits. The implementation environment excludes the new
   provider hosts and its web research tool was unavailable. No equity feed or
   FRED deployment is claimed live-qualified.
2. Capture and inspect actual class-specific data, corporate-action completeness,
   instrument identities and sourced schedule exceptions. A normalized imported
   equity fixture demonstrates software, not licensed price coverage.
3. Observe real elapsed operating periods and eligible future outcome clocks.
   Synthetic receipt tests cannot prove months of provider quality or a financial
   edge. Frozen declarations remain locally asserted rather than externally
   attested preregistration.
4. Qualify broader frequencies, instruments and advanced models before enabling
   them. Automatic dividend reinvestment, physical futures delivery/expiry
   settlement, full daily-margin survival and adjusted continuous curves are
   explicitly unsupported. The delivered fixed-margin mark and unadjusted curve
   kernels expose their narrower assumptions.

These gates require new external evidence or additional model scope. No permanent
worker, public deployment, paid API or real order has been activated.
