# Actionable native analysis workflows

Version 0.8.0 implements the five projects in
[ANALYSIS_WORKFLOWS_PLAN.md](ANALYSIS_WORKFLOWS_PLAN.md). It adds four finite CLI
commands and four read-only MCP tools. Public collection still needs no account
key or paid data source. No worker, deployment or order execution is activated.

## Explain what changed

Create a separate synthetic fixture for these examples:

```bash
uv run crypto-market-analysis demo --data-dir data/analysis-demo --days 750 --quotes EUR,USD --as-of 2026-10-29T00:00:00Z
uv run crypto-market-analysis analysis-changes --data-dir data/analysis-demo --request examples/analysis-changes.json --as-of 2026-10-29T00:00:00Z --format markdown
```

The request selects one to four explicit source/asset/native EUR or USD contexts
and a `previous_as_of` strictly before the current cutoff. It accepts the same
optional native venue/cost, derivative, quote and calendar selectors as a dossier.
Both reconstructions use one read-only physical SQLite snapshot, with their own
event and availability cutoffs. Late corrections cannot rewrite the earlier view.

Changes distinguish newly evented observations, same-event corrections, late
arrivals, receipt-only changes, missing evidence and clock/range/expiry changes.
Collection errors are a separate source/task panel. Price movement requires
comparable native price observations; changed hashes alone are not movements.
Requested descriptive context metrics retain before/after values and status.
No new price, fee, conversion, publication clock or purchase signal is inferred.

The report includes all selected native market summaries, exact counts and up to
100 changes by default (maximum 500). Shared limits are 200,000 candles across
the two cutoffs, 100,000 per dossier, 250,000 consumed records and 4 MiB result.
Both reads share the Store's 64 MiB returned-text, 50-million-VM-step and 15-second
session bounds. Oversized requests fail explicitly; narrow their selection.
Previously archived or deleted evidence cannot be reconstructed by this tool.

MCP: `get_analysis_changes(request, as_of)`. Use `get_request_schema` for its
nested contract and synthetic example. Omitted details remain available through
the existing hash-bound `get_result_page` operation.

## Diagnose which conclusions have support

```bash
uv run crypto-market-analysis analysis-readiness --data-dir data/analysis-demo --request examples/analysis-readiness.json --as-of 2026-10-29T00:00:00Z --format markdown
cat examples/analysis-readiness.json | uv run crypto-market-analysis analysis-readiness --data-dir data/analysis-demo --request - --as-of 2026-10-29T00:00:00Z
```

This captures the actual dossier and empirical qualification in one transaction.
Optional window estimates consume the dossier's detached candles after that
transaction closes, rather than fetching another history or keeping a SQLite
reader open during pure calculations. Individual claims retain
their actual support, coverage, freshness, reasons and required inputs. A context
return needs its required complete daily closes; having many sparse rows is not
enough. A supported short window does not imply support for longer windows.

Claim states include available, incomplete, missing, stale, unsupported,
contradictory, not requested and demo only. The overall data status includes an
explicitly requested calendar; an empty event list does not establish that no
events exist. Permission, provider access and France entity/service verification
remain separate from numerical readiness. Synthetic data cannot qualify a real
source or establish investment performance.

`next_actions` gives ordered, deduplicated guidance with command arguments,
required user inputs and `execute: false`. It does not collect or repair data.
Missing stores return a useful missing report without creating directories.
Reference source identifiers are limited to 64 characters by the reused
qualification contract. Other section selectors retain dossier bounds.

The four-market example uses 750 days, leaving room under the 32 MiB full-result
ceiling while supporting 730-day context. An 800-day four-market dossier with
all window claims can exceed that ceiling; reduce the selection or retained
history for this combined report. No evidence is silently truncated.

Optional windows use existing estimator parameters, with a shared maximum of 64
market × horizon × threshold cells. Other bounds include 64 KiB requests, 32 MiB
full results and 128 next actions, alongside the shared Store/dossier budgets.

MCP: `get_analysis_readiness(request, as_of)`. Compact replies preserve all native
selections and claim-status maps, plus a small ordered action preview. Full claim
details, calculations and remaining actions are frozen and paginated. Very broad
core summaries can exceed the MCP ceiling; narrow the requested lookbacks or
sections instead of treating missing output as an analytical result.

## Bound actual public HTTP work

Add optional settings to `observer.toml`:

```toml
http_max_requests = 10000
http_deadline_seconds = 300.0
```

Defaults apply one finite shared budget across each collection/capture invocation
and its workers, catalogue work, redirects and observable retries. Nested capture
scopes reuse the active budget. The continuous scheduler gets a new budget per
collection invocation; this is not a lifetime allowance. Requests are admitted
before sending, with actual attempts and refusals reported separately from
logical adapter operations. Known per-stage transport timeouts and owned retry
waits are capped to the remaining monotonic deadline.

