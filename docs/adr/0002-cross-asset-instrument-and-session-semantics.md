# Use instrument and series semantics for future asset classes

Accepted as design constraints: the product will include equities, commodities
and other asset classes in addition to cryptocurrencies. Resolve stable
instrument/listing identities separately from provider aliases; keep price basis,
units, actual intervals, sourced session/publication calendars and known-at
versions explicit. Asset class and instrument form are separate dimensions.

The present multi-code cryptocurrency implementation is the legacy profile, not
proof of other classes' support. Its 24-hour day, whole-hour grid, positive-price
and 365-day assumptions must not become universal rules. New providers require
their relevant domain semantics and acceptance evidence before shared calculations
consume their data. Existing frozen records and historical hashes remain immutable;
new contracts/formats use explicit versions and compatibility readers.

Retain the measured SQLite/Parquet/DuckDB roles while introducing those identities
and profiles at their actual owners. Changing asset class does not by itself
justify a PostgreSQL migration. See [the cross-asset design](../CROSS_ASSET_DESIGN.md)
for concrete code gaps, implementation order and mixed-class acceptance cases.
This decision prepares extension; it does not add non-crypto runtime support.
