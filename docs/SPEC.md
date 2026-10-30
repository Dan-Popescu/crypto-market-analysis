# Imported prototype scope and acceptance criteria

This records the imported prototype scope as of October 28, 2026, with the
English-only policy applied. It does not claim implementation of the later
accumulation roadmap. See [MIGRATION.md](MIGRATION.md) for the language and
compatibility contract.

User-approved requirements: BTC and ETH research at 1, 7, 21, 30, 60 calendar-day horizons; weekly context; no paid collection API; uv; CLI and local/remote MCP; concise English explanations and reproducible charts; no trade execution. Whales postponed. Options lightweight and free. No tick archive.

Collect large venues through a capability-aware exchange registry (Binance, Coinbase, Kraken, OKX, Bybit, Bitstamp, Bitfinex, KuCoin, Gate, Crypto.com, Gemini, Bitget, MEXC, HTX). Do not infer MiCA compliance from a brand; jurisdiction and legal entity must have a dated official source to be labelled verified. Report region/access failures without bypassing them. Do not merge native USD, EUR or stablecoin books without a measured conversion rate.

Market bars hourly; funding/open interest hourly; order-book summaries every five minutes; options summaries six-hourly; macro/network/stablecoins daily. Public HTTP feeds only. No API payment keys. Rate limits, retries, bounded pagination, idempotent writes, source timestamps, ingestion timestamps, freshness and missing-source visibility.

Data: OHLCV, taker quote volume if published, funding and OI, depth at 0.5/1/2 percent with coverage checks, spread and imbalance; Deribit option IV and expiry aggregates; Coin Metrics community network indicators; DefiLlama stablecoin supply; FRED public CSV macro series; user-imported dated events/ETF flows (explicitly identified as manual until automated sources are validated).

Use SQLite WAL for concurrent collector/MCP operational access and versioned observations; provide Parquet export and DuckDB inspection over exported data. Preserve first-seen/known-at information. Keep data modes (live/import/demo) separate. Empty or unavailable data must never trigger synthetic fallback. Demo must be explicit and visibly labelled.

Cost control: liquidity summary detail retained for seven days by default, older points compacted to hourly medians with min/max, counts and coverage flags. Daily maintenance in the permanent scheduler, plus an explicit maintain CLI. Historical intraday replay is limited by retention; candle revisions remain untouched.

Research computes trend/volatility/spot pressure/coverage. Forecasts are empirical historical estimates and remain labelled experimental, with chronological walk-forward evaluation, non-overlapping evaluation origins, horizon purge, same-source contiguous daily bars, upward/downward endpoint and intraperiod barrier events. No invented calibrated probabilities. No LLM dependency or LLM cost for collection/calculation/charts; the host AI consumes structured evidence and writes the analysis. Calendar-red close and forward-return are distinct.

CLI: init, venues, collect, run, status, brief, forecast, evaluate, chart, export, import-events, demo, serve; all noninteractive with JSON output. MCP exposes read-only analysis/catalogue/metrics/forecasts/chart tools, STDIO and Streamable HTTP; network mode binds localhost by default, authenticated public deployment only through documented HTTPS OAuth proxy. No arbitrary code/SQL/filesystem tool. Tools return bounded results and provenance.

Verification: deterministic fixtures for provider parsing, unsupported capabilities, sparse books, idempotence, quote separation, partial failures, no future rows, no incomplete candles, chronological forecast isolation, gaps, and honest demo/missing data. End-to-end offline demo CLI and real MCP protocol tests; live public-source smoke checks when network allows.
