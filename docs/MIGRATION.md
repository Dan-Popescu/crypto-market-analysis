# English repository and output migration

The implementation brief adopted on October 28, 2026 makes English canonical
for first-party repository content and software output. This supersedes earlier
French-output requirements. It applies to documentation, examples, code names,
comments, prompts, CLI and MCP text, reports, charts, public schema values, new
commit messages and pull request text. Existing Git history is preserved.

Raw external evidence, provider identifiers, instrument symbols, official names
and source URLs retain their original content. An English explanation must keep
externally supplied text distinguishable from application-authored prose.

## Public contract inventory

| Surface | Migration contract |
| --- | --- |
| CLI command and Python package | Keep `crypto-market-analysis` and `crypto_market_analysis`. |
| Configuration and data locations | Keep `OBSERVER_*`, `observer.toml` and `observer.sqlite`; no automatic configuration or database rename. |
| MCP authentication | Keep the `observer:read` scope and existing OAuth setting names. |
| Existing CLI/MCP operations | Keep technical operation names; translate authored help, descriptions, errors and responses. |
| Analysis bundle | Version the canonical English-output bundle as schema `2.0`; consumers must check the schema version. |
| `daily_trend` and `weekly_trend.direction` | Canonical output values are `up`, `down` and `flat`, replacing `hausse`, `baisse` and `stable`, respectively. |
| Forecast event keys | Keep `red`, `up`, `flat`, `down_barrier` and `up_barrier`. `red` names an endpoint comparison, not a calendar-candle color. |
| Legacy unknown-unit value | Accept `inconnu` only as a legacy input alias; authored English output uses `unknown`. |
| Synthetic JSON examples | Translate authored strings deterministically while preserving keys, numbers, timestamps and synthetic provenance. |
| Validation evidence | Preserve the original author's reported results in `VALIDATION.md` separately from independently executed import checks in `IMPORT.md`. Translation is not a new test run. |

This document defines the coordinated migration contract. Runtime conformance
must be verified by the implementation checks; documentation translation alone
does not validate the application. The migration introduces no paid data source,
public deployment, account connection or new analytical feature.

## Existing local data and clients

SQLite market tables remain in place. Schema version 2 recomputes only observation
identity dimensions, preserving payloads, first-seen times and revisions. Contract
size, settlement, market type and funding conventions remain distinct. Equivalent
explicit funding-period aliases normalize to seconds; conflicts remain unknown.
New calendar/import and journal tables are additive. Use SQLite's backup API for
a consistent backup before updating an existing database.

Issued evidence uses a lossless `zlib+base64` envelope with a SHA256 checksum and
a 32 MiB uncompressed limit. `restore_evidence` also accepts early raw envelopes.
Plan inputs reuse captured candles and retain additional versions only once.
Later plan outcomes pin original evidence and use first-observed fully
post-issuance candles; a straddling hour remains excluded. No original assessment
is rewritten. MCP returns evidence counts/hashes rather than the archive body.

Keep local databases and settings in place. Consumers that previously compared
French direction values must update those comparisons for schema `2.0`.
Retain the distinction between legacy input compatibility and canonical output:
accepting an older value does not make it a supported new output value.
Missing observations remain missing, and `live`, `import` and `demo` data remain
separate. Do not rewrite external evidence or personal runtime records merely
to translate their language.

## Accumulation configuration and result schemas

The accumulated-feature results and plan configuration use their own integer
`schema_version: 1`; this is separate from the existing bundle's string `2.0`.
Do not assume a version number applies across every operation. Preserve decimal
strings in plan inputs/outputs and integer UTC epoch seconds in public time
arguments. CLI `--as-of` additionally accepts timezone-aware ISO dates.

Existing TOML files remain usable. New optional keys are:

| Key | Default and behavior |
| --- | --- |
| `quote_currencies` | `["EUR", "USD", "USDT", "USDC"]`; requests distinct native markets, without conversion. |
| `purchase_jurisdiction` | `"France"`; other jurisdictions are currently rejected. |
| `purchase_candidates` | `["kraken", "coinbase", "bitvavo"]`; separates purchase candidates from global analytical venues. Entity/service evidence must still be checked. |
| `liquidity_venues` | Omitted means all configured analytical venues. An explicit subset bounds book requests only; an empty list disables those requests. |

The registry now includes Bitvavo alongside the previous 14 venues. A stored
explicit `venues` list stays explicit: add `bitvavo` to opt that configuration
in. The example keeps all 15 analytical venues and samples liquidity from the
three purchase candidates to bound request volume. Settings reject unknown
keys; do not add undocumented policy fields to TOML. Purchase policies belong
in a separate schema-1 JSON plan file.

No local database rename or destructive migration is required. Additional
journal/calendar/local-record tables are initialized by the operations that
need them. Preserve existing observation versions, modes and provenance.
New `issue`, `observe`, imports, collection and explicit vigilance `--state-key`
operations write locally. Existing and new MCP lookup/calculation tools remain
read-only; there is no MCP issuance or outcome-append operation.
