# Accumulation release review

Two independent reviews examined the change from
`e81a4ed5e2a05a8c1328686314b067038e733b92` to the initial integrated
`29ae1a7d09d9f64c02103a4a19f943c899343f6a`. The adopted cloud brief and
implementation plan were the specification. Later fixes were reviewed against
the original findings, then covered by the final integrated suite.

## Standards

One confirmed issue: the canonical metric identity omitted native contract
conventions that vigilance treated separately. Different contract sizes could
collapse during retrieval. Canonical identities now include contract size,
settlement, market type, funding period, aggregation and counting conventions;
equivalent explicit funding-period aliases normalize to seconds. Storage,
descriptive interpretation and vigilance share this definition. The preserving
schema-v2 migration was independently checked with both original payloads intact.

The reviewer verified this finding fixed at `eab7088`. No other high-confidence
actionable standards finding was reported.

## Spec

Four confirmed issues were corrected:

1. Ordinary two-year assessments with an attached plan exceeded the JSON-node
   limit because candle histories were duplicated. Evidence is now deduplicated,
   archived losslessly with a checksum and bounded by uncompressed size. A real
   two-year Engine issuance regression passes.
2. Collection-time fallback timestamps were classified as fresh market events.
   Their market event age now remains unknown and unusable for freshness-sensitive
   interpretation; ingestion age remains separately visible.
3. Qualified unit/period interpretation was disconnected from the shared bundle.
   It is now integrated, incompatible conventions remain distinct, and unknown
   OI units suppress comparisons. A follow-up regression identified valid funding
   classifications being suppressed; explicit actual/projected metric names now
   preserve their classifications and legitimate comparisons.
4. Outcome observation ignored saved plans. It now appends 90/180-day plan
   outcomes using frozen rules/original inputs and first-observed fully
   post-issuance prices. Missing terminals stay pending and originals stay immutable.

Focused verification found the original four issues addressed; its funding
follow-up was fixed and tested before final verification. No scope creep was
reported. Additional integration checks excluded delayed-issuance barrier
ambiguity from legacy scoring and verified actual CLI/MCP calculation paths.

These reviews support software correctness and intent fit. They establish neither
a predictive advantage nor live provider availability. Exact final verification
is recorded in [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md).
