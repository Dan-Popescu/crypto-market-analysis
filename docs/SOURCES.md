# Qualified sources and retrospective history

The source catalogue is a dated documentation contract, not a live availability
claim. `source_catalog.source_catalog()` exposes each endpoint's authentication,
cost, page cap, native unit, timezone, retention, earliest/latest verified time,
rights evidence and check dates. Documentation was reviewed on **2026-10-28**.
Runtime access remains **untested** in these profiles. Earlier environment probes
that returned proxy HTTP 403 do not prove a provider is globally unavailable;
conversely, a readable documentation page does not establish live collection.
No public network call is made by the catalogue or by the offline tests.

Global analysis includes 15 material spot venues: Binance, Coinbase, Kraken, OKX,
Bybit, Bitstamp, Bitfinex, KuCoin, Gate, Crypto.com, Gemini, Bitget, MEXC, HTX and
Bitvavo. Their documented derivatives routes remain separate endpoint contracts;
provider support is distinct from unified-connector support. A documented candidate
endpoint is not an implemented adapter. Purchase eligibility never filters global
market analysis. EUR, USD, USDT and USDC remain separate native markets; no parity
or conversion is assumed. Actual active BTC/ETH listings must be discovered.

## French purchase candidates

These official AMF PSCA passport records were reviewed on 2026-10-28. The dates
below describe intended commencement of services in France, **not** the original
MiCA authorization grant. Evidence applies only to the named entity and services,
not to every product operated under the brand. Contract entity, service, source
rights and execution conditions still have distinct meanings.

