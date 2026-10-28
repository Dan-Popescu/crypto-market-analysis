# Accumulation improvements: reproducible local workflows

These commands exercise the focused repairs, next-tranche brief, plan constructor,
cost sensitivity and journal accounting. They do not demonstrate an accumulation
advantage. Ledgerly remains authoritative for actual transactions and holdings.

## Fixed offline demonstration

Run from the repository root after `uv sync --frozen --extra dev`. The following
workflow uses only synthetic observations and local files. With a read-only home,
set `UV_CACHE_DIR` to a writable cache directory before running uv. The fixed cutoff is
October 28, 2026 at 00:00 UTC. Keep the generated database and reports outside Git.

```bash
CMA_RUN_DIR=$(mktemp -d)
CMA_AS_OF=2026-10-28T00:00:00Z
uv run --offline --frozen crypto-market-analysis plan-init \
  --request examples/plan-request.json --output "$CMA_RUN_DIR/plan.json"
uv run --offline --frozen crypto-market-analysis plan-preview \
  --plan-config "$CMA_RUN_DIR/plan.json" --as-of "$CMA_AS_OF" \
  --format json --output "$CMA_RUN_DIR/preview.json"
uv run --offline --frozen crypto-market-analysis demo --data-dir "$CMA_RUN_DIR/data" \
  --days 800 --quotes EUR --as-of "$CMA_AS_OF"
uv run --offline --frozen crypto-market-analysis accumulation-brief \
  --data-dir "$CMA_RUN_DIR/data" --mode demo --asset BTC --source demo --quote EUR \
  --plan-config "$CMA_RUN_DIR/plan.json" --as-of "$CMA_AS_OF" \
  --format markdown --output "$CMA_RUN_DIR/brief.md"
uv run --offline --frozen crypto-market-analysis cost-sensitivity \
  --data-dir "$CMA_RUN_DIR/data" --mode demo --plan-config "$CMA_RUN_DIR/plan.json" \
  --cost-cases examples/cost-cases.json --validation retrospective \
  --as-of "$CMA_AS_OF" --format json --output "$CMA_RUN_DIR/costs.json"
uv run --offline --frozen crypto-market-analysis journal-summary \
  --data-dir "$CMA_RUN_DIR/data" --mode demo --as-of "$CMA_AS_OF" \
  --limit 20 --format json --output "$CMA_RUN_DIR/journal.json"
```

The last command should report an empty journal: calculations do not issue an
assessment. `issue` explicitly freezes a local assessment; `observe` explicitly
appends outcomes. Those operations use actual issuance and observation clocks;
this fixed historical demonstration does not manufacture prospective records.

The example expands six monthly dates from May 31 using `month_end_rule: clip`:
May 31, June 30, July 31, August 31, September 30 and October 31. `skip` instead
omits months without the original day, and counts generated occurrences. Expanded
schedules must fit within 180 days. Decimal amounts are strings. The constructor
emits the existing normalized schema-1 configuration; it does not choose a policy.

At the fixed cutoff, the next declared tranche is October 31. The split policy
allocates half immediately and half at a 5% decline, waits at most seven days and
falls back to buying. The deferred policy allocates everything to a 10% decline,
waits fourteen days and falls back to reserve. These are distinct illustrative
rules. `plan-preview` identifies equivalent policies when supplied.

A EUR 100 quote notional plus a 0.2% quote fee and EUR 1 fixed fee requires
EUR 101.20. Splitting it into two positive slices incurs two fixed fees. Depositing
EUR 100 therefore exposes a funding shortfall. The simulator limits spending to
funded cash; the price-free preview reserves commitments without inventing fills
or rounding refunds. Base-asset fees instead reduce received units. A cap on cash
locked in targets is separate from a protected cash floor.

The brief separates current observed conditions, hypothetical policy states and
historical outcomes. Every analytical section uses the pinned source/native
quote, cutoff and input reference. Stale or missing anchors suppress current
price-based targets. Future targets based on today's anchor are indicative; their
actual activation anchor remains unknown. Pending targets show amount, threshold,
deadline and fallback. A future 90/180-day outcome remains pending.

EUR and USD are independent native-market analyses. Seed `--quotes EUR,USD` only
when both synthetic scenarios are wanted; this creates no measured FX relation.
Changing the plan's quote also requires matching native data. USDT/USDC do not
become EUR or USD.

## Reading repaired results and sensitivity

Markdown now exposes evaluation sample counts, exclusions, protocol,
underperformance and descriptive dispersion. Plan tables keep terminal valuations
separate and show free/locked cash, waiting, funding and unfilled exceptions.
JSON retains exact decimal strings and the complete public result structure.

`accumulation-v2` changes waiting and evidence accounting. Waiting includes funded
cash until fill, expiry, release or the terminal cutoff, including reserve expiry
and unfilled execution. `cash_waiting_quote_days` measures amount × elapsed days;
its target/execution components and cash-weighted waiting retain the distinction
between waiting for a decline and waiting for an eligible execution observation.
These amount-weighted measures do not grow merely because one allocation is
represented by several equivalent slices.

