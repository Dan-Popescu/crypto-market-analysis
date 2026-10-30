# V1 validation

This document preserves the report supplied with the Crypto Observer v0.1.0 ZIP.
The results below are those reported by the original author for that delivery.
Independent checks of the renamed import are recorded in [IMPORT.md](IMPORT.md).
Translation does not constitute a new execution of these checks.

The original checks were performed on October 28, 2026 in the development environment. They describe that execution; public interface availability may change.

## Software checks

- Complete suite: **71 tests passed**.
- `uv run ruff check src tests`: no errors.
- `uv lock --check --offline`: consistent lockfile.
- Python distribution built successfully with `uv build --wheel`.
- MCP integration tested over STDIO and local Streamable HTTP, including structured JSON responses and native images.
- Data revisions, availability at each date, non-overlapping windows, incomplete days and hourly scenario alignment checked.
- Source, currency and real/synthetic mode separation, idempotence and retention recovery after interruption checked.
- Two separate reviews covered requirements and project conventions; accepted issues were fixed and sensitive points checked again.

The final change to brief presentation was also checked with CLI tests and Ruff.

## Real-market collection

Hourly candles were obtained from **10 venues**: Coinbase, Kraken, OKX, Bitstamp, Bitfinex, Bitget, Crypto.com, Gate, Gemini and HTX. The requested history is not guaranteed at every venue.

The other four registry connectors encountered restrictions: Binance returned HTTP 451; the public Bybit, KuCoin and MEXC catalogues were inaccessible from that environment. Errors appear in status and never produce replacement data.

Deribit, DefiLlama, FRED and the basic free Coin Metrics metrics were also collected. Metrics restricted to paid plans are not enabled.

MiCA status remains **subject to verification by legal entity and jurisdiction**. The software does not assign regulatory status to an entire brand.

## Supplied examples

In the original ZIP, `live-*`, `btc-daily-live.md` and the two `btc-*-live.png` charts came from real collection, with analysis as of `2026-10-28T18:07:44Z`. These seven outputs are excluded from the GitHub repository; synthetic `demo-*` files are retained. The project does not include the complete collection database.

Approximately five years of Bitstamp history supported experimental frequencies over the five horizons. This demonstrates no predictive advantage.

Strict `known_at` evaluation produces **zero evaluable origins** on this history downloaded today: the system did not know it at the simulated historical dates. `retrospective` evaluation is available to explore current data with an explicit warning. It does not constitute validation under real-world conditions.

## Remaining configuration and measurements

- The server is not publicly deployed and no ChatGPT or Claude account is connected. Access from their websites requires HTTPS hosting and a compatible OAuth provider.
- Docker examples are provided; their build was not checked because Docker was absent from that environment.
- Infrastructure costs and storage volume over several months have not been measured. Detailed order-book summaries are compacted into hourly statistics after seven days by default.
- Macro history uses currently published values; it is not presented as a history of first releases. Calendar and ETF flows use sourced imports.
- Detailed whale tracking is deferred. V1 scenarios use prices and volatility; other data enrich the evidence an AI can interpret, without claiming a validated causal model.

Continuous collection starts only when `uv run crypto-market-analysis run` is launched. The delivery does not create an external scheduled task.
