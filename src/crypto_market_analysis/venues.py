"""Venue catalogue. Regulatory status requires dated, entity-specific evidence."""

from dataclasses import dataclass


@dataclass(frozen=True)
class VenueSpec:
    id: str
    name: str
    mica_status: str = "unverified"
    legal_entity: str | None = None
    jurisdiction: str | None = None
    official_source: str | None = None
    as_of: str | None = None
    book_limit: int = 100
    preferred_quotes: tuple[str, ...] = ("USD", "USDT", "USDC", "EUR")


USDT_FIRST = ("USDT", "USD", "USDC", "EUR")
EUR_FIRST = ("EUR", "USDC", "USD", "USDT")
FRANCE_PURCHASE_CANDIDATES = ("kraken", "bitvavo", "coinbase")


VENUES = {
    v.id: v
    for v in (
        VenueSpec("binance", "Binance", book_limit=5000, preferred_quotes=USDT_FIRST),
        VenueSpec("coinbase", "Coinbase"),
        VenueSpec("kraken", "Kraken", book_limit=500),
        VenueSpec("bitvavo", "Bitvavo", book_limit=1000, preferred_quotes=EUR_FIRST),
        VenueSpec("okx", "OKX", book_limit=400, preferred_quotes=USDT_FIRST),
        VenueSpec("bybit", "Bybit", preferred_quotes=USDT_FIRST),
        VenueSpec("bitstamp", "Bitstamp"),
        VenueSpec("bitfinex", "Bitfinex"),
        VenueSpec("kucoin", "KuCoin", preferred_quotes=USDT_FIRST),
        VenueSpec("gate", "Gate", preferred_quotes=USDT_FIRST),
        VenueSpec("cryptocom", "Crypto.com", book_limit=50),
        VenueSpec("gemini", "Gemini"),
        VenueSpec("bitget", "Bitget", preferred_quotes=USDT_FIRST),
        VenueSpec("mexc", "MEXC", preferred_quotes=USDT_FIRST),
        VenueSpec("htx", "HTX", book_limit=150, preferred_quotes=USDT_FIRST),
    )
}