| Candidate entity | Home regulator | Intended French commencement | Listed services |
| --- | --- | --- | --- |
| [Payward Europe Solutions Limited](https://protectepargne.amf-france.org/acteurs-listes-blanches/psca/payward-europe-solutions-limited-kraken-digital-asset-exchange-kraken) (Kraken) | Central Bank of Ireland | 2025-06-25 | Custody, exchange for funds/crypto, execution, placing, reception/transmission, portfolio management, transfers |
| [Coinbase Luxembourg S.A.](https://protectepargne.amf-france.org/acteurs-listes-blanches/psca/coinbase-luxembourg-sa) | CSSF | 2025-06-20 | Custody, exchange for funds/crypto, execution, placing, reception/transmission, transfers |
| [Bitvavo B.V.](https://protectepargne.amf-france.org/acteurs-listes-blanches/psca/bitvavo-bv) | AFM | 2025-07-14 | Custody, operating a crypto trading platform, transfers |

Bitvavo is primarily a native EUR candidate; USDC stays USDC. A native USD pair
was not established. The public schedule does not establish a user's fee tier.
Bitvavo's public order-book timestamp is documented in nanoseconds, while its
candle timestamps use milliseconds; adapter normalization must preserve that fact.

## Access, history and rights

Basic documented spot REST access requires no key and no paid subscription.
That says nothing about eligibility to trade or permission to redistribute data.
Account APIs, private full books, high-frequency/VIP channels and optional keyed
services are outside this no-key claim. FRED graph CSV is an unkeyed web export;
FRED/ALFRED's documented API requires a free registered key. No key is configured
or obtained by these modules.

Important history limits remain visible: Kraken REST has the last 720 bars only,
including an uncommitted current bar; backward pagination cannot recover older
bars. Coinbase Advanced Trade documents 350 candles, while its separate Exchange
API and the inspected connector cap at 300. Bitvavo documents 1,440, Binance 1,000,
Bitfinex 10,000, KuCoin classic spot 1,500 and MEXC's current spot API 500. Gemini
has no documented deep-history time pagination. Requested history is never evidence
of obtained coverage, and current book snapshots cannot reconstruct historical books.

Rights remain endpoint-specific and may constrain the intended use. In particular:

- [Binance Vision dataset terms](https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md)
  v1.0 (2026-08-26) specify CC BY-NC-SA 4.0, subject to exceptions; the software's
  MIT licence is a different licence.
- [Coin Metrics Community](https://github.com/coinmetrics/data/blob/master/README.md)
  is CC BY-NC 4.0, not an unrestricted commercial source.
- [DefiLlama](https://defillama.com/terms) restricts republication/commercial extraction.
- [Coinbase market data](https://www.coinbase.com/legal/market_data) and
  [Bitvavo's Developer Agreement](https://bitvavo.com/en/developer-agreement) include
  material intended-use restrictions. Authentication does not settle reuse rights.
- FRED rights vary by series; VIXCLS includes Cboe copyright. Deribit redistribution
  rights and a durable unrestricted automatic ETF-flow API remain unresolved.

Real provider dumps, credentials and generated local reports remain outside Git.
Synthetic parser fixtures are committed. No paid API is required for core plans;
optional context failures must remain explicit and cannot block those calculations.

## Bounded hourly file imports

`history_import.import_history(...)` returns `HistoryImportResult(candles,
provenance)`. It reads a named local file, not a URL, and never extracts archive
members. It supports:

- Official Kraken headerless OHLCVT CSV: timestamp seconds, OHLC, base volume and
  trade count. Select an exact market member such as `XBTEUR_60.csv`.
- Binance Vision spot 12-column kline CSV or ZIP: exact native symbol and `1h`
  filename. A companion SHA256 line or digest is required. Pre-2025 spot timestamps
  use milliseconds; timestamps from 2025 onward use microseconds. Unit/date
  contradictions and incomplete hourly klines are rejected.

Only hourly data is supported by the existing `Candle` contract. Daily files are
rejected rather than relabelled as hourly. Unknown schemas, malformed rows,
conflicting duplicates, incomplete candles and wrong market filenames fail the
whole import. Missing no-trade buckets remain gaps; no bars are synthesized.

Default limits are 256 MiB per input file, 64 MiB per selected uncompressed member,
2,000 members and 200,000 CSV rows. `ImportLimits` permits explicit local adjustment
for official large Kraken archives. Choose the exact hourly member; archive paths,
duplicate member names, encrypted entries and traversal paths are rejected. The
parser hashes and reads the same bounded temporary input snapshot even if the
original file changes during import. The snapshot may spill to a temporary file;
archive members are never extracted.

Every candle has `data_mode="import"` and `observed_at` equal to the supplied
import capture time. Provenance records the file and member SHA256, immutable
hash-derived data version, schema, native market, checksum status, rows, coverage
and gap count. Persist that provenance with the import run. A file backfilled today
has `historical_known_at="unknown"` and cannot manufacture strict past availability.
Later corrected files have different hashes. Repeated identical candles remain
idempotent through the existing versioned store.

## Official schedules and optional context

`parse_bls_ics(text, observed_at)` parses CPI schedule entries, preserving UID,
sequence and source modification text. Floating BLS times use documented Eastern
Time; explicit `America/New_York` times follow daylight-saving rules. Ambiguous,
nonexistent, unsupported-zone, recurring and date-only entries keep time unknown.
Cancellations remain visible. Scheduled UTC time does not prove actual publication
or historical public availability.

`parse_fomc_calendar(html, observed_at)` parses official meeting rows and year
headings. Ordinary FOMC rows contain meeting dates only. Their `event_time` stays
`None`; the parser never infers 14:00 ET from a meeting date. An offset-bearing
HTML time in the same row may qualify a scheduled instant when its local date
matches the meeting end date. It still does not prove publication. Minutes,
press conferences, consensus estimates and surprise measures are separate products.
Both parsers enforce a two-megabyte input and 2,000-event limit.
`collect_calendars(..., client=...)` uses an explicitly injected PublicHTTP-compatible
client and reports failures per source; it starts no scheduled job. Parsers validate
bounded input after retrieval; transport download limits belong to the HTTP client.

`interpret_context(observations, as_of)` excludes future events/captures, selects
the latest visible revision and returns source values, units, data mode, event age,
ingestion age, interpretation and limitations. Changes require the same source,
asset, metric, quote, instrument, expiry, native unit, funding period and
actual/projected kind. Funding interval seconds, explicitly labelled hours or
unit-bearing text are accepted; conflicting or unitless intervals stay unknown.
Unknown OI units cannot support comparable changes. OI is not spot buying pressure;
quoted IV is not an event probability; global stablecoin supply is not an attributed
BTC/ETH inflow. No weighted buy/sell score is produced.

`etf_flow_row(...)` qualifies manually supplied fund-level rows in USD millions.
All dashed fund cells with a displayed total of `0.0` are pending, not zero flows.
Partial rows retain unknown complete-session flow. Explicit reported zero with
all funds present remains measured zero. The helper preserves session date,
source URL, capture time, raw reported total and completeness; it is not an
automated ETF feed or point-in-time historical release archive.
