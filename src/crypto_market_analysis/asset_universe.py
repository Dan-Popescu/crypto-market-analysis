"""Explicit adapter currency codes, without inferring economic identity or support."""

from __future__ import annotations

import re

MAX_ASSETS = 1000
_CODE = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,31}\Z")
SPECIALIZED_ASSETS = frozenset({"BTC", "ETH"})


def validate_asset(value: object) -> str:
    """Require a canonical CCXT base code; tickers do not prove token identity.

    Native quote selection is configured separately. Catalogue availability and
    provider-specific support remain unknown until observed from that provider.
    """
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise ValueError("asset must be an uppercase CCXT currency code of 1 to 32 characters.")
    if value in {"USD", "EUR"}:
        raise ValueError("USD and EUR are quote currencies, not research assets.")
    return value


def validate_assets(values: object) -> list[str]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ASSETS:
        raise ValueError(f"assets must contain between 1 and {MAX_ASSETS} distinct CCXT currency codes.")
    assets = [validate_asset(value) for value in values]
    if len(set(assets)) != len(assets):
        raise ValueError("assets must contain distinct CCXT currency codes.")
    return assets
