# Local automatic data connections

Version 0.5.0 connects the existing calculations to captured/stored inputs.
The [implementation scope](DATA_CONNECTION_PLAN.md) preserves native markets,
availability clocks and explicit collection/issuance. No deployment or exchange
account is required. Public source availability still depends on network access.

## Explicit one-shot capture

```bash
uv run crypto-market-analysis capture-books --data-dir data/live --sources kraken,coinbase,bitvavo --assets BTC,ETH --quotes EUR,USD
uv run crypto-market-analysis collect-official-context --data-dir data/live --include fx,calendar --max-requests 12
uv run crypto-market-analysis collect-official-context --data-dir data/live --include fx,stablecoins --venues kraken --max-requests 12
```

These commands require live mode and the actual current clock. They write local
evidence and make bounded public requests; they never start a permanent worker.
Failures remain per-source errors, without synthetic replacement data.

Book capture retains bounded received bid/ask levels, provenance, receipt time,
exchange timestamp when supplied, and content hashes. The same response supplies
liquidity metrics. Received depth is not a full book; missing exchange timestamps
remain unknown and prevent inappropriate contemporaneous comparisons. Explicit
retention does not change the older collector's default storage profile.

ECB calendar parsing is based on inspected rendered date/title entries. Its raw
markup fixture is synthetic and transport/markup qualification remains explicit.
The daily FX XML retains its source date without inventing a publication hour.
Stablecoin prices retain their original native pair and any unknown market clock.
Response-byte hashes and decoded-text/unified-response hashes have distinct scope.
Reference rates are informational rather than executable conversions.

`--max-requests` bounds logical adapter operations. Catalogue loading and retries
can cause multiple wire requests; no exact HTTP accounting is implied.

## Read stored inputs

```bash
uv run crypto-market-analysis stored-venue-disagreement --data-dir data/live --asset BTC --quote EUR --sources kraken,coinbase --format markdown
uv run crypto-market-analysis stored-derivatives --data-dir data/live --asset BTC --quote USDT --source binance --format markdown
uv run crypto-market-analysis stored-quotes --data-dir data/live --quote EUR --sources ecb,kraken --format markdown
```

Stored commands never fetch. Requests can be supplied as bounded JSON using
`--request`; their explicit mode and native selectors determine the read. For
named imported datasets use `--mode import`. Derivatives preserve every required
known-at vintage in a bounded 48-hour/5,000-row read; exceeding the bound fails
rather than silently losing historical endpoints. Each calculation reports its
own consumed references, hash, selection, watermark and omissions; separate
analyses do not claim one global snapshot.

`stored-venue-costs` also requires budget and explicit per-source product, fee and
rule assumptions. Collection does not infer a user's fee tier, minimum order,
payment charges or France legal eligibility. Missing assumptions suppress costs.
The prior [explicit snapshot examples](OFFLINE_WORKFLOWS.md) define those fields;
the stored bridge uses `assumptions_by_source`, `product_by_source` and optional
`purchase_eligibility_by_source` mappings in its JSON request.

Date-only FX evidence remains visible as a dated reference. It cannot supply an
invented exact endpoint for a stablecoin basis calculation; independently timed
USD observations can still be analyzed. Unknown OI conventions remain unknown.

## Qualify four native markets

```bash
uv run crypto-market-analysis qualify-sources --data-dir data/live --request examples/source-qualification.json --format markdown
```

Qualification is read-only. BTC/EUR, ETH/EUR, BTC/USD and ETH/USD retain independent
source coverage, exact missing ranges, revisions, freshness and resource status.
A recent pair or generic successful run cannot certify another pair. Imported
history can meet coverage requirements while its historical availability remains
unknown. A 730-day hourly range does not automatically provide 730 daily returns;
those need 731 complete daily closes. Source rights, purchase eligibility and
actual runtime access remain separate from stored data coverage.

## One local workflow

```bash
uv run crypto-market-analysis local-workflow --data-dir data/import-research --mode import --request examples/local-workflow.json --format markdown
uv run crypto-market-analysis local-workflow --data-dir data/live --request examples/local-workflow-refresh.json --format markdown
```

The first example reads only. The second explicitly refreshes: inspect its sources,
markets, logical-operation/page bounds and capture selections before running it.
Historical `as_of` and non-live modes cannot refresh. Retained book capture avoids
duplicating the older liquidity fetch. Official context has a separately declared
logical-operation budget. Storage/query limits qualify the SQLite snapshot; they
are not filesystem quotas.

Qualification gates each market's analyses and optional issuance independently.
Missing/stale history suppresses issuance and remains inspectable. A fresh,
complete offline demonstration may calculate but remains synthetic/unqualified.
Issuance additionally requires `issue_assessments: true` and an explicit
`idempotency_key`; it is disabled in both examples. A retry recovers its original
without manufacturing a new publication or replacing it after data changes.

Read-only MCP counterparts are `get_stored_derivatives`, `get_stored_quotes`,
`get_stored_venue_costs`, `get_stored_venue_disagreement` and `qualify_sources`.
Collection and workflow writes are not MCP tools. See [MCP clients](MCP_CLIENTS.md).
Keep captures, reports, databases and personal fee assumptions outside Git.