Configured caps cover collection, complete local refresh/qualification, books,
official context/calendars and explicit manual/automatic named-dataset captures.
The supported public Requests and standard HTTPX transport paths preserve proxy
and TLS configuration. Custom/hidden transports disclose incomplete enforcement
and measurements rather than asserting a hard ceiling. HTTP accounting exposes
`http_limits` snapshots in retained measurements; capture reports also expose
the shared policy and consumption.

Limits are cooperative transport controls. They are not process termination or
OS bandwidth/RAM quotas, cannot forcibly interrupt arbitrary adapter code and do
not establish a strict wall-clock bound for every possible trickled stream.
Decoded body bytes, normalized payload admission and wire bytes remain distinct.

## Resume Coinbase or Bitstamp history

```bash
uv run crypto-market-analysis backfill-history --mode import --data-dir data/coinbase-history --request examples/backfill-history.json --format markdown
uv run crypto-market-analysis backfill-history --mode import --data-dir data/coinbase-history --request examples/backfill-history.json --apply
uv run crypto-market-analysis backfill-history --mode import --data-dir data/coinbase-history --status
```

Inspection never creates a store, contacts a provider, replays pending work or
changes a cursor. `--apply` opts into one finite cycle in an ordinary configured
store. Named Kraken datasets are refused; their existing archive and automatic
repair interfaces remain separate. The default example covers two native markets;
select one to four BTC/ETH EUR/USD markets in an explicit half-open hourly range.

An apply checkpoints intent before catalogue/page requests, then selects actual
oldest missing closed hours. Committed candles survive an interruption before
the final cursor update. Empty/partial/nonadvancing pages never advance over an
unknown gap. Failures, unsupported pairs and retry cooldowns remain explicit;
small budgets rotate markets. Catalogue and page failures persist the later of
local exponential cooldown and usable Retry-After/retry-at evidence. A fallback
to CCXT client headers is explicitly labelled unverified; no raw headers or
provider error text are retained, and inspection never sleeps until a retry.
A fail-fast cooperative store lock serializes
participating backfill writers, without claiming control over arbitrary external
writers. No daemon is started.

Defaults: four pages, eight logical operations, 1,200 rows, 2 MiB normalized page
admission, 32 actual HTTP attempts and 120 seconds. Maximums: 32 pages, 64 logical
operations, 32,000 rows, 32 MiB normalized admission, 256 HTTP attempts and 300
seconds over an explicitly bounded range of at most 20 years. The CLI also applies
the configured HTTP ceilings when stricter. Coinbase pages are bounded to 300
and Bitstamp to 1,000 rows; actual native support/history must be qualified.

Stored import rows use actual receipt availability, not invented historical
publication times. Page records retain the normalized CCXT row hash, selected
symbol/source and owned-versus-injected provenance; original provider bodies are
not archived by this operation. Nonempty owned captures can establish declared
native runtime access evidence through qualification. Injected, empty, foreign
market or later-known records cannot do so. Access does not approve reuse rights.

Earliest retained history remains unknown until observed. For unavailable ranges,
use appropriately sourced compatible archives if available. The existing free
Kraken archive route can establish a different reference dataset; it does not fill
Coinbase/Bitstamp gaps or merge currencies/sources. No large archive is downloaded
automatically, and no platform's complete historical availability is promised.

MCP: `get_history_backfill_status(limit)` reads bounded current mutable import jobs
only. It never resumes or captures. Nonimport configuration reports a selection
mismatch. CLI report exit 0 means a valid inspection or finite successful cycle;
errors, deferral, busy state or HTTP exhaustion during apply return 1, and invalid
input returns 2. An incomplete but successful bounded cycle can be resumed.

## Test robustness of a declared plan comparison

```bash
uv run crypto-market-analysis plan-robustness --data-dir data/analysis-demo --request examples/plan-robustness.json --as-of 2026-10-29T00:00:00Z --format markdown
```

Declare a validated plan with explicit fee currency/rate/fixed assumptions, one
to four origins, one to three named cost cases and optionally two missing-hour
cases (maximum 168 aligned hours each). Costs, origins and stresses are fixed
inputs. The operation reuses existing Decimal simulation and chronological rules,
without fitting, threshold optimization, a ranking or a recommended winner.

Compare absolute outcomes and policy-minus-regular differences for wealth,
quantity, cash reserve, native fees, waiting and drawdown at 90/180 days. Origins
are selected with a fixed 180-day spacing, independent of stressed data. Missing
or pending evidence stays in explicit excluded denominators. Direction/stability
claims require the complete qualified declared grid and at least two distinct
origins; scenario cells are dependent and are not independent sample counts.

Known-at and retrospective modes remain separate. Missing-hour stresses remove
all overlapping vintages rather than inventing replacement prices or fills. A
stored calculation captures at most 100,000 vintages once and then computes from
that frozen evidence. Additional bounds are 216 operation units, 100,000 expanded
action-work units, 50 million conservative compute-work units and 2 MiB result;
large grids fail before opening storage.

MCP: `get_plan_robustness(request, as_of)`. Compact replies retain stability and
qualified/excluded denominators; scenario evidence remains available through
frozen paging. Descriptive historical stability is not calibrated probability,
causality, observed execution or prospective performance.
