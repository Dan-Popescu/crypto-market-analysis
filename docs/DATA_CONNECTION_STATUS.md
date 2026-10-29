# Version 0.5.0 data connections: delivery and evidence

All five projects in [DATA_CONNECTION_PLAN.md](DATA_CONNECTION_PLAN.md) are
implemented. [DATA_CONNECTIONS.md](DATA_CONNECTIONS.md) gives local commands.
This completes the selected software connection work, not real-source acceptance
or prospective performance observation.

## Delivery

| Project | Delivered |
| --- | --- |
| Retained books | Opt-in bounded level capture, native identity, exact supplied exchange timestamp, actual receipt/provenance/hash, 500 retained rows per side with truthful truncation, and liquidity metrics from the same fetch. Bitvavo 1,000-row and Binance 5,000-row requests retain bounded evidence without losing full-response metrics. Explicit fee/product/rule assumptions feed costs; unknown clocks stay unknown. Checked backup/restore preserves books. |
| Stored calculations | Public bounded coherent Store snapshots and read-only quote, derivative and venue bridges. Derivatives preserve all necessary known-at versions over 48 hours rather than latest-only samples. Results expose exact consumed references/hashes, physical watermark and omissions. Each analysis has its own snapshot. Date-only FX remains visible but cannot establish a precise basis endpoint. |
| Official/free acquisition | Bounded ECB daily XML, opt-in official calendar parsing, and public native stablecoin spot tickers with source errors, hash scopes and receipt clocks. Calendar parsing used inspected official rendered entries; raw markup fixtures are synthetic and explicitly unqualified. Network callers sample each calendar clock after its response. Dates without time do not acquire invented timestamps. |
| Source qualification | Independent BTC/EUR, ETH/EUR, BTC/USD and ETH/USD coverage, exact gaps, revisions, actual freshness, bounded references and resource failures. Documentation, reuse rights, purchase eligibility and observed access remain separate. Generic collection success cannot certify a missing pair or historical availability. |
| Local workflow and adapters | Explicit optional refresh, quality gates, stored views and native briefs, then optional keyed issuance. Default operation reads only. Live refresh forbids historical clocks. Missing/stale history suppresses issuance. Explicit complete/fresh demo issuance remains synthetic and excluded prospectively. Eight new CLI commands and five read-only MCP tools; no collection or workflow-write MCP tool. |

The workflow reuses the existing pipeline and public clients. Limits count
declared logical exchange operations/pages; official context has its own budget.
Wire HTTP fan-out is unknown. Query/storage bounds qualify the database snapshot
and are not filesystem quotas. No permanent worker or deployment was activated.
Third-party dependency versions did not change; the package version is 0.5.0.

## Final verification

Exact tested and independently reviewed code:
`c51edf879a10a6aadcd0add8ff11b080f09b66d7`.
Baseline: `691c1296cbcb2f280c9646a74b08dee5b0f857ef`.
The subsequent documentation commit changes no application code.

```bash
UV_CACHE_DIR=/workspace/.cache/uv uv sync --frozen --extra dev --offline
UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/improvements/mpl-cache uv run --frozen python tools/check.py
UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/connections/dist
```

- Offline lock coherent: 73 packages; Ruff clean; **846 tests passed in
  58.47 seconds**. Actual local CLI and STDIO transports exercised all six new
  read/workflow commands in JSON/Markdown and all five read-only tools. Capture
  boundary tests use injected public clients and real SQLite.
- The existing 25-command actual CLI smoke passed on the final revision,
  covering four independent synthetic BTC/ETH × EUR/USD contexts and briefs,
  funding, sensitivity, immutable issue/retry, outcomes and journal reports.
  Local artifacts: `/workspace/work/improvements/verification/cli-c51edf87`.
- Actual named-dataset subprocess checks cover request-selected import mode,
  inner workflow lineage, unchanged rows/identity, no network and no journal
  creation. Book tests cover concurrent snapshots and checked recovery.
- Wheel ZIP integrity and every packaged Python source matched the tested tree.
  Artifact: `/workspace/work/connections/dist/crypto_market_analysis-0.5.0-py3-none-any.whl`.
  SHA256: `ddd437a1d29bb4502ab4937cd5144d5fa36bded8683974d27870110fdda7df76`.
- Static runtime relative-import inspection found 45 modules, 152 edges and no
  cycle, excluding type-checking-only imports. The workflow orchestrates Engine
  one way rather than being called back through its facade. This is not a proof
  of zero future debt or all dynamic dependencies.

## Independent reviews

### Standards

No remaining documented violations or material maintainability findings. Two
correctness findings were fixed and independently re-probed: provider request
limits above the retained-row bound, and calendar availability stamped before
receipt. Public regressions preserve full metrics and source-specific clocks.

### Spec

No remaining actionable findings. The receipt-clock defect was independently
reproduced and verified fixed. The synthetic issuance exception was clarified as
intentional: explicit fresh/complete demo records remain excluded from prospective
evidence. A public regression verifies that distinction. Live/import quality
gates and missing/stale suppression remain unchanged.

## Real-source qualification remains separate

A bounded actual CLI FX probe at `b32e161` ran through the supported session proxy.
It returned exit code 1, `status: error`, `ProxyError`, and zero inserted rows.
The result is stored locally at
`/workspace/work/connections/verification/b32e161/official-fx-probe.json`.
It establishes an environment acquisition failure, not provider-wide denial.
No fallback observation or successful collection was claimed.

The enforced cloud policy excludes market and ECB hosts. Authorized document
inspection established the ECB calendar's rendered date/title entries and EUR
reference-rate meaning; it did not qualify raw HTML/XML runtime transport.
Relevant primary sources:

- [ECB calendar](https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html)
- [ECB reference rates](https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html)
- [Kraken public ticker](https://docs.kraken.com/api-reference/market-data/get-ticker-information)

The commands can run without deployment in an ordinarily authorized reachable
environment. Real four-market history, live book/fee evidence, raw official format
acceptance and freshness still require actual successful captures. No paid API,
account key, order, transfer, notification, public hosting, push or PR was added.
True prospective 90/180-day results still require subsequent elapsed time.
