# Crypto Market Analysis

A local BTC/ETH market analysis tool using public data: collection, descriptive analysis, experimental historical scenarios, charts and read-only MCP access. It never executes trades. Collection, calculations and charts do not call a language model.

## Getting started

From the project directory, with Python and `uv` installed:

```bash
uv sync --frozen
uv run crypto-market-analysis demo
uv run crypto-market-analysis brief --mode demo --asset BTC
uv run crypto-market-analysis chart --mode demo --asset BTC --kind weekly --output output/btc-weekly.svg
```

The demo is **synthetic**, stored in `demo` mode separately from `live` mode: it does not describe the real market. A collection failure never triggers synthetic data. To collect real observations and inspect coverage:

```bash
uv run crypto-market-analysis collect --venues kraken,coinbase --days 7
uv run crypto-market-analysis status
uv run crypto-market-analysis brief --asset BTC
```

The registry includes 14 venues by default; availability, assets, history and access vary by venue and region. A longer initial collection can be requested, without any guarantee of obtaining the full history:

```bash
uv run crypto-market-analysis collect --venues kraken,coinbase --tasks market --days 1825 --max-pages 100
```

Pagination is bounded to 100 pages, with up to 500 candles per page; venues may impose stricter limits. Inspect the result and `status`: partial data, errors and missing sources remain visible. Subsequent collection can be automated with `uv run crypto-market-analysis run`.

Forecasts are experimental: they compare historical situations, demonstrate no predictive advantage and establish no causality. Days must contain 24 complete hourly candles; weeks run from Monday through Sunday UTC. USD, EUR and stablecoin quotes remain separate. Public collection interfaces require no paid API (collection API cost: €0); infrastructure, network and hosting costs are not estimated without measurements.

See [operations](docs/OPERATIONS.md), the [data catalogue](docs/DATA_CATALOG.md), [MCP client setup](docs/MCP_CLIENTS.md), and `uv run crypto-market-analysis --help`.

The [validation report](docs/VALIDATION.md) records the checks and limitations reported for the original release. The [migration policy](docs/MIGRATION.md) distinguishes English output from technical identifier compatibility.

## Local or cloud development

The repository is [Dan-Popescu/crypto-market-analysis](https://github.com/Dan-Popescu/crypto-market-analysis).
It imports the Crypto Observer v0.1.0 prototype under the name **Crypto Market Analysis**.
The Python package is `crypto_market_analysis` and the command is `crypto-market-analysis`.
The `OBSERVER_*` variables, `observer:read` scope and `observer.toml` file
retain their names to preserve existing settings.

After checking out the repository, install dependencies and run the checks:

```bash
uv sync --frozen --extra dev
uv run --frozen python tools/check.py
```

These commands work on a PC and in a Codex cloud environment with `uv`
installed. The checks verify dependencies, code and tests using temporary data;
they do not collect from public sources. MCP tests use a local process and a
temporary local HTTP connection.

GitHub Actions is disabled for this repository. Each pull request must record
verification results for its final version before merging.

The repository contains source code and synthetic demonstrations. Real-market
reports from the original ZIP remain local, along with databases, secrets and
personal settings. Ledgerly retains portfolio management; its connection to
this analysis tool is not implemented yet.
