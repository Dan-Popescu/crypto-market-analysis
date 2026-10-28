# Data catalogue

All market timestamps use UTC epoch seconds internally. Every observation retains source, asset, metric, native unit, optional quote currency, event time, ingestion/availability time and data mode. Preserve revisions and gaps. A missing value is neither zero nor a neutral signal. EUR, USD, USDT and USDC are never merged or converted implicitly.

## Source qualification and access

`uv run crypto-market-analysis sources` returns the dated machine-readable catalogue: endpoint, documentation, authentication/cost, page cap, timestamp units, history limits, rights, implementation state and runtime access evidence. Documentation qualification dated October 28, 2026 establishes no successful live availability. New public-source checks in this cloud were blocked by the proxy allowlist; earliest/latest observed history and gaps remain unverified unless actual observations establish them. The original author's different environment and historical results remain separately recorded in [VALIDATION.md](VALIDATION.md).

Access permission, redistribution rights and purchase eligibility are separate questions. An unpaid public endpoint does not grant permission to republish its data. No paid API is required by the core workflow. Do not commit licensed dumps, private records or runtime journals. Some optional sources have noncommercial or per-series restrictions; consult the catalogue's primary URLs before reuse.

The analytical registry includes Binance, Coinbase, Kraken, OKX, Bybit, Bitstamp, Bitfinex, KuCoin, Gate, Crypto.com, Gemini, Bitget, MEXC, HTX and Bitvavo. Global market evidence remains independent of purchase eligibility. For France, dated AMF records identify Payward Europe Solutions Limited (Kraken), Coinbase Luxembourg S.A. and Bitvavo B.V., with their listed services. Recheck the actual contracting entity and intended service; a record is not brand-wide authorization or evidence about best execution.

Explicit quote selection requests native markets where supported, with unavailable pairs reported. Default reference-market preferences do not identify the deepest market and do not cover every pair. Supported protocol endpoints are capability-dependent; a documented derivative route does not mean every unified collector implements it. A regional HTTP 403/451 remains an access failure, without bypass or synthetic replacement.

| Family | Implemented input and content | Interpretation and limitations |
| --- | --- | --- |
| Prices and volumes | Capability-aware venue adapters; hourly OHLCV, quote and taker-buy volume when published | Complete days require 24 aligned closed hours; complete weeks run Monday–Sunday UTC. No ticks, interpolation or implicit quote conversion. Aggressive spot pressure requires published executed-volume data, not open interest. |
| Order books | Public snapshots; bid/ask, spread in basis points, depth at 0.5%, 1% and 2%, imbalance | Native quote values. Incomplete band coverage is a flagged lower bound. Global snapshots do not identify traders or prove spoofing. Aggregated summaries cannot reconstruct full fill curves. |
| Derivatives | Existing venue adapters for available open interest and actual/projected funding | Preserve symbol, contract convention, native units and interval. Unknown units remain unknown. One-hour and eight-hour rates are not directly comparable; open interest does not identify spot buying or all liquidation levels. |
| Options | Deribit current summaries: near-the-money IV, actual expiry, open interest and comparable-moneyness put/call IV difference | Targets 7/30/60 days need not match an available expiry. IV is quoted volatility, not calibrated probability. The difference is not 25-delta skew. No complete historical surface is supplied. |
| Network | Coin Metrics Community daily indicators when available | Coverage is asset/metric dependent; active addresses are not people. CC BY-NC 4.0 restrictions apply. Historical publication time is unknown. |
| Stablecoins | DefiLlama aggregate USD-pegged circulating supply | Global supply is not BTC/ETH-specific capital inflow. Terms limit reuse; historical revisions and availability remain unknown. |
| Macro | Existing FRED graph CSV series | Current-vintage values are not a first-release archive. Per-series rights apply, including Cboe restrictions for VIX. FRED/ALFRED's free-key API is catalogued, not configured or integrated as a verified intraday vintage source. |
| Release calendar | Bounded BLS CPI ICS and Federal Reserve FOMC HTML parsers, local import and explicit one-shot collection | Scheduled times are not actual publication evidence. Timezone/DST handling is explicit. FOMC dates stay date-only without an explicit timestamp. Collection can be unavailable per source. |
| ETF context | Sourced manual `import-metrics` fallback and unit-aware interpretation | No complete automated feed. Net flows differ from AUM revaluation. Farside USD-million values require conversion by their stated unit; all pending fund cells do not become measured zero. Automatic API/redistribution permission is unresolved. |
| Liquidations | Documented prospective stream candidates | No verified implemented full live feed or historical liquidation map. Binance snapshots can omit bursts; stream gaps are unknown, not zero. Vigilance reports this missing coverage. |

## Historical archive inputs

`import-history` supports explicit local Kraken hourly OHLCVT and Binance Vision hourly spot files for BTC/ETH and native EUR/USD/USDT/USDC quotes. Exact filenames identify the market and interval. ZIP members are selected explicitly; files are not extracted. Binance requires an official expected SHA-256; Kraken accepts an optional expected checksum. The importer retains file/member hashes, source schema, observation time, rows and gaps. File/member/row limits prevent unbounded archive processing. See [archive commands and limits](ACCUMULATION.md#local-history-and-calendar-inputs).

Kraken uses epoch seconds. Binance spot history uses milliseconds before 2025 and microseconds from January 1, 2025; contradictory units fail. Kraken REST's latest 720 bars cannot establish years of hourly history through backward pagination. Archive coverage plus REST can still leave a gap. Downloaded historical data is retrospective: it does not establish what the system knew at the old event dates. No raw licensed archive is included in the repository.

## Availability, retention and analytical use

`live`, `import` and `demo` are separate. The synthetic fixture is explicit and uses independent EUR/USD scenarios. Versions retain ingestion availability; first downloaded today never means known years ago. Calendar records separately retain scheduled time, actual publication status, source observation and source version. Date-only records are stored without inventing a market timestamp.

Detailed liquidity summaries are retained seven days by default, then compacted into hourly median/min/max/count/coverage summaries. This limits old intraday replay; it does not create a tick archive. Quotes, source identity and availability remain attached to results and input fingerprints.

Context is descriptive. Purchase-window frequencies are experimental and require adequate non-overlapping evidence. Plan execution uses configured historical costs and conservative bar opens, never a measured full-book fill. A lower acquisition cost alone does not show a better outcome; terminal holdings plus reserve and underperformance against regular purchases are reported separately. Unknown future outcomes stay pending. Optional context is not presented as a validated causal predictor.
