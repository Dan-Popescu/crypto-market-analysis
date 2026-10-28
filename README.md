# Crypto Market Analysis

A local, read-only BTC/ETH research tool for gradual spot purchases over weeks and months. It provides historical context, experimental purchase windows, hypothetical purchase-plan comparisons, chronological evaluation and optional 4/8-hour vigilance. CLI and MCP share the same calculations. It never executes orders, signs wallet operations or transfers funds. Collection, calculations and charts require no language model or paid data API.

## Offline demonstration

With Python and `uv` installed, run from the repository root:

```bash
uv sync --frozen
uv run crypto-market-analysis demo --data-dir data/demo-docs --days 800 --quotes EUR,USD --as-of 2026-10-28T00:00:00Z
uv run crypto-market-analysis context --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z
uv run crypto-market-analysis windows --data-dir data/demo-docs --mode demo --asset BTC --source demo --quote EUR --as-of 2026-10-28T00:00:00Z --horizons 1,3,7,14 --thresholds 0.02,0.05,0.10
uv run crypto-market-analysis compare-plans --data-dir data/demo-docs --mode demo --plan-config examples/accumulation-plan.json --as-of 2026-10-28T00:00:00Z
```

These are **synthetic** scenarios, with independent EUR and USD prices, not a measured FX conversion or real-market evidence. All modes (`live`, `import`, `demo`) remain separate. Failed collection never triggers synthetic replacement data. The fixed demo includes two years of context and complete 90/180-day outcomes for the example plan starting January 1, 2026. Smaller fixtures or later starts can legitimately produce missing context or pending outcomes.

See [the accumulation guide](docs/ACCUMULATION.md) for reproducible evaluation, journal, vigilance, chart and import examples; [MCP clients](docs/MCP_CLIENTS.md) includes a local protocol example. [Implementation status](docs/IMPLEMENTATION_STATUS.md) separates implemented code, verification evidence and blocked live checks. No predictive advantage over regular purchases has been established.

## Sources and configuration

```bash
uv run crypto-market-analysis sources
uv run crypto-market-analysis venues
uv run crypto-market-analysis collect --venues kraken,coinbase,bitvavo --quotes EUR,USD --tasks market --days 7
uv run crypto-market-analysis status
```

Network commands require source access and can fail by region or environment. The current cloud's new public-source availability checks were blocked by its proxy allowlist; they establish no successful new collection. The registry contains 15 analytical venues, including Bitvavo. Global analysis includes material venues independently of purchase eligibility. France is the purchase jurisdiction; Kraken, Coinbase and Bitvavo are configured candidates, with dated evidence attached to particular legal entities and services. This is not brand-wide approval, execution-quality evidence or a reason to bypass access restrictions.

Copy [examples/observer.toml](examples/observer.toml) to `observer.toml`. Native EUR, USD, USDT and USDC markets stay separate. The example bounds order-book requests to three venues while retaining all 15 for market collection. Omitting `liquidity_venues` retains the runtime default of all configured analytical venues. Inspect coverage, ages, errors and licensing limits in the [data catalogue](docs/DATA_CATALOG.md) and [operations guide](docs/OPERATIONS.md).

The `OBSERVER_*` environment variables, `observer.toml`, `observer.sqlite` and `observer:read` MCP scope retain their names. See [schema and configuration migration](docs/MIGRATION.md).

## Development and evidence

The repository is [Dan-Popescu/crypto-market-analysis](https://github.com/Dan-Popescu/crypto-market-analysis), imported from Crypto Observer v0.1.0. The Python package is `crypto_market_analysis`.

```bash
uv sync --frozen --extra dev
uv run --frozen python tools/check.py
uv build --wheel --offline
```

Checks use temporary data and local MCP transports, independently of public APIs. Record the exact tested revision, commands, results and unavailable checks before merging. GitHub Actions remains disabled; no hosted CI success is assumed. Original author-reported validation remains in [VALIDATION.md](docs/VALIDATION.md), separately from independently executed [import checks](docs/IMPORT.md).

Keep budgets, portfolio records, databases, runtime journals, secrets and generated personal reports outside version control. Ledgerly remains the source of actual holdings and acquisition history; optional local inputs do not imply a live Ledgerly connection. No permanent job, external notification, account connection or public deployment is activated by these changes.
