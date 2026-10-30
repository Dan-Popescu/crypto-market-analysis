# Low-frequency cross-asset sources

The provider catalogue has no default. Collection requires an explicit resolved
series and source choice. No adapter needs a paid subscription, API credential or
implicit paid fallback. An implemented connector is not production acceptance.
Imported, demo and live receipts retain separate data modes.

## Qualification evidence, 2026-10-30

| Source | Implemented path | Semantics and outstanding evidence | Acceptance |
| --- | --- | --- | --- |
| Normalized JSON/CSV | Offline explicit import | Caller declares native currency/unit, raw or adjusted basis, origin and reuse rights. Optional split/dividend observations and completeness declarations retain receipt clocks. Declaration is not independent verification. | Conditional on supplied contracts; no general production-source acceptance |
| FRED graph CSV, `DCOILWTICO` only | Explicit opt-in, bounded HTTP | Candidate WTI commodity reference, expected USD per barrel. Reference observations are not executable trades, listed funds or futures settlements. Actual feed metadata, rights and current HTTP availability still require verification. | Unqualified live access and rights |
| Yahoo chart | Catalogue candidate only; network refused | No verified contract for chart OHLC adjustment across splits, volume adjustment, action timing, delisted history, reuse or endpoint stability. Neither a field called close nor an adjusted-close ratio proves historical raw traded fills. | Unsupported |
| Stooq CSV | Catalogue candidate only; network refused | Adjustment conventions, actions, historical membership, rights and available identifiers are unverified. Do not label an unexplained history as raw. | Unsupported |

Primary-source research was attempted through the available web search tool, but
both search and direct page opening returned empty tool errors. The cloud runtime
reported a current, enforced restricted network policy: FRED, EIA, Yahoo and Stooq
were outside the permitted HTTP hosts. No network-policy bypass or live feed
request was attempted. Consequently the following addresses are **verification
targets, not documents successfully consulted in this run**:

