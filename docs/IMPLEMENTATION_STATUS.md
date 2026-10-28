# Accumulation implementation status

Delivery status as of October 28, 2026. This table records implemented code and
its verification boundary; it is not a statement of investment usefulness or
successful public-source collection. Original author-reported V1 validation and
independent prototype import checks remain in [VALIDATION.md](VALIDATION.md) and
[IMPORT.md](IMPORT.md), respectively.

| Phase | Implemented code | Verification and remaining boundary |
| --- | --- | --- |
| 0: English migration | First-party documentation/output, English direction values and compatibility inventory. | Final integrated suite passed (244 tests); historical evidence and raw provider text retain provenance. |
| 1: Historical context | Explicit native quotes, complete UTC periods, lookback coverage through 730 days, source catalogue, Bitvavo and bounded local hourly archives. | Offline parsing/context checks are part of integration verification. New live endpoint availability and long-history completeness are blocked/unverified in this cloud. |
| 2: Purchase windows | 1/3/7/14-day endpoint/touch distinction, pre-issuance exclusion, uncertainty, references, freshness and insufficient-data states. | Offline synthetic/temporal checks are included; no calibrated probability or demonstrated prospective edge. |
| 3: Purchase plans | Decimal cash accounting, regular/split/deferred families, reserve limits, deadlines, base/quote fees and common 90/180-day outcomes. | Offline accounting cases are included. Historical execution remains a configured assumption; full-book fill curves and third-asset fees are unsupported. |
| 4: Evaluation and issuance | Paired chronological outcomes, baseline underperformance, unitized drawdown and append-only idempotent local issuance/outcome records. | Offline chronology/journal checks are included. Synthetic or retrospective results do not establish prospective usefulness; strict unavailable origins remain excluded. |
| 5: Context sources | Existing context collectors, unit-aware interpretation, bounded BLS/FOMC parsers, explicit local imports and one-shot calendar collection. | Offline provider/calendar cases are included. New live collection was blocked by the proxy allowlist; complete automated ETF flow and historical intraday macro-vintage coverage remain unavailable. |
| 6: Exceptional vigilance | Opt-in 4/8-hour observational assessment, degraded-input flags, deduplication, cooldown, recovery, expiry and local alert output contract. | Offline synthetic transitions are included. No full liquidation map, numeric crash probability, permanent job or external messaging activation. |
| Shared interfaces | CLI, read-only MCP, deterministic Markdown, charts and synthetic configuration examples use the shared engine. | CLI scenarios, eight actual STDIO MCP calls and wheel build passed; see the exact evidence below. |

## Final integrated verification record

Verified on October 28, 2026, against code revision
`4081bf39d060c41cac787dc85f9e605ede33a819` (release `0.2.0`). The following
verification documentation commit changes no application code.

- `UV_CACHE_DIR=/workspace/.cache/uv uv sync --frozen --extra dev --offline`:
  succeeded using the environment's cached locked dependencies.
- `UV_CACHE_DIR=/workspace/.cache/uv MPLCONFIGDIR=/workspace/work/implementation/mpl-cache uv run --frozen python tools/check.py`:
  lock coherent (73 resolved packages), Ruff clean, **244 tests passed in 27.72 s**.
  Actual local STDIO/HTTP protocol checks used permission for local network sockets.
- `UV_CACHE_DIR=/workspace/.cache/uv uv build --wheel --offline --out-dir /workspace/work/implementation/dist`:
  built `crypto_market_analysis-0.2.0-py3-none-any.whl`. ZIP integrity, metadata
  version and presence of all new calculation/interface modules were checked.
- CLI smoke: documented fixed 800-day synthetic workflow, with generated data and
  outputs redirected to a scratch directory. BTC/EUR, BTC/USD, ETH/EUR and ETH/USD
  each produced 730-day context; windows, all three plans, retrospective/strict
  evaluation, vigilance, three charts, issuance/retry, lookup, appended outcomes
  and source-catalogue JSON succeeded. Complete 180-day plan outcomes, empty
  strict evaluation, degraded vigilance and identical issuance retries were asserted.
  The smoke harness initially supplied an unsupported `sources --output`; its
  corrected stdout-redirection invocation passed without an application change.
- Actual MCP smoke: the SDK example in [MCP_CLIENTS.md](MCP_CLIENTS.md) succeeded
  for `get_analysis_bundle`, `get_source_catalog`, `get_market_context`,
  `get_purchase_windows`, `compare_purchase_plans`, `evaluate_purchase_plans`,
  `get_vigilance` and `get_assessment`. No account/client service was connected.
- PNG previews were inspected; chart titles, costs and input hashes were made
  readable. Numerical CLI/SDK results remain the authoritative full precision.
- Independent [Standards and Spec reviews](REVIEW.md) produced fixes for complete
  identities, event-age uncertainty, qualified comparisons, large evidence and
  observed plan outcomes. Focused regression checks passed before the final suite.
- Public-source access remains unverified: the earlier minimal Kraken/Coinbase
  probes received proxy-level 403 responses and their API hosts were absent from
  the cloud allowlist. No successful new live collection is claimed.
- Hosted Actions, public deployment, account connections and permanent scheduled
  jobs were not activated. The current Actions enabled state could not be refreshed.

Raw smoke outputs, synthetic databases and package artifacts are local scratch
material excluded from version control. Original validation/import evidence is
kept separate; the historical 71-test result was not reused as new validation.

## Remaining limitations

Software correctness, historical comparison and prospective evidence are separate.
No improvement over regular purchases has been established. Finite samples,
non-independent regimes, unknown historical publication times and future missing
outcomes limit inference. Two years of context cannot support original-method
90/180-day frequency calibration.

Plan execution uses conservative bar opens with explicit spread/slippage/fee
assumptions. It cannot infer a fill curve from aggregate depth, supports fees in
base or quote only and does not simulate orders on a venue. Reserve has zero
assumed yield. Unknown starting cost remains unknown. Live Ledgerly integration
and personal portfolio inputs are not required or provided.

Source access can differ by region; permissions and redistribution conditions
must be checked independently. Optional ETF/network/macro/liquidation context
can remain manual, missing or unavailable. FOMC meeting dates do not establish a
decision time, and scheduled events do not prove actual publication. No complete
historical liquidation map or automated comprehensive ETF feed is supplied.
