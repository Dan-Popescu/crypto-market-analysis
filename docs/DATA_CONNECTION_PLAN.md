# Automatic local data connections

Baseline: `691c1296cbcb2f280c9646a74b08dee5b0f857ef`.
The user approved these five follow-up projects on October 29, 2026.
The implementation must work through local commands without a deployment,
exchange account, paid API or permanent worker.

## Acceptance

1. Retain bounded native order-book levels and connect them to existing venue
   cost/disagreement calculations. Reuse each public book request for both
   retained levels and current metrics. Unknown exchange time remains unknown;
   received depth is not a complete historical book or guaranteed fill.
   Fee/product/rule assumptions remain explicit and unknowns suppress costs.
2. Build existing quote/derivatives/venue requests from persisted observations.
   Read coherent bounded known-at evidence, retaining historical endpoint
   vintages. Each result identifies consumed inputs and omissions. Reading never
   collects, issues an assessment or creates journal tables.
3. Acquire official ECB calendar/reference data and public stablecoin spot data.
   Parse defensible source formats, retain raw evidence hashes and actual receipt
   time, and surface per-source failures. Date-only reference observations retain
   their precision and do not receive an invented publication/market timestamp.
   Existing manual inputs remain supported.
4. Qualify BTC/EUR, ETH/EUR, BTC/USD and ETH/USD independently against declared
   source/history/freshness/gap/resource requirements. Source documentation,
   legal eligibility, data reuse and measured runtime access are separate facts.
   A source-wide successful run cannot certify an unobserved pair. Local
   qualification is read-only; historical ingestion never fabricates known-at
   availability. Hourly history coverage is not automatically complete daily
   return lookback coverage.
5. Provide one explicit local workflow: optional bounded refresh, qualification,
   stored analyses and report, then optional explicitly requested issuance with
   an idempotency key. Default reads do not refresh or issue. Refresh uses actual
   current time in live mode. Failed quality gates suppress issuance without
   hiding partial market reports. Logical request/page limits must not be
   misrepresented as complete HTTP accounting when catalogue calls are unknown.

## Integration and verification

One owner integrates CLI, read-only MCP tools and English reports. Network/write
operations remain explicit CLI actions. New tables must preserve existing SQLite
backup/restore behavior, source/mode/native quote identities and frozen evidence.
No permanent collector, public service or external notification is activated.

Use actual SQLite, injected public clients, source-format fixtures and real local
CLI/MCP transports to verify the full flow. Distinguish synthetic arithmetic from
observed source content and real runtime collection. Run the repository checks
on the final integrated code and record the exact tested revision.

The managed environment currently enforces a package-host allowlist excluding
the public market and ECB hosts. This does not block implementation or injected
tests. A failed real probe must remain an unavailable/error result, without a
synthetic fallback or a claim of provider-wide denial. No network policy bypass
is part of this task. Actual prospective 90/180-day results still require time.
