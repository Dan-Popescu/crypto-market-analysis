# Accumulation implementation status

Delivery status as of October 28, 2026. This table records implemented code and
its verification boundary; it is not a statement of investment usefulness or
successful public-source collection. Original author-reported V1 validation and
independent prototype import checks remain in [VALIDATION.md](VALIDATION.md) and
[IMPORT.md](IMPORT.md), respectively.

| Phase | Implemented code | Verification and remaining boundary |
| --- | --- | --- |
| 0: English migration | First-party documentation/output, English direction values and compatibility inventory. | Integrated final-suite result pending below; historical evidence and raw provider text retain provenance. |
| 1: Historical context | Explicit native quotes, complete UTC periods, lookback coverage through 730 days, source catalogue, Bitvavo and bounded local hourly archives. | Offline parsing/context checks are part of integration verification. New live endpoint availability and long-history completeness are blocked/unverified in this cloud. |
| 2: Purchase windows | 1/3/7/14-day endpoint/touch distinction, pre-issuance exclusion, uncertainty, references, freshness and insufficient-data states. | Offline synthetic/temporal checks are included; no calibrated probability or demonstrated prospective edge. |
| 3: Purchase plans | Decimal cash accounting, regular/split/deferred families, reserve limits, deadlines, base/quote fees and common 90/180-day outcomes. | Offline accounting cases are included. Historical execution remains a configured assumption; full-book fill curves and third-asset fees are unsupported. |
| 4: Evaluation and issuance | Paired chronological outcomes, baseline underperformance, unitized drawdown and append-only idempotent local issuance/outcome records. | Offline chronology/journal checks are included. Synthetic or retrospective results do not establish prospective usefulness; strict unavailable origins remain excluded. |
| 5: Context sources | Existing context collectors, unit-aware interpretation, bounded BLS/FOMC parsers, explicit local imports and one-shot calendar collection. | Offline provider/calendar cases are included. New live collection was blocked by the proxy allowlist; complete automated ETF flow and historical intraday macro-vintage coverage remain unavailable. |
| 6: Exceptional vigilance | Opt-in 4/8-hour observational assessment, degraded-input flags, deduplication, cooldown, recovery, expiry and local alert output contract. | Offline synthetic transitions are included. No full liquidation map, numeric crash probability, permanent job or external messaging activation. |
| Shared interfaces | CLI, read-only MCP, deterministic Markdown, charts and synthetic configuration examples use the shared engine. | Final integrated CLI/MCP smoke evidence and suite/build result must be recorded below. |

## Final integrated verification record

- Exact tested revision: **pending final integration verification**.
- `uv sync --frozen --extra dev`: **pending final integration record**.
- `uv run --frozen python tools/check.py`: **pending; final test count not yet recorded**.
- `uv build --wheel --offline`: **pending final integration record**.
- Reproducible CLI/local-MCP commands: [ACCUMULATION.md](ACCUMULATION.md) and [MCP_CLIENTS.md](MCP_CLIENTS.md); final execution results **pending**.
- Public-source checks: blocked by cloud proxy allowlist; **no successful new live collection claimed**.
- Hosted Actions, public deployment, account connections and permanent scheduled jobs: not performed.

Replace pending entries with the exact final revision, command results and any
unavailable checks after integration. Do not copy the original 71-test result
into this record as evidence for the new implementation.

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