- [FRED series metadata](https://fred.stlouisfed.org/series/DCOILWTICO)
- [FRED graph CSV candidate endpoint](https://fred.stlouisfed.org/graph/fredgraph.csv?id=DCOILWTICO)
- [FRED legal terms](https://fred.stlouisfed.org/legal/)
- [EIA reuse and copyright guidance](https://www.eia.gov/about/copyrights_reuse.php)
- [Yahoo historical-price explanation](https://help.yahoo.com/kb/SLN28256.html)
- [Stooq](https://stooq.com/)

Keyless access does not establish public-domain status or reuse rights. FRED's
publisher role does not establish a universal license over upstream data. Do not
publish retained vendor data until its applicable terms have been checked; tests
in this change contain synthetic observations only. Live collection from a
permitted environment must record actual status, latency and bytes before an
availability claim. No general free real-time equity capability is claimed.

## Adapter contract

`provider_catalog()` returns detached metadata without HTTP or persistence.
`fetch_series(request, received_at=None, transport=None)` returns `status`, an
optional canonical `series`, `source_assessment`, `metrics` and `http_accounting`.
It performs no database writes. `transport` accepts an HTTPX transport for offline
verification. Production callers omit `received_at` to capture the actual wall
clock; the explicit value supports deterministic tests and controlled ingestion.
The canonical result is validated by `prices.validate_series`.

Every request contains:

```json
{
  "descriptor": {
    "id": "explicit-series-id",
    "instrument_id": "resolved-instrument-id",
    "listing_id": "resolved-listing-id",
    "provider": "normalized_import",
    "provider_symbol": "EXPLICIT",
    "native_currency": "USD",
    "unit": "share",
    "price_basis": "raw",
    "data_mode": "import",
    "calendar_id": "explicit-calendar-id",
    "calendar_version": "explicit-version",
    "interval": "session",
    "revision_semantics": "captured_history"
  },
  "start": 0,
  "end": 86400,
  "source_contract": {
    "origin": "https://example.com/authorized-export",
    "rights": "Caller must replace this with the actual applicable permission",
    "price_basis": "raw",
    "action_coverage": "unknown"
  },
  "import_data": {"bars": [], "actions": []}
}
```

All timestamps are integer UTC seconds; pre-1970 dates are supported. The identity
and calendar owners resolve actual instruments/listings, unit and calendar before
this call. An arbitrary ticker is not a resolved instrument. Actions use the
canonical split/cash-dividend schema. Source contracts must match the descriptor's
price basis; continuous futures are refused. Import mode is `import` or `demo`.
No imported history can request `live` or `point_in_time` semantics.

`import_csv` can replace `import_data`. Its required columns are
`start,end,open,high,low,close`; optional columns are `session_date,volume,observed_at`.
Unknown or duplicate columns are refused, including embedded action columns.
For CSV, supply actions separately in the `import_actions` array. For JSON use
`import_data.actions`. The byte/row caps include actions, so a large sidecar cannot
bypass admission. Prices remain native; no FX conversion or adjustment occurs.

Import ingestion sets each bar/action `observed_at` to the actual ingestion clock,
and resets an optional descriptor `action_coverage.known_at` to that clock. A
publisher's original historical date is not proof that this installation knew the
observation then. Announcements, effective dates and payment dates are preserved.
Completeness declarations must use the explicit descriptor `action_coverage`
object consumed by price/plan validation; the free-text source contract alone
does not prove completeness or establish plan eligibility.

## FRED reference selection

Select provider `fred_graph`, symbol `DCOILWTICO`, currency `USD`, unit
`USD_per_barrel`, price basis `raw`, interval `reference`, `listing_id: null`,
`data_mode: live`, and `revision_semantics: captured_history`. Here `raw` means
untransformed published reference observations; it does not mean raw traded fills.
Supply `opt_in_network: true` and a bounded `publication_grid` from the calendar
owner, containing `{session_date, start, end}` for each reference observation date.
Publication-only reference points preserve equal `start` and `end` clocks exactly;
reference intervals can instead have a positive duration declared by the calendar.
The adapter does not invent exchange sessions, publication times or midnight
bounds. Grid endpoints must precede receipt. `observation_date`/legacy `DATE`
identifies the source's observation date, not proof of the actual release time.

Only the fixed HTTPS graph endpoint is used, with explicit bounded date selection;
caller-controlled URLs and redirects are not supported. Blank/dot observations
remain missing, and negative finite values are retained. Reference values occupy
the four OHLC fields equally solely to share the versioned storage shape; they
do not become intraday OHLC or executable fills. No corporate-action data or
point-in-time vintage endpoint is fabricated. Capturing today's history cannot
reconstruct older source revisions or knowledge.

## Bounds and acceptance checks

Default bounds are two HTTP attempts, 20 seconds, 2 MiB decoded response bytes and
5,000 observations/actions. Requests can tighten limits; ceilings are three
attempts, 30 seconds, 2 MiB and 5,000 rows. A surrounding invocation's HTTP budget
is shared and cannot be reset by a retry. Per-attempt timeout is at most ten
seconds. Decoded body bytes are checked every 16 KiB chunk; the rejecting chunk
still counts as actual received bytes. These are cooperative transport deadlines,
not process termination or a guarantee against arbitrary custom transport code.

Only connection failures and 502/503/504 responses get another bounded immediate
attempt. HTTP 429 is deferred without a retry loop; redirects and other failures
are unavailable. Attempt, response, failure and decoded-body counters retain
actual observed work, including unsuccessful attempts. Wire/request/TLS byte
counts remain unknown. Errors omit HTTP URLs, headers and response bodies.
Malformed schema, duplicate dates, nonfinite values and limit violations fail
closed. Imported bytes are reported separately from the zero HTTP attempts.

Offline tests exercise raw equity imports with a split, normalized CSV plus action
sidecars, actual receipt clocks, explicit contracts, negative reference values,
missing observations, byte bounds, request/retry bounds, shared outer budgets,
redirect refusal and sanitized transport errors. These prove adapter behavior;
they do not prove source availability, completeness, reuse rights, delisting
coverage, historical universe membership or financial performance.
