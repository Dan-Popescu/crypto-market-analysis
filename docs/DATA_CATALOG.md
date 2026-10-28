# Data catalogue

One reference market is selected per asset and venue. Venues mainly quoted in stablecoins prefer USDT, then USDC; Coinbase, Kraken, Bitstamp, Gemini, Bitfinex and Crypto.com prefer USD. This convention does not measure which market is deepest and does not cover every pair on a venue. Histories and order books in different quote currencies are never implicitly added together.

All market timestamps use UTC. Observations carry source, asset, metric, unit, optional quote currency, event time and ingestion time; versions and modes are retained in SQLite. A missing value is neither zero nor a neutral signal. Source access can fail, including with HTTP 403; inspect `status` and errors. USD, EUR, USDT and USDC are neither merged nor converted implicitly.

| Family | Source and contents | Interpretation and limitations |
| --- | --- | --- |
| Prices and volumes | Registry venues, hourly OHLCV candles; quote volumes and aggressive buying when published | Days use only 24 aligned, closed hours. Complete weeks run Monday–Sunday UTC. No tick archive or currency merging. |
| Order books | Bid/ask, spread in basis points, depth at 0.5%, 1% and 2%, imbalance | Values use the native quote currency. If the book does not cover the full band, depth is an explicitly flagged **lower bound**; do not compare it as complete depth. |
| Derivatives | Open interest and funding published by venues | Units and contract sizes depend on the source; the unit may be unknown. Actual and projected rates differ; the funding period may be unknown, and 1-hour and 8-hour rates are not directly comparable. |
| Options | Deribit: near-the-money IV for target horizons of 7, 30 and 60 days, open interest by expiry and put/call IV difference | Nearest actual expiry subject to liquidity and distance constraints; it need not match the target horizon exactly. The difference compares strikes with similar moneyness, **not** 25-delta skew. Quoted IV is not a calibrated probability. |
| Network | Coin Metrics Community: active addresses, transactions, realized capitalization when available | Check source coverage and licensing conditions; the Community license is non-commercial (NC). |
| Stablecoins | DefiLlama: aggregate supply of USD-pegged stablecoins | Global multi-chain series, not capital flows attributed to BTC or ETH. |
| Macro | FRED: 10-year real yields, trade-weighted dollar, 2-year and 10-year yields, VIX | Historical values come from the current vintage; their exact historical availability is unknown. They are not used as historical scenario features. |
| Calendar and ETF | Manually imported, sourced events | No guaranteed comprehensive calendar or automated ETF feed. Events do not prove publication at a historical date; the dedicated manual ETF metric import still needs confirmation in the interface. |

Initial scenarios are experimental, based on hourly history aggregated into complete days and neighboring historical regimes. They distinguish a lower endpoint close from barrier crossings; they are neither a causal forecast nor a calibrated probability. Insufficient chronological evaluation must remain explicitly insufficient.
