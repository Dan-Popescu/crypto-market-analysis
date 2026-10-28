"""Dated endpoint contracts, French entity evidence and unit-aware context.

Documentation qualification does not assert runtime access or a reuse licence.
The catalogue never filters global market analysis by purchase eligibility.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from .models import Observation

CHECKED_AT = "2026-10-28"


@dataclass(frozen=True)
class EndpointProfile:
    id: str
    dataset: str
    endpoint: str
    documentation_url: str
    authentication: str = "none"
    cost: str = "no_paid_subscription_documented"
    history: str = "earliest_market_history_unknown"
    page_cap: int | None = None
    timestamp_unit: str = "milliseconds"
    timezone: str = "UTC"
    retention: str = "unknown"
    units: str = "OHLC in native quote; volume in base asset"
    rights: str = "unresolved; public access is not a redistribution licence"
    rights_url: str = ""
    documentation_checked_at: str = CHECKED_AT
    access_status: str = "untested"
    access_checked_at: str | None = None
    earliest_verified_time: int | None = None
    latest_verified_time: int | None = None
    observed_gaps: str = "unknown"
    implementation: str = "documented_candidate"
    notes: str = ""


@dataclass(frozen=True)
class SourceProfile:
    id: str
    name: str
    role: str
    endpoints: tuple[EndpointProfile, ...]
    purchase_jurisdiction: str | None = None
    purchase_evidence: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


def _spot(
    id: str, endpoint: str, doc: str, cap: int | None, *, rights_url: str = "", **kwargs
) -> EndpointProfile:
    return EndpointProfile(
        "spot_candles", "spot_ohlcv", endpoint, doc, page_cap=cap, rights_url=rights_url or doc, **kwargs
    )


def _book(endpoint: str, doc: str, cap: int | None, **kwargs) -> EndpointProfile:
    return EndpointProfile(
        "spot_book",
        "spot_order_book",
        endpoint,
        doc,
        page_cap=cap,
        history="current_snapshot_only",
        retention="no_historical_snapshots_promised",
        units="price in native quote; size in base asset",
        rights_url=doc,
        **kwargs,
    )


def _entity(
    entity: str, regulator: str, url: str, commencement: str, services: tuple[str, ...]
) -> dict[str, Any]:
    return {
        "legal_entity": entity,
        "home_regulator": regulator,
        "jurisdiction": "France",
        "status": "AMF_PSCA_passport_record_verified",
        "source_url": url,
        "checked_at": CHECKED_AT,
        "intended_french_services_start": commencement,
        "services": list(services),
        "scope": "This entity and listed services only; not brand-wide or product-wide authorization.",
        "limitations": "Recheck entity contract and intended service; not execution-quality or uptime evidence.",
    }


_COMMON_SERVICES = (
    "custody_and_administration",
    "exchange_crypto_for_funds",
    "exchange_crypto_for_crypto",
    "execution_of_orders",
    "placing",
    "reception_and_transmission_of_orders",
    "transfer_services",
)
FRANCE_PURCHASE_ENTITIES = {
    "kraken": _entity(
        "Payward Europe Solutions Limited",
        "Central Bank of Ireland",
        "https://protectepargne.amf-france.org/acteurs-listes-blanches/psca/"
        "payward-europe-solutions-limited-kraken-digital-asset-exchange-kraken",
        "2025-06-25",
        (*_COMMON_SERVICES, "portfolio_management"),
    ),
    "coinbase": _entity(
        "Coinbase Luxembourg S.A.",
        "Commission de Surveillance du Secteur Financier",
        "https://protectepargne.amf-france.org/acteurs-listes-blanches/psca/coinbase-luxembourg-sa",
        "2025-06-20",
        _COMMON_SERVICES,
    ),
    "bitvavo": _entity(
        "Bitvavo B.V.",
        "Autoriteit Financiële Markten",
        "https://protectepargne.amf-france.org/acteurs-listes-blanches/psca/bitvavo-bv",
        "2025-07-14",
        ("custody_and_administration", "operating_crypto_trading_platform", "transfer_services"),
    ),
}

_SPOTS = {
    "binance": (
        "Binance",
        "/api/v3/klines",
        "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints",
        1000,
        {},
    ),
    "coinbase": (
        "Coinbase",
        "/api/v3/brokerage/market/products/{product_id}/candles",
        "https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-candles",
        350,
        {
            "timestamp_unit": "seconds",
            "rights_url": "https://www.coinbase.com/legal/market_data",
            "rights": "limited personal/internal research; redistribution and AI/ML uses require intended-use review",
            "notes": "Advanced Trade; Exchange API is distinct (300); inspected CCXT caps at 300.",
        },
    ),
    "kraken": (
        "Kraken",
        "/0/public/OHLC",
        "https://docs.kraken.com/api/docs/rest-api/get-ohlc-data/",
        720,
        {
            "timestamp_unit": "seconds",
            "history": "last_720_bars_including_uncommitted_current_bar",
            "retention": "720 bars; backward pagination cannot escape retention",
        },
    ),
    "okx": (
        "OKX",
        "/api/v5/market/history-candles",
        "https://www.okx.com/docs-v5/en/#rest-api-market-data-get-candlesticks-history",
        300,
        {
            "history": "recent_years_documented; exact_hourly_coverage_unverified",
            "notes": "Regional host and current endpoint limits require runtime verification; daily UTC uses 1Dutc.",
        },
    ),
    "bybit": (
        "Bybit",
        "/v5/market/kline",
        "https://bybit-exchange.github.io/docs/v5/market/kline",
        1000,
        {"notes": "Newest-first; preserve category and quote."},
    ),
    "bitstamp": (
        "Bitstamp",
        "/api/v2/ohlc/{market_symbol}/",
        "https://www.bitstamp.net/api/",
        1000,
        {"timestamp_unit": "seconds", "rights": "commercial data use requires Data License Agreement"},
    ),
    "bitfinex": (
        "Bitfinex",
        "/v2/candles/trade:1h:{symbol}/hist",
        "https://docs.bitfinex.com/reference/rest-public-candles",
        10000,
        {"notes": "Native column order differs; set explicit limit and sort."},
    ),
    "kucoin": (
        "KuCoin",
        "/api/v1/market/candles",
        "https://www.kucoin.com/docs-new/rest/spot-trading/market-data/get-klines",
        1500,
        {
            "timestamp_unit": "seconds",
            "notes": "Classic spot, not UTA futures; no-trade intervals omitted.",
            "rights_url": "https://www.kucoin.com/legal/terms-of-use",
            "rights": "systematic collection restrictions require intended-use review; France restricted in global terms",
        },
    ),
    "gate": (
        "Gate",
        "/api/v4/spot/candlesticks",
        "https://www.gate.com/docs/developers/apiv4/en/spot/",
        1000,
        {
            "timestamp_unit": "seconds",
            "notes": "limit cannot be combined with from/to; global and EU entities differ.",
        },
    ),
    "cryptocom": (
        "Crypto.com",
        "public/get-candlestick",
        "https://exchange-developer.crypto.com/exchange/v1/docs/api/rest/public-get-candlestick",
        None,
        {"notes": "Current docs maximum unknown; inspected CCXT cap 300 is a connector limit."},
    ),
    "gemini": (
        "Gemini",
        "/v2/candles/{symbol}/{time_frame}",
        "https://developer.gemini.com/rest/market-data",
        None,
        {
            "history": "recent_response_only; no_documented_deep_pagination",
            "retention": "unknown",
            "rights_url": "https://www.gemini.com/legal/market-data-agreement",
            "rights": "redistribution/benchmark restrictions; commercial use agreement required",
            "notes": "EEA account closure differs from public API access.",
        },
    ),
    "bitget": (
        "Bitget",
        "/api/v3/market/history-candles",
        "https://www.bitget.com/docs/catalog/market/market-data",
        100,
        {
            "history": "older_than_90_days_route; query_window_maximum_90_days",
            "notes": "v3 history; recent v3 candles cap 1000; CCXT default v2 is distinct.",
            "rights_url": "https://www.bitget.com/support/articles/12560603797947",
            "rights": "limited revocable API licence; global terms restrict France",
        },
    ),
    "mexc": (
        "MEXC",
        "/api/v3/klines",
        "https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/klinecandlestick-data",
        500,
        {"notes": "Hourly interval 60m; official archive advertises data since 2023, not REST retention."},
    ),
    "htx": (
        "HTX",
        "/market/history/kline",
        "https://huobiapi.github.io/docs/spot/v1/en/",
        2000,
        {
            "history": "legacy_latest_only",
            "notes": "CCXT newer /market/history/candles route retention not verified.",
        },
    ),
    "bitvavo": (
        "Bitvavo",
        "/v2/{market}/candles",
        "https://docs.bitvavo.com/docs/rest-api/get-candlestick-data/",
        1440,
        {
            "timezone": "hourly epoch UTC; native daily boundary unresolved",
            "notes": "EUR and USDC candidates; no verified native USD listing. No-trade intervals omitted.",
            "rights_url": "https://bitvavo.com/en/developer-agreement",
            "rights": "limited service-related use; storage/aggregation/benchmark restrictions require intended-use review",
        },
    ),
}

# Snapshot endpoint contracts are separate from candle history. Sizes are per side where applicable.
_BOOKS = {
    "binance": (
        "/api/v3/depth",
        "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints",
        5000,
        {},
    ),
    "coinbase": (
        "/api/v3/brokerage/market/product_book",
        "https://docs.cdp.coinbase.com/api-reference/advanced-trade-api/rest-api/public/get-public-product-book",
        None,
        {},
    ),
    "kraken": ("/0/public/Depth", "https://docs.kraken.com/api/docs/rest-api/get-order-book/", 500, {}),
    "okx": ("/api/v5/market/books", "https://www.okx.com/docs-v5/en/", 400, {}),
    "bybit": ("/v5/market/orderbook", "https://bybit-exchange.github.io/docs/v5/market/orderbook", None, {}),
    "bitstamp": (
        "/api/v2/order_book/{market_symbol}/",
        "https://www.bitstamp.net/api/",
        None,
        {"timestamp_unit": "seconds_and_microseconds"},
    ),
    "bitfinex": (
        "/v2/book/{symbol}/{precision}",
        "https://docs.bitfinex.com/reference/rest-public-book",
        None,
        {},
    ),
    "kucoin": (
        "/api/v1/market/orderbook/level2_100",
        "https://www.kucoin.com/docs-new/rest/spot-trading/market-data/get-part-orderbook",
        100,
        {"notes": "Full spot book is private; partial 20/100 is public."},
    ),
    "gate": ("/api/v4/spot/order_book", "https://www.gate.com/docs/developers/apiv4/en/spot/", None, {}),
    "cryptocom": (
        "public/get-book",
        "https://exchange-developer.crypto.com/exchange/v1/docs/api/rest-single-page/",
        50,
        {},
    ),
    "gemini": ("/v1/book/{symbol}", "https://developer.gemini.com/rest/market-data", None, {}),
    "bitget": (
        "/api/v3/market/orderbook",
        "https://www.bitget.com/docs/catalog/market/market-data",
        None,
        {},
    ),
    "mexc": (
        "/api/v3/depth",
        "https://www.mexc.com/api-docs/spot-v3/market-data-endpoints/order-book",
        5000,
        {},
    ),
    "htx": (
        "/market/depth",
        "https://huobiapi.github.io/docs/spot/v1/en/",
        None,
        {"notes": "CCXT unaggregated default limit 150 must not be replaced with 100."},
    ),
    "bitvavo": (
        "/v2/{market}/book",
        "https://docs.bitvavo.com/docs/rest-api/get-order-book/",
        1000,
        {
            "timestamp_unit": "nanoseconds",
            "notes": "Nonce continuity and timestamp propagation need adapter verification.",
        },
    ),
}


# Derivatives use separate product routes even where the spot venue brand matches.
# These are candidate contracts, not claims that the unified adapter implements them.
_DERIVATIVES = {
    "binance": (
        (
            "funding_history",
            "/fapi/v1/fundingRate",
            "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History",
            1000,
            "actual_fraction_per_symbol_native_funding_period; interval_not_assumed",
            "historical_funding; earliest_unknown",
        ),
        (
            "open_interest",
            "/fapi/v1/openInterest",
            "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest",
            None,
            "base_asset_amount_for_USD_M_contract; retain_contract_identity",
            "current_snapshot_only",
        ),
    ),
    "kraken": (
        (
            "futures_ticker",
            "https://futures.kraken.com/derivatives/api/v3/tickers",
            "https://docs.kraken.com/api/docs/futures-api/trading/get-tickers/",
            None,
            "native_OI_and_actual_predicted_funding; contract_mapping_required",
            "current_snapshot_only; separate_futures_adapter",
        ),
    ),
    "coinbase": (
        (
            "global_derivatives",
            "https://drb.coinbase.com/api/v2",
            "https://docs.cdp.coinbase.com/coinbase-app/advanced-trade-apis/rest-api",
            None,
            "native_instrument_funding_and_OI; separate_JSON_RPC_contract",
            "not_supplied_by_spot_adapter",
        ),
    ),
    "okx": (
        (
            "funding_history",
            "/api/v5/public/funding-rate-history",
            "https://www.okx.com/docs-v5/en/",
            None,
            "fraction_per_native_instrument_interval",
            "history_retention_requires_current_endpoint_check",
        ),
        (
            "open_interest",
            "/api/v5/public/open-interest",
            "https://www.okx.com/docs-v5/en/",
            None,
            "contracts_and_coin_and_USD_notional_are_distinct_fields",
            "current_snapshot_only",
        ),
    ),
    "bybit": (
        (
            "funding_history",
            "/v5/market/funding/history",
            "https://bybit-exchange.github.io/docs/v5/market/history-fund-rate",
            200,
            "fraction_per_instrument_fundingInterval; interval_in_minutes",
            "earliest_unknown",
        ),
        (
            "open_interest",
            "/v5/market/open-interest",
            "https://bybit-exchange.github.io/docs/v5/market/open-interest",
            200,
            "linear_base_coin; inverse_USD; preserve_contract_category",
            "symbol_launch_dependent",
        ),
    ),
    "bitstamp": (
        (
            "derivatives",
            "/api/v2/funding_rate/{market_symbol}/",
            "https://www.bitstamp.net/api/",
            None,
            "native_product_funding; verify_BTC_ETH_product_and_entity",
            "history_time_filters_at_most_30_days_old",
        ),
    ),
    "bitfinex": (
        (
            "derivatives_status",
            "/v2/status/deriv",
            "https://docs.bitfinex.com/reference/ws-public-status",
            None,
            "current_funding_next_event_accrual_and_OI_native_fields",
            "current_snapshot_only; margin_lending_not_perpetual_funding",
        ),
    ),
    "kucoin": (
        (
            "futures_contract",
            "/api/v1/contracts/{symbol}",
            "https://www.kucoin.com/docs-new/rest/futures-trading/market-data/get-symbol",
            None,
            "OI_contracts_with_multiplier; native_funding_interval",
            "current_contract_metadata",
        ),
    ),
    "gate": (
        (
            "funding_history",
            "/api/v4/futures/{settle}/funding_rate",
            "https://www.gate.com/docs/developers/apiv4/en/futures/",
            None,
            "fraction_per_contract_interval",
            "earliest_unknown",
        ),
        (
            "open_interest_history",
            "/api/v4/futures/{settle}/contract_stats",
            "https://www.gate.com/docs/developers/apiv4/en/futures/",
            None,
            "OI_contracts_and_quote_notional_separate",
            "history_available; earliest_unknown",
        ),
    ),
    "cryptocom": (
        (
            "derivatives_ticker",
            "public/get-tickers",
            "https://exchange-developer.crypto.com/exchange/v1/docs/api/rest-single-page/",
            None,
            "ticker_oi_native_units_unresolved",
            "current_snapshot_only",
        ),
        (
            "funding_valuations",
            "public/get-valuations",
            "https://exchange-developer.crypto.com/exchange/v1/docs/api/rest-single-page/",
            None,
            "actual_current_estimated_funding_separate_types",
            "type_dependent_history_unknown",
        ),
    ),
    "gemini": (
        (
            "derivatives",
            "public funding and risk statistics",
            "https://developer.gemini.com/rest/derivatives",
            None,
            "funding_USD_amount_per_long_one_contract_per_hour; not_dimensionless_rate",
            "retention_unknown; native_GUSD_quote_not_USD",
        ),
    ),
    "bitget": (
        (
            "derivatives",
            "v3 public funding and OI market endpoints",
            "https://www.bitget.com/docs/catalog/market/market-data",
            None,
            "native_contract_category_interval_and_OI_units_required",
            "v2_v3_route_and_retention_unverified",
        ),
    ),
    "mexc": (
        (
            "funding_history",
            "/api/v1/contract/funding_rate/history",
            "https://mexcdevelop.github.io/apidocs/contract_v1_en/",
            1000,
            "fraction_per_native_contract_period",
            "page_based; earliest_unknown",
        ),
        (
            "contract_ticker",
            "/api/v1/contract/ticker",
            "https://mexcdevelop.github.io/apidocs/contract_v1_en/",
            None,
            "holdVol_contract_holdings; conversion_requires_contract_size",
            "current_snapshot_only",
        ),
    ),
    "htx": (
        (
            "derivatives",
            "public linear swap funding/OI routes",
            "https://huobiapi.github.io/docs/usdt_swap/v1/en/",
            None,
            "contract_units_and_period_unresolved_for_current_v5_route",
            "current_v5_route_retention_unverified",
        ),
    ),
}


def _venue_profiles() -> dict[str, SourceProfile]:
    profiles = {}
    for id, (name, endpoint, doc, cap, kwargs) in _SPOTS.items():
        book_endpoint, book_doc, book_cap, book_kwargs = _BOOKS[id]
        spot = _spot(id, endpoint, doc, cap, **kwargs)
        book = _book(book_endpoint, book_doc, book_cap, **book_kwargs)
        # Source rights apply independently to each endpoint; a snapshot has no implied licence exemption.
        book = EndpointProfile(**{**asdict(book), "rights": spot.rights, "rights_url": spot.rights_url})
        derivatives = tuple(
            EndpointProfile(
                endpoint_id,
                "derivatives_context",
                route,
                documentation,
                page_cap=page_cap,
                units=units,
                history=history,
                rights=spot.rights,
                rights_url=spot.rights_url,
                notes="Separate product contract; no trading eligibility or unified-adapter support inferred.",
            )
            for endpoint_id, route, documentation, page_cap, units, history in _DERIVATIVES.get(id, ())
        )
        profiles[id] = SourceProfile(
            id,
            name,
            "global_market_analysis",
            (spot, book, *derivatives),
            "France" if id in FRANCE_PURCHASE_ENTITIES else None,
            FRANCE_PURCHASE_ENTITIES.get(id, {}),
        )
    return profiles


def _context(id: str, name: str, dataset: str, endpoint: str, doc: str, **kwargs) -> SourceProfile:
    return SourceProfile(
        id,
        name,
        "optional_context",
        (
            EndpointProfile(
                dataset, dataset, endpoint, doc, rights_url=kwargs.pop("rights_url", doc), **kwargs
            ),
        ),
    )


SOURCE_PROFILES = _venue_profiles()
SOURCE_PROFILES.update(
    {
        "kraken_archive": _context(
            "kraken_archive",
            "Kraken official OHLCVT files",
            "spot_history_archive",
            "official quarterly/full OHLCVT CSV/ZIP files",
            "https://support.kraken.com/in/articles/360047124832-downloadable-historical-ohlcvt-open-high-low-close-volume-trades-data",
            timestamp_unit="seconds",
            history="per_pair_published_files; latest_archive_plus_REST_can_leave_gap",
            retention="published_file_coverage_only",
            implementation="local_hourly_file_import",
            notes="Official full archives can be multi-GB; select exact pair hourly member within explicit limits.",
        ),
        "binance_vision": _context(
            "binance_vision",
            "Binance Vision spot archives",
            "spot_history_archive",
            "https://data.binance.vision/data/spot/",
            "https://github.com/binance/binance-public-data/blob/master/README.md",
            timestamp_unit="microseconds_from_2025; milliseconds_before_2025",
            history="per_symbol_daily_monthly_files",
            retention="published_files_can_be_corrected",
            rights="CC BY-NC-SA 4.0 dataset v1.0 (2026-08-26); exceptions apply",
            rights_url="https://github.com/binance/binance-public-data/blob/master/TERMS_AND_CONDITIONS.md",
            implementation="local_hourly_file_import_with_SHA256",
        ),
        "deribit": _context(
            "deribit",
            "Deribit",
            "options_snapshot",
            "https://www.deribit.com/api/v2/public/get_book_summary_by_currency",
            "https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency",
            units="IV percent; options OI underlying coin; inverse futures OI USD",
            history="current_snapshot_only",
            retention="historical_option_surface_not_supplied",
            implementation="existing_public_adapter",
            notes="Production host; instrument family/settlement/actual expiry required. Quoted IV is not a probability.",
        ),
        "coinmetrics": _context(
            "coinmetrics",
            "Coin Metrics Community",
            "network_daily",
            "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics",
            "https://docs.coinmetrics.io/api/v4/",
            timestamp_unit="ISO8601",
            units="AdrActCnt addresses; TxCnt transactions per daily period",
            history="asset_metric_community_catalogue_dependent",
            rights="CC BY-NC 4.0; attribution and noncommercial restrictions",
            rights_url="https://github.com/coinmetrics/data/blob/master/README.md",
            implementation="existing_public_adapter",
            notes="Current vintage; exact historical publication time unknown. Addresses are not people.",
        ),
        "defillama": _context(
            "defillama",
            "DefiLlama",
            "stablecoin_supply",
            "https://stablecoins.llama.fi/stablecoincharts/all",
            "https://api-docs.defillama.com/",
            timestamp_unit="seconds",
            units="USD value of USD-pegged circulating supply bucket",
            rights="limited personal noncommercial use; republication/commercial extraction needs permission",
            rights_url="https://defillama.com/terms",
            implementation="existing_public_adapter",
            notes="Supply is not attributed BTC/ETH inflow; historical revisions/publication times unknown.",
        ),
        "fred_csv": _context(
            "fred_csv",
            "FRED graph CSV",
            "macro_series",
            "https://fred.stlouisfed.org/graph/fredgraph.csv",
            "https://fred.stlouisfed.org/help/graphs/share-my-fred-graph/export-options/",
            timestamp_unit="calendar_date",
            units="per_series",
            timezone="release_timezone_unknown",
            rights="per-series rights; VIXCLS includes Cboe copyright",
            rights_url="https://fred.stlouisfed.org/series/VIXCLS",
            implementation="existing_public_adapter",
            notes="Web export, not keyed API. Latest vintage is not known at historical midnight.",
        ),
        "fred_api": _context(
            "fred_api",
            "FRED/ALFRED API",
            "macro_vintages",
            "https://api.stlouisfed.org/fred/series/observations",
            "https://fred.stlouisfed.org/docs/api/fred/series_observations.html",
            authentication="free_registered_key",
            cost="free_key; no_paid_dependency",
            timestamp_unit="calendar_date",
            units="per_series",
            timezone="release_timezone_unknown",
            rights="per-series rights plus API terms",
            rights_url="https://fred.stlouisfed.org/docs/api/terms_of_use.html",
            notes="Day-resolution vintages do not establish intraday release time; no key configured by this project.",
        ),
        "bls": _context(
            "bls",
            "Bureau of Labor Statistics",
            "release_calendar",
            "https://www.bls.gov/schedule/news_release/bls.ics",
            "https://www.bls.gov/schedule/news_release/cpi.htm",
            timestamp_unit="iCalendar",
            timezone="America/New_York",
            units="scheduled_release_time",
            history="published_schedule_only",
            rights="generally US public domain; stated exceptions apply",
            rights_url="https://www.bls.gov/opub/copyright-information.htm",
            implementation="bounded_ICS_parser",
            notes="Scheduled CPI time is not actual publication; retain UID and sequence.",
        ),
        "federalreserve": _context(
            "federalreserve",
            "Federal Reserve",
            "meeting_calendar",
            "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
            timestamp_unit="HTML_dates",
            timezone="America/New_York",
            units="meeting_dates; explicit_announcements_only_when_time_supplied",
            history="published_schedule_only",
            implementation="bounded_HTML_parser",
            notes="Meeting date alone never implies 14:00 ET decision time; minutes and press conference are separate.",
        ),
        "farside": _context(
            "farside",
            "Farside Investors",
            "etf_daily_net_flow",
            "https://farside.co.uk/bitcoin-etf-flow-all-data/",
            "https://farside.co.uk/ethereum-etf-flow-all-data/",
            timestamp_unit="session_date",
            timezone="US_session_date",
            units="USD_millions",
            history="BTC_since_2024-01-11; ETH_since_2024-07-23; as-published_vintages_unknown",
            rights="automatic_API_and_redistribution_permission_not_established",
            implementation="manual_import_fallback",
            notes="All fund cells dashed with total 0.0 means pending, not measured zero. No automatic feed promised.",
        ),
        "bybit_liquidations": _context(
            "bybit_liquidations",
            "Bybit observed liquidations",
            "liquidation_stream",
            "allLiquidation.{symbol}",
            "https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation",
            units="native_contract_size; bankruptcy_price_in_native_quote",
            history="prospective_stream_only",
            retention="no_historical_archive_verified",
            notes="Buy side means long liquidation; connection gaps unknown, not zero.",
        ),
        "binance_liquidations": _context(
            "binance_liquidations",
            "Binance force-order snapshots",
            "liquidation_stream",
            "{symbol}@forceOrder",
            "https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/Liquidation-Order-Streams",
            units="native_contract_quantity_and_quote_price",
            history="prospective_stream_only",
            retention="no_historical_archive_verified",
            notes="Latest event per symbol/1000ms can omit bursts; not complete liquidation map.",
        ),
    }
)


def source_catalog() -> dict[str, Any]:
    """Return fresh JSON-compatible profiles without mutable shared catalogue state."""
    return {
        "schema_version": 1,
        "documentation_checked_at": CHECKED_AT,
        "runtime_verification": "No live availability is established by documentation qualification.",
        "quote_policy": "EUR, USD, USDT and USDC remain separate native markets.",
        "sources": [asdict(profile) for profile in SOURCE_PROFILES.values()],
    }


def etf_flow_row(
    *, asset: str, session_date: str, fund_values: list[Any], total: Any, observed_at: int, source_url: str
) -> dict[str, Any]:
    """Qualify a manually supplied daily table row; never treat pending totals as zero."""
    date.fromisoformat(session_date)
    if asset not in {"BTC", "ETH"} or not source_url.startswith("https://"):
        raise ValueError("ETF row requires BTC/ETH and an HTTPS source URL.")
    if not isinstance(observed_at, int) or isinstance(observed_at, bool) or observed_at < 0:
        raise ValueError("ETF observation time must be a nonnegative epoch second.")

    def number(value):
        if value is None or str(value).strip() in {"", "-", "–", "—", "N/A"}:
            return None
        result = float(str(value).replace(",", "").strip())
        if not math.isfinite(result):
            raise ValueError("ETF flow must be finite.")
        return result

    values = [number(value) for value in fund_values]
    reported = sum(value is not None for value in values)
    parsed_total = number(total)
    status = "pending" if reported == 0 else "partial" if reported < len(values) else "reported"
    value = parsed_total if status == "reported" else None
    return {
        "asset": asset,
        "session_date": session_date,
        "observed_at": observed_at,
        "source_url": source_url,
        "unit": "USD_millions",
        "status": status,
        "value": value,
        "reported_total": parsed_total,
        "fund_values": values,
        "fund_count": len(values),
        "reported_fund_count": reported,
        "origin": "manual_import",
        "historical_known_at": "unknown",
        "interpretation": "Daily reported net flows, not intraday demand or AUM change.",
    }


def _funding_period(metadata: dict[str, Any]) -> float | None:
    candidates = []
    for key, multiplier in (
        ("funding_interval_seconds", 1),
        ("interval_seconds", 1),
        ("funding_interval_hours", 3600),
    ):
        value = metadata.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and value > 0
        ):
            candidates.append(value * multiplier)
    raw = metadata.get("funding_interval")
    if isinstance(raw, str):
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|m|h|d)\s*", raw, re.I)
        if match:
            value = float(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2].lower()]
            if math.isfinite(value) and value > 0:
                candidates.append(value)
    # Conflicting explicit units are unknown, never resolved by field preference.
    return candidates[0] if candidates and all(item == candidates[0] for item in candidates) else None


def interpret_context(observations: list[Observation], as_of: int) -> dict[str, Any]:
    """Describe available observations without pooling incompatible units/periods.

    Comparisons use exactly the same source/asset/quote/instrument/unit/period and
    actual/projected classification. Unknown OI units or funding periods cannot
    support a comparable change. Revised observations retain their capture time.
    """
    groups: dict[tuple, list[Observation]] = {}
    excluded = 0
    for observation in observations:
        if observation.event_time > as_of or observation.observed_at > as_of:
            excluded += 1
            continue
        metadata = observation.metadata
        kind = metadata.get("value_kind", metadata.get("funding_kind"))
        if kind is None:
            kind = (
                "projected"
                if metadata.get("projected") is True or "predicted" in observation.metric
                else "unknown"
            )
        period = _funding_period(metadata)
        identity = (
            observation.source,
            observation.asset,
            observation.metric,
            observation.quote,
            observation.unit,
            metadata.get("instrument", metadata.get("symbol")),
            period,
            kind,
            metadata.get("expiry"),
            observation.data_mode,
        )
        groups.setdefault(identity, []).append(observation)
    rows = []
    for identity, group in sorted(groups.items(), key=lambda pair: repr(pair[0])):
        # Latest revision for each event time, then latest two distinct event times.
        versions = {}
        for observation in sorted(group, key=lambda item: (item.event_time, item.observed_at)):
            versions[observation.event_time] = observation
        ordered = sorted(versions.values(), key=lambda item: item.event_time)
        latest = ordered[-1]
        funding = "funding" in latest.metric
        oi = "open_interest" in latest.metric
        missing_unit = latest.unit.strip().lower() in {"", "unknown", "contracts_unknown", "none"}
        period = identity[6]
        valid_period = (
            isinstance(period, (int, float))
            and not isinstance(period, bool)
            and math.isfinite(period)
            and period > 0
        )
        comparable = not missing_unit and (
            not funding or (valid_period and identity[7] in {"actual", "projected"})
        )
        status = latest.metadata.get("status", "observed")
        finite = math.isfinite(latest.value)
        usable = status not in {"pending", "missing", "unavailable"} and finite
        row = {
            **asdict(latest),
            "event_age_seconds": as_of - latest.event_time,
            "ingestion_age_seconds": as_of - latest.observed_at,
            "value_kind": identity[7],
            "funding_interval_seconds": period,
            "status": status if finite else "invalid",
            "change": None,
            "change_unit": latest.unit,
            "comparable": comparable and usable,
            "limitations": [],
        }
        if not usable:
            row["value"] = None
        if len(ordered) > 1 and comparable and usable:
            previous = ordered[-2]
            if math.isfinite(previous.value) and previous.metadata.get("status") not in {
                "pending",
                "missing",
                "unavailable",
            }:
                row.update(
                    change=latest.value - previous.value,
                    previous_event_time=previous.event_time,
                    change_period_seconds=latest.event_time - previous.event_time,
                )
        if missing_unit:
            row["limitations"].append("Unknown source unit prevents comparison or aggregation.")
        if funding and not comparable:
            row["limitations"].append(
                "Funding requires a known native period and actual/projected classification."
            )
        if oi:
            row["limitations"].append(
                "Open interest is positioning, not spot buying pressure or liquidation levels."
            )
        if "stablecoin" in latest.metric or latest.source == "defillama":
            row["limitations"].append("Global supply is not an attributed BTC/ETH capital flow.")
        if "option" in latest.metric:
            row["limitations"].append("Quoted implied volatility is not a calibrated event probability.")
        if (
            latest.metadata.get("historical_known_at") == "unknown"
            or latest.metadata.get("vintage") == "current"
        ):
            row["limitations"].append(
                "Current-vintage history does not establish historical release-time availability."
            )
        rows.append(row)
    return {
        "as_of": as_of,
        "observations": rows,
        "excluded_future_observations": excluded,
        "status": "available" if rows else "unavailable",
        "composite_score": None,
        "limitations": ["Context is descriptive; incremental predictive value has not been established."],
    }