A complete terminal price alone does not establish a qualified execution path.
Evaluation `sample_size` counts qualified origins; `scenario_sample_size` retains
complete valuation scenarios. `unqualified_scenarios` and per-outcome
`evidence_qualification` expose missing execution coverage. Sparse scenarios stay
inspectable without entering qualified performance aggregates. Journal plan
summaries distinguish prospective, retrospective and hybrid chronology.

The cost file supplies one to three uniquely named cases. Only fee rate/fixed/
asset and spread/slippage overrides are accepted. Missing overrides inherit the
plan configuration. Cashflows, policy rules, input bars and terminal dates stay
fixed. The first caller case is the arithmetic baseline, with no preferred case.
Within-case differences are policy minus regular; cross-case differences are
case minus the first case. Ranking reversals describe hypothetical wealth ordering
and carry evidence qualification. Unknown terminal metrics remain unknown.
These assumed costs are not measured order-book fills or a venue price ranking.

## Observable endpoints and journal cohorts

The exact issuance-relative endpoint remains `exact_deadline_v1`. An issue at
00:30 cannot acquire a known exact 00:30 price from hourly candles. New outcomes
also expose `hourly_after_deadline_v1`: the first aligned hourly close at or after
the nominal deadline, with its actual mark time and offset. Missing marks remain
unavailable. This separately named observation does not repair the exact event.

Historical anchor-relative estimates have no established forecast comparability
to the shifted hourly event. No matched probability scoring is introduced.
The bounded journal summary reports issued/mature/resolved/ambiguous/missing/
pending/excluded events in explicit market, lineage, rule and event-convention
cohorts. Repeated outcome appends do not create new issued events. Inspect the
summary's selected-record limit and exclusions before interpreting denominators.

## Native-market history dataset: preparation, not real-source acceptance

The initial route is an explicitly named local Kraken research dataset. BTC/EUR
is the first qualification target; ETH/EUR, BTC/USD and ETH/USD have separate
coverage records. A reference venue does not establish purchase eligibility for a
legal entity and service in France. Runtime source acceptance remains blocked/unverified in this
managed environment. Synthetic tests, archive parsing and executable commands do
not establish real download access, intended-use permission or a successful
native-market analytical report.

Create a local configuration outside Git, for example `/tmp/cma-history/config.json`:

```json
{
  "name": "kraken-research-20261028",
  "directory": "research",
  "target_start": 1730073600,
  "target_end": 1793145600
}
```

The directory is relative to the configuration file. The target is an explicit
hourly half-open range from October 28, 2024 to October 28, 2026; do not silently
shorten it to conceal missing history. Initialization and status are offline:

```bash
uv run --frozen crypto-market-analysis history-dataset init \
  --dataset-config /tmp/cma-history/config.json
uv run --frozen crypto-market-analysis history-dataset status \
  --dataset-config /tmp/cma-history/config.json
```

Use Kraken’s [official archives](https://support.kraken.com/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data) and
[OHLC documentation](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
to qualify the actual source. After independently obtaining parts, create a local manifest:
`{"parts":[{"path":"archive.zip.001","url":"ACTUAL_OFFICIAL_URL","sha256":"ACTUAL_64_HEX_SHA256"}]}`.
Use actual URLs/hashes, all parts in their original binary order, and the exact
member name observed in that archive. Part paths are relative to the manifest.
The manifest is provenance, not an instruction to download. Staging is offline,
checks hashes and bounds, and imports only the selected native hourly member:

```bash
uv run --frozen crypto-market-analysis history-dataset stage \
  --dataset-config /tmp/cma-history/config.json \
  --parts-manifest /tmp/cma-history/parts.json --member XBTEUR_60.csv \
  --asset BTC --quote EUR --captured-at ACTUAL_RECEIPT_TIMESTAMP
```

Archive files can be several GB. Inspect configured part/archive/member/disk/row
budgets before staging; limits fail explicitly. Names alone do not prove date
coverage. Kraken REST exposes at most 720 recent entries and cannot retrieve old
history merely by changing `since`. Once source access is independently available,
these are explicit one-shot network operations, with no scheduler activation:

```bash
uv run --frozen crypto-market-analysis history-dataset refresh \
  --dataset-config /tmp/cma-history/config.json --asset BTC --quote EUR \
  --since 2026-10-01T00:00:00Z --limit 720
uv run --frozen crypto-market-analysis history-dataset plan-repair \
  --dataset-config /tmp/cma-history/config.json --asset BTC --quote EUR \
  --start 2026-10-01T00:00:00Z --end 2026-10-28T00:00:00Z
uv run --frozen crypto-market-analysis history-dataset repair \
  --dataset-config /tmp/cma-history/config.json --asset BTC --quote EUR --request-budget 1
uv run --frozen crypto-market-analysis context --data-dir /tmp/cma-history/research \
  --mode import --asset BTC --source kraken --quote EUR --format markdown
```

`plan-repair` is offline. `repair` persists progress and resumes within its request
budget; an empty/failed request does not fill a gap. Recent refresh does not mark
older holes complete. Inspect status, individual market gaps, attempts, capture
provenance and limits after each operation. The database is `observer.sqlite`.
Archive and REST materializations preserve import lineage, hashes and revisions;
there is no implicit union with live observations. Backfilled history remains
retrospective even when imported today. Genuine later captures may support a
separate future cohort after frozen issuance; the import label alone proves
neither prospectiveness nor its absence.
