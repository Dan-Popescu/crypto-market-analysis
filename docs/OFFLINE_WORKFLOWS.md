# Offline workflows

Version 0.4.0 implements the remaining bounded offline projects from
[the completion plan](OFFLINE_COMPLETION_PLAN.md). No deployment, account key,
language model or paid data API is required. Local snapshots and examples are
inputs, not evidence that a real source is reachable or a policy performs better.

Install the locked environment:

```bash
uv sync --frozen --extra dev
```

## Backup, restore and original replay

These are explicit local operations. Keep backups outside the source directory
and generated reports outside both the source and backup bundles.

```bash
uv run crypto-market-analysis backup --database data/my-research/observer.sqlite --destination /path/to/new-backup
uv run crypto-market-analysis verify-backup --bundle /path/to/new-backup
uv run crypto-market-analysis restore-backup --bundle /path/to/new-backup --destination /path/to/new-restored-data
```

The ordinary SQLite backup can run while normal market collection writes. A named
history dataset also has filesystem recovery evidence: stop its writers and add
`--quiescent-dataset` to `backup`. The tool detects observed changes and rejects
missing or corrupt payloads. The default dataset bounds are 10,000 evidence files
and 512 MiB; exceeding them fails rather than producing a partial backup.

Verification checks hashes, database integrity, schema and journal evidence.
Restoration publishes into an absent or empty directory and preserves the
source; named datasets receive a rebased directory while retaining their original
identity and manifest provenance. Database versions remain intact.

```bash
uv run crypto-market-analysis replay-assessment --data-dir /path/to/new-restored-data --id ASSESSMENT_ID --format markdown
uv run python examples/offline-recovery.py
```

Replay verifies an original assessment and recomputes supported frozen
context/windows/plan calculations from its archive. It does not collect data,
append an outcome or reconstruct missing future observations. An unsupported
historical rule is an explicit failure, not a successful replay under new rules.
The runnable Python example uses temporary synthetic data.

## Prospective scorecard

```bash
uv run crypto-market-analysis journal-scorecard --data-dir data/my-research --limit 100 --format markdown
```

The scorecard reads existing originals and outcomes without creating journal
tables. It shows 90/180-day plan wealth including reserve, paired differences
against regular purchases, waiting/cost metrics, qualified samples and all
inspectable scenarios. Native market, dataset lineage, rule, temporal class and
relative policy/cashflow cohorts stay separate. Disjoint selection is determined
by declared times before outcomes are inspected; pending and missing records
remain visible. Repeated appends do not add samples.

Event rates and Wilson intervals are descriptive. Anchor-relative forecasts are
not scored against incompatible issuance-relative outcomes. Actual prospective
90/180-day performance still requires elapsed time and subsequent observations;
synthetic fixtures and historical simulations cannot supply it.

## Shared BTC/ETH budget

```bash
uv run crypto-market-analysis budget-preview --request examples/shared-budget.json --as-of 1767225600 --format markdown
```

The envelope's contributions are the sole shared cash source. Embedded plans'
contribution declarations are not added again. Declared plan order resolves
simultaneous requests. Fees, exclusive commitments, per-plan target caps and
same-time reserve releases use the purchase-plan accounting implementation.
Future deposits cannot fund earlier requests. A buy fallback stays committed;
the price-free preview invents neither fills nor rounding refunds.

Use separate envelopes for EUR and USD and keep modes separate. This optional
calculation is not a holdings ledger; Ledgerly remains authoritative.

## Venue snapshots

```bash
uv run crypto-market-analysis purchase-costs --request examples/offline-purchase-costs.json --as-of 1793145600 --format markdown
uv run crypto-market-analysis venue-disagreement --request examples/offline-venue-disagreement.json --as-of 1793145600 --format markdown
```

The examples are synthetic. Real requests need explicit native-market identity,
capture/event timestamps and provenance. Purchase costs additionally require
actual ask levels, the spot-order-book product, fee tier and explicit fees,
payment/withdrawal assumptions, minimums and steps. Unknown assumptions suppress
cost calculations. Aggregated depth cannot reconstruct an amount-specific fill.
Available book depth, freshness and source clock skew constrain interpretation;
even complete modeled coverage is not a fill guarantee or measured execution.

Disagreement retains stale, missing and outlier sources. It reports aligned
same-quote dispersion without a fair-value index or purchase signal. France
entity/service eligibility remains separate from globally useful analytical
coverage. EUR, USD, USDT and USDC are never silently merged.

## EUR, USD and stablecoin context

```bash
uv run crypto-market-analysis quote-context --request examples/quote-context.json --as-of 1793145660 --format markdown
uv run crypto-market-analysis import-calendar --data-dir data/manual-calendar-example --kind ecb-manual --file examples/ecb-manual-calendar.json
```

The quote example retains the original native observations and explains an
explicit indicative EUR/USD reference and directly observed stablecoin basis.
Inverse orientation and any EUR-to-USD informational calculation have visible
transformation provenance. Stale, unavailable or skewed endpoints suppress derived
values. An indicative FX reference is not an executable conversion price.

`ecb-manual` accepts a sourced manual JSON transcription, not an automatic official
ECB feed. The example contains visibly invented future events for schema
demonstration; replace them and their provenance with independently verified
entries before using them as real context. Date-only meetings remain date-only.
Explicitly timed entries preserve European daylight saving, revisions and
cancellations. A scheduled time does not establish actual publication.

## Native derivatives context

```bash
uv run crypto-market-analysis derivatives-context --request examples/derivatives-context.json --as-of 1767398400 --format markdown
```

The synthetic example keeps its USDT-native perpetual market distinct from fiat
quotes. It compares funding and open interest over declared 4h/24h endpoints,
preserving actual/projected funding, funding period, counting convention,
contract size, symbol and settlement. Historical endpoints require both the event
and its observation to be available by that endpoint. Missing conventions, late
versions, stale points and excessive endpoint skew leave changes unknown. No
cross-source aggregate, annualized yield or crash probability is inferred.

## Read-only MCP

The calculation tools accept bounded structured requests, not local file paths:
`preview_shared_budget`, `calculate_purchase_costs`,
`calculate_venue_disagreement`, `get_quote_context` and `get_derivatives_context`.
`get_journal_scorecard` reads existing local journal data; `replay_assessment`
accepts an existing assessment ID. Backup and restoration are CLI-only writes.
See [MCP clients](MCP_CLIENTS.md) for transport setup.

For request-file calculations, the request's explicit `data_mode` is authoritative,
as with the existing plan preview. Global collection defaults do not relabel it.
Keep personal files, backups and generated reports outside Git.
