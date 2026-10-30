# Operations

## Configuration and collection

Copy `examples/observer.toml` to `observer.toml` and adjust venues, assets and intervals. A relative TOML `data_dir` is resolved against the file's directory. `--data-dir` overrides it. `uv run crypto-market-analysis venues` displays the registry; a brand alone does not establish an entity's MiCA status.

```bash
uv run crypto-market-analysis collect --venues kraken,coinbase --tasks market,liquidity,options,context --days 7
uv run crypto-market-analysis run --config observer.toml
uv run crypto-market-analysis status --config observer.toml
```

By default, the continuous collector schedules market data hourly, order books every five minutes, options every six hours and context every 24 hours; each source can be unavailable or partial. HTTP 403, HTTP 451 and regional restrictions remain access failures, never neutral data. Do not try to bypass them. Collection uses public sources without a paid API or language model. `live`, `demo` and `import` modes remain separate; there is no automatic synthetic fallback.

To generate an independent local daily report, create an output directory and add a line such as the following to your crontab, replacing the absolute paths:

```cron
CRON_TZ=Europe/Paris
10 8 * * * cd /path/to/crypto-market-analysis && /path/to/uv run --no-sync crypto-market-analysis brief --asset BTC --output /path/to/reports/btc.md
```

`CRON_TZ` support depends on the cron implementation. This line writes the report locally; it does not send it. Launch time follows Europe/Paris and its daylight-saving changes, but market closes and complete weeks use UTC: the week starts Monday at 00:00 UTC and counts only after Sunday's UTC close. The report depends on data already collected; monitor freshness with `status`.

## Storage, export and backup

SQLite uses WAL and allows the collector and MCP server to share the same local database. Keep the database and its WAL/SHM files together when present; for a consistent backup, use SQLite's backup function or stop processes before copying. The shared volume must provide SQLite's local locking semantics; do not assume network storage is suitable.

```bash
uv run crypto-market-analysis export --output output/parquet
uv run python -c "import duckdb; print(duckdb.sql(\"SELECT source, asset, quote, count(*) AS n FROM read_parquet('output/parquet/candles.parquet') GROUP BY 1,2,3\").df())"
```

Export writes the latest available versions as Zstd Parquet; a file is created only when its table contains data. This is not a complete archive of every version. The DuckDB example inspects an exported file locally; it is not a SQL interface exposed through MCP.

Detailed order-book summaries are retained for seven days (`liquidity_retention_days`). Each day, the continuous collector compacts older points into hourly medians with minimum, maximum, observation counts and coverage. A backlog is processed in batches of seven old days, then the collector resumes catch-up on its next iteration while eligible points remain. This reduction does not affect candles, funding or options. It limits replay of old intraday observations; medians carry the availability time of their calculation. `uv run crypto-market-analysis maintain --keep-days 7` processes the oldest eligible day; repeat it when `remaining_old_rows` is nonzero for manual catch-up. No ticks are stored.

## Evaluating scenarios

Scenarios use 24-hour windows aligned with the latest known hourly close; the anchor time and price are displayed. Days and weeks in calendar charts remain UTC. `uv run crypto-market-analysis evaluate --asset BTC --horizon 7` uses `known_at`: each origin is evaluated only with candle versions already observed at that date, at the same UTC anchor hour as the current scenario. Actual collection delay is recorded; an origin more than one hour late is excluded. The first window's extremes can include movement before issuance: barrier results remain candle-based diagnostics, not validation of execution at a tradable price. History downloaded today is not an archive of what was known a year ago and therefore does not manufacture validation results at those earlier dates. Validation coverage builds through version retention and observation over time.

To explore current history, `uv run crypto-market-analysis evaluate --asset BTC --horizon 7 --validation retrospective --output output/evaluation.json` runs an explicitly retrospective chronological simulation, without guaranteeing historical version availability. Use `chart --kind calibration --validation retrospective --output output/calibration.png` for its chart. This diagnostic proves neither real-world calibration nor profitability. Add `--mode demo` to both commands for the synthetic demonstration.

## Local and remote MCP

```bash
uv run crypto-market-analysis serve --transport stdio
uv run crypto-market-analysis serve --transport streamable-http --host 127.0.0.1 --port 8000
```

Local HTTP listens on `127.0.0.1:8000` at `/mcp`. STDIO is suitable for a local MCP client. Remote mode requires an external OAuth provider, registration configuration compatible with the MCP client (CIMD or DCR depending on the provider/client), an HTTPS reverse proxy and tokens carrying the `observer:read` scope. The server validates JWTs against the configured issuer and JWKS keys. Set `OBSERVER_OAUTH_ISSUER`, `OBSERVER_JWKS_URL` and `OBSERVER_PUBLIC_URL` (a public HTTPS URL ending in `/mcp`); `OBSERVER_OAUTH_AUDIENCE` is optional. Binding to `0.0.0.0` is refused without OAuth configuration. Auth0 or Keycloak can be configured as external providers; neither is installed, connected or deployed by this project. Check actual registration-flow support in your provider and client before exposing the service.

`deploy/compose.yaml` runs the collector and MCP with a shared database and keeps MCP on the Linux host's loopback interface through `network_mode: host`. From the repository root:

```bash
docker compose -f deploy/compose.yaml up --build
```

This file is a local example, not a public deployment or production OAuth configuration. Host networking differs by Docker platform; verify its behavior before use.

## Imports

`examples/events.json` is a valid empty list: replace it with your own verified events. Each object requires `title`, `event_time` (epoch seconds or a timezone-aware ISO date) and an HTTPS `source_url`, for example with an actual date verified against an official source such as https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm. Do not fill in assumed dates. `uv run crypto-market-analysis import-events --file examples/events.json` imports this calendar as manual and non-exhaustive; historical publication time is not verified automatically. No complete automated ETF feed is claimed.

Candles can be imported through `import-candles` with an explicit source and quote currency; see `--help`. Imported data is isolated in `import` mode.

Candle CSV requires `open_time,open,high,low,close,volume`; `open_time` is the UTC hour's opening time as Unix seconds or a timezone-aware ISO date. Optional columns: `quote_volume,taker_buy_quote_volume`. Example: `uv run crypto-market-analysis import-candles --file candles.csv --source my-provider --asset BTC --quote USD`, then `uv run crypto-market-analysis brief --mode import`.

`import-metrics --file measures.csv` accepts `source,asset,metric,event_time,value,unit,quote,source_url` columns and creates observations in `import` mode. An HTTPS provenance URL is required; `event_time` is Unix seconds or a timezone-aware ISO date. For ETF flows, choose an explicit metric such as `etf_net_flow_usd`, asset BTC or ETH and unit USD, checking that these are flows rather than a change in holdings value. Historical availability is not reconstructed: these imports are not eligible features for the current backtest.
