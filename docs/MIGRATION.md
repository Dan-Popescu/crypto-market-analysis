# English repository and output migration

Version 0.6.0 deliberately makes `maintain` a dry run unless `--apply` is supplied.
Local refresh is incremental; explicit `backfill_days` requests the old refetch
behavior. MCP dictionary results are bounded projections with `_mcp_result` and
read-only `get_result_page` access to full frozen details. Consumers must inspect
omissions and expiry; history `points` remains a list and its cursor advances from
the last emitted row. Native tool names and calculation rules stay compatible.
See [EFFICIENCY.md](EFFICIENCY.md) for conservation defaults and operator commands.

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
New plan issuances capture those versions in the same SQLite read transaction as
the latest candles. A correction inserted after capture cannot change only the
issued plan. The original archive layout remains readable; the plan's own input
reference hashes all consumed vintages and includes the captured watermark.
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

## Version 0.4.0 offline additions

For the subsequent 0.5.0 connection layer, see [DATA_CONNECTIONS.md](DATA_CONNECTIONS.md).
Its retained book table is created only by explicit retention/capture. New
`fx_reference` and `stablecoin_price` local record kinds are additive; store schema
version 2 and existing backup/restore behavior remain compatible. Existing
collection defaults do not retain levels, and existing pure-request tools remain
supported. Official network callers now stamp each calendar response after its
receipt; injected callers may still supply an explicit frozen clock.

The [offline workflow guide](OFFLINE_WORKFLOWS.md) documents new explicit CLI
commands and seven read-only MCP tools. Existing commands, configuration, journal
archives, plan rule `accumulation-v2` and mode/native-quote identities remain
compatible. No third-party dependency changed and no destructive migration runs.

Backup/restore operates on explicit paths through the CLI only. Restoration
requires a new or empty directory. Named-dataset backup requires stopped writers
and `--quiescent-dataset`; directory rebasing retains original manifest provenance.
Original replay refuses unsupported frozen rules instead of silently updating
results. The prospective scorecard consumes bounded journal projections and
validates recorded replay/qualification; it does not replace archive replay or
establish calibration against incompatible forecast clocks.

Request-only calculations use their request's explicit data mode. Venue estimates
require actual supplied ask levels and explicit fee/product/rule assumptions;
stored aggregate depth is insufficient. The new `ecb-manual` import is a sourced
manual JSON schema, not an automatic official feed. FX transformations are
informational and explicit; no core price series is converted or renamed.

## Version 0.3.0 follow-up contracts

The follow-up implements the selected priorities in [IMPROVEMENT_REVIEW.md](IMPROVEMENT_REVIEW.md);
[IMPROVEMENTS.md](IMPROVEMENTS.md) contains executable examples. Existing commands,
TOML, native quotes and the integer schema-1 plan configuration remain compatible.
Result consumers must also inspect `rule_version`, rather than the schema alone.

The architecture follow-up preserves plan configuration, `accumulation-v2`, CLI
commands and MCP tools. Purchase-plan funding previews remain importable from
`plan_builder`; accounting is owned by `plans`. Exact-time brief funding now
includes target reserve released before a new tranche at that timestamp. Research
forecast horizons add `reference_frequencies` and `reference_confidence_intervals`
for descriptive samples below the unchanged conditional support gate. Legacy
`base_rates` remain gated alongside conditional probabilities. Journal overview
results retain their existing shape; journal reads no longer initialize schema.

- `accumulation-v2` changes waiting statistics to include funded pending, expired
  and unfilled intents, clipped at the reported cutoff. Released free cash stops
  target waiting. Amount-weighted quote-days do not depend on splitting an intent.
- Plan terminal `complete` still describes valuation availability. New
  `execution_coverage` and per-origin `evidence_qualification` separately determine
  eligibility for performance aggregates. `sample_size` now counts qualified
  origins; `scenario_sample_size` retains complete valuation scenarios. Missing
  observations never manufacture fills.
- Old issued records and outcomes remain immutable. A saved plan with an
  unavailable earlier rule version remains readable; observing that plan reports
  `unsupported` rather than silently recomputing it under v2. New future-starting
  plans can be frozen as `not_started`, with pending outcomes.
- New issues freeze `outcome_protocol` with `hourly_after_deadline_v1`. It adds a
  separately named observable hourly mark, timestamp and offset. The exact
  endpoint remains unknown when unobservable. Legacy issues without this protocol
  retain their exact convention. New protocol outcomes use first-observed price
  versions; this does not establish comparability with anchor-relative forecasts.
- Input references add actual consumed-evidence `available_at` and optional
  `dataset_lineage`. Legacy projected records without availability evidence remain
  excluded from prospective cohorts. Import provenance alone does not disqualify
  a genuinely future issuance; no historical availability is backdated.
- Capture profiles bound evidence by operation. Candle-only context/windows,
  briefs and issuance omit unused book history; broad bundles retain bounded
  native series with explicit coverage/omissions. Vigilance retains the raw
  baseline its policy requires. The 32 MiB issuance limit remains unchanged.
- Late raw liquidity for a finalized hour is preserved as `late_raw_unmerged`.
  The earlier median/count remains, with a later `partial_unmerged` aggregate
  version and incomplete-coverage flag. Quarantined observations cannot enter a
  raw vigilance baseline. A small local retention-lock SQLite sidecar serializes
  compactors and contains no portfolio or assessment evidence.

The optional named research dataset uses its own directory, `dataset.json` and
`observer.sqlite`. Its additive audit/repair tables and payload files preserve
archive parts/member hashes, real capture/materialization time, conflicts and
resumable missing ranges. Normal `--mode import --data-dir DATASET_DIRECTORY`
analysis reads it. It is never silently unioned with ordinary live data. Archive
staging and repairs are explicit CLI actions; no permanent worker is activated.

The five new MCP calculations/reads are `get_accumulation_brief`,
`construct_purchase_plan`, `preview_purchase_funding`, `compare_cost_sensitivity`
and `get_journal_summary`. They accept bounded objects, not arbitrary file paths,
and create no assessments, orders, datasets or notifications. Journal summaries
report their record/outcome bounds and count each issued event once; they are not
a calibrated scorecard or evidence of an investment advantage.
