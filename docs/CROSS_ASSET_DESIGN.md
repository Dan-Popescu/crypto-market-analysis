# Extending research beyond cryptocurrencies

The product's long-term scope includes cryptocurrencies, equities, commodities
and further asset classes. Version 0.9.0 implements multiple cryptocurrency asset
codes; accepting a ticker does not establish equity, commodity or futures support.
This document records design requirements and implementation gates, not delivered
non-crypto connectors or validated cross-asset financial models.

## Instrument identity precedes broader collection

A provider symbol is an alias, not the instrument's global identity. Resolve an
instrument and, where applicable, its listing before ingestion: asset class,
instrument form, venue,
native currency/unit, provider identifier and effective/known-at alias mappings.
Retain published identifiers such as ISIN or venue MIC when available and relevant;
their absence stays explicit. Cryptocurrency identities may also need network and
contract-address information. No one external identifier covers every class.

A stock and a token can share a ticker. The same security may have several
listings and quote currencies. A commodity exposure may be a physical reference,
an ETF/ETC or a particular futures contract. These remain distinct identities.
An ETF's instrument form and its underlying asset-class exposure are separate
attributes. A data publisher is also distinct from the trading venue.

The future observation key must pin the resolved instrument and applicable listing,
provider, native
quote/unit, series/price basis, interval and receipt version. Calendar and
transformation versions belong in evidence and calculation/protocol identity.
The present source/asset/quote keys remain the legacy cryptocurrency contract;
existing rows, hashes, archives and original assessments are not reinterpreted.

## Actual trading and publication schedules

UTC remains the interchange clock. Session identity also retains the venue's
local date, IANA time zone and a sourced, versioned schedule. Coverage is measured
against expected observations for that series: normal/short sessions, holidays,
lunch breaks, auctions, maintenance and provider publication delays where relevant.
An exchange closure is different from missing data or a stale quote during an
open session. Some reference series have publication schedules rather than
tradable market sessions. Unexpected halts require observed status; they are not
silently treated as scheduled closures.

Declare whether a horizon means calendar days, trading sessions or elapsed time,
and how a deadline outside a session is handled. Preserve actual interval bounds;
a session's opening/closing bar need not be a full UTC-aligned hour. Annualization
uses the declared series/calendar convention. Neither 365 nor 252 is universal.

## Prices, distributions and contract semantics

Keep raw traded prices, split-adjusted prices, dividend/total-return series and
derived continuous contracts separate. Corporate actions retain announcement,
effective and receipt clocks. Currently adjusted history cannot establish what
was known before a later split, dividend or correction. Delisted instruments and
historical universe membership remain visible to avoid survivorship bias.

Hypothetical equity-plan wealth requires declared handling of distributions,
entitlements, reinvestment and relevant instrument costs. A price-only return
must remain labelled as such. Ledgerly stays authoritative for actual holdings
and transactions; these research models do not reconstruct a real portfolio.

Commodity prices require units and quotation conventions (for example USD per
troy ounce versus USD per barrel). Futures additionally identify expiry, contract
size/multiplier, settlement and the applicable trading schedule. Negative prices
can be valid; percentage/log-return methods still need their own admissibility
rules. Position valuation, collateral/margin and mark-to-market settlement need
explicit models; the current cash-funded spot-plan accounting does not supply them. A continuous futures series retains its chain, roll/adjustment method,
version and source contracts. Contract selection and roll decisions use only chain
and liquidity information available at the applicable cutoff; later knowledge
cannot choose the historical chain. Its synthetic price path is not an executable fill.

## Current code assumptions requiring replacement

| Current owner | Cryptocurrency assumption | Gate before another class uses it |
| --- | --- | --- |
| `asset_universe.py`, configuration and CCXT collectors | Canonical currency-code selectors and exchange market catalogues | Resolved instrument/listing/provider mappings and class-specific capabilities |
| `storage.py`, candle capture | Whole UTC-hour alignment, one-hour bounds and positive OHLC values | Versioned native intervals, instrument semantics and method-specific validity |
| `context.py`, `research.py` | A complete day has 24 hourly bars; daily aggregation is UTC; volatility uses 365 | Sourced session/publication schedules and explicit annualization/return basis |
| `calendars.py`, `freshness.py` | Macro-release schedules and age-based expiry, without exchange-session semantics | Separate session/publication ownership and explicit closed/open/halted/delayed state |
| `qualification.py`, `operating_acceptance.py`, `analytical_store.py` | Expected hours are a continuous interval divided by 3600 | Expected observation grids and closed/open/delayed/missing states |
| `plans.py`, `purchase_windows.py`, prospective protocols | Calendar-time purchase/expiry rules and crypto hourly outcome conventions | Declared session handling, distributions/contracts and matching event clocks |
| Frozen evidence, snapshots and journal cohorts | Legacy source/asset/quote and rule-version identities | New versioned identities without modifying old originals or joining unlike bases |

These are actual implementation gaps. They are not solved by adding `AAPL` or a
futures symbol to the current asset list. Separate provider adapters supply data;
domain modules own identity, calendar, price-basis and execution rules. Use shared
mathematical functions only where their declared assumptions hold. Add interfaces
at those demonstrated variations; retain the existing CCXT adapter's own limits.

## Storage and capacity

The [measured storage roles](adr/0001-storage-for-scaled-research.md) remain useful:
transactional authority/journal and immutable analytical Parquet/DuckDB snapshots.
Instrument class alone does not establish a benefit from changing database engine.
Future schemas and partition selection must accommodate resolved instrument IDs,
series basis, contract expiry, actual intervals, calendar versions and known-at
metadata. A new snapshot format preserves old-format readers and exact provenance.

Plan capacity by instruments, listings/contracts, providers, quotes, frequencies,
vintages and actions. One thousand crypto codes in a synthetic workload do not
qualify one thousand mixed-class listings or a futures chain with every expiry.
Rebenchmark those workloads and writer concurrency before adopting PostgreSQL or
claiming an end-to-end cadence. Live exchange data licensing can differ from
public crypto access; prefer suitable delayed/end-of-day sources where sufficient,
without promising free real-time data or enabling a paid dependency implicitly.

## Implementation order and acceptance

1. Introduce versioned instrument/listing identity, effective/known-at symbol
   resolution and a compatibility mapping for current cryptocurrency records.
   Ambiguous aliases require disambiguation; old evidence hashes stay unchanged.
2. Introduce sourced calendar/publication profiles, expected observation grids and
   capability checks across collection, quality, analytics and frozen protocols.
   Verify weekends, DST, a short session, a lunch break and delayed publication.
3. Qualify one equity listing end to end, starting with a suitable low-frequency
   feed: native data, split/dividend basis, delisting/membership and a matching
   purchase-plan/prospective interpretation. Unknown capabilities fail explicitly.
4. Qualify a commodity reference or listed fund as its own instrument. Add futures
   only with explicit expiry/unit/multiplier/roll semantics and supported valuation;
   keep negative-price cases distinct from methods requiring positive prices.
5. Expose instrument metadata/capabilities and explicit unsupported states through
   CLI/MCP, then measure mixed-class 100/1,000-instrument workloads, costs and actual
   provider acceptance. No simulated period establishes real future performance.

Acceptance cases follow the activated capabilities: identity includes ticker
collisions and multiple listings/currencies where applicable; equity support
includes later-known corporate actions, delistings, closed sessions and early
closes. Continuous-futures support additionally verifies contract rolls and their
point-in-time selection. Published claims require end-to-end evidence for the
selected class and series basis; cryptocurrency benchmarks remain their own scope.
