# Measured operation, complete workflows and coherent MCP analysis

Baseline: `f2bf337eb7b3fd733f7b9cc3f84a06aae0b18d9f` (version 0.6.0).
The user authorized all five follow-up projects, including independent parallel
implementation, local verification and integration. No deployment, account key,
paid data source, public publishing or permanent worker is part of this work.

## Acceptance

1. **Observe real HTTP work.** Instrument the existing public HTTP/CCXT seams,
   including catalogue requests, retries, errors and latency. Report observed
   requests and response body bytes per source/endpoint, distinguishing decoded
   body measurements from unknown wire/TLS overhead and uninstrumented clients.
   Never persist headers, credentials, query strings or bodies. Bound accounting
   work and retention, preserve existing logical-operation budgets and expose
   bounded stored measurements through read-only MCP. Exercise actual local HTTP
   transport offline; do not claim synthetic adapter calls are HTTP requests.
2. **Exercise the whole local pipeline.** Add a reproducible, bounded synthetic
   endurance/charge harness using real collection, SQLite, scheduling, MCP reads
   and maintenance. Include virtual multi-day time, partial/slow sources, restart,
   concurrent readers and resource/expiry/quota limits. Record observations,
   correctness failures, timings, memory, storage and source revision. Defaults
   must finish in a practical local run. This is not a production SLO or a
   distributed throughput proof.
3. **Automatically repair recoverable gaps.** Build on named datasets' existing
   repair contracts: discover work, bound requests/ranges/markets, persist progress,
   resume after interruption and respect known provider retention and retry
   deadlines. Explicit local opt-in applies work; inspection is a dry run.
   Old gaps requiring archives remain distinct from API-recoverable work. Keep
   import/live lineage, real receipt times and source/native-market identities.
   Do not activate a permanent scheduler or expose mutation through MCP.
4. **Offer a coherent analysis dossier.** Add one read-only bounded CLI/MCP
   operation assembling market context, stored venue/derivative/quote context and
   calendar evidence from one SQLite read transaction, cutoff and physical
   watermark. Reuse existing calculations; do not recursively invoke MCP tools,
   rewrite the domain rules or patch shared Store instances. Native markets stay
   separate, cost assumptions remain explicit, missing sections remain unknown,
   and detail paging retains the complete frozen result. Concurrent late writes
   must not affect only one section. Preserve existing independent tool contracts.
5. **Make real qualification portable.** Provide a documented bounded local
   capture/qualification route for BTC/EUR, ETH/EUR, BTC/USD and ETH/USD using
   existing free public collectors and quality gates. Default inspection is
   read-only; refresh is explicit. Record independent native coverage, actual
   failures, gaps, freshness, chronology, source rights and resource limits.
   Reuse existing archive/dataset workflows for older history rather than
   downloading gigabytes or claiming recent-only APIs supply years of data.
   Test runnable CLI and injected transports here; real provider acceptance
   remains unverified while the enforced cloud policy excludes those hosts.

## Integration and verification

HTTP measurement owns collector/provider seams and run payload instrumentation.
Endurance owns its harness. Repair owns the dataset repair extension. Coherent
analysis owns the shared-read Store boundary and dossier calculations. Portable
qualification owns its workflow module. The integrating agent owns CLI/MCP
registration, request schemas, shared configuration, documentation and release
metadata. Independent owners use isolated worktrees and focused tests.

After integration run the required offline checks, actual local CLI/MCP scenarios,
bounded capacity experiments and independent Standards/Spec reviews. Record exact
tested revisions, raw local artifacts, limitations and remaining real-input
dependencies. Keep generated runtime data outside version control. Preserve
OBSERVER names, immutable journals, original replay, UTC/known-at clocks, France
purchase eligibility boundaries and separate native EUR/USD/stablecoin identities.
