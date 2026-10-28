# Architecture and invariant ownership

The architecture follow-up to version 0.3.0 targets four demonstrated areas of
duplicated domain knowledge. It preserves the existing Python application,
SQLite storage, CLI and MCP interfaces. Domain terms are defined in
[CONTEXT.md](../CONTEXT.md).

## Scope and order

1. Concentrate purchase-plan activation accounting in the purchase-plan module.
   Fix the brief's omission of a target reserve release at the exact timestamp
   of a new tranche.
2. Capture the candle vintages needed by an issued plan coherently with its other
   evidence. Fix the possibility of mixing a captured market snapshot with
   later-inserted plan versions from the same issuance second.
3. Give the assessment journal ownership of bounded reads, storage lifecycle and
   original retrieval for retries, in addition to its existing immutable writes.
4. Give research ownership of descriptive reference sample selection, including
   sparse evidence below the conditional forecast support threshold.

These changes address architecture and correctness. They do not claim successful
real-source qualification, investment outperformance or a complete operational
backup/replay product. See [IMPROVEMENT_STATUS.md](IMPROVEMENT_STATUS.md) for those
separate product and access limitations.

## Purchase-plan accounting

The purchase-plan module owns tranche expansion, fee-inclusive cash requirements,
exclusive allocation, deferred caps and activation state. Its interface must give
the simulator, price-free funding preview and accumulation brief leverage from
the same accounting implementation. Callers should not reconstruct activation
state by subtracting a second from the cutoff or duplicating cash formulas.

Contributions precede deadline releases and new allocations at equal timestamps.
Slices allocate in declared order; earlier commitments cannot be spent twice.
Quote fees consume quote cash; base fees reduce received quantity. Deferred caps
apply to target commitments, including quote fees, rather than every waiting
purchase. No allocation borrows from future contributions.

Historical execution and a price-free preview retain different assumptions. The
simulator can observe a price, fill an eligible purchase and release rounding
refunds. A preview cannot infer those events. Shared accounting must preserve this
distinction rather than force both calculations to produce identical balances.

Tests cross the same interface as callers and cover simultaneous contributions,
reserve releases and tranches, fixed fees, partial funding, caps, missing anchors
and conservation of committed cash. Existing historical execution, waiting,
terminal valuation and cost-sensitivity contracts remain load-bearing.

## Captured evidence

The capture module owns the coherent set of permitted candle vintages. Calculations
consume that set without a second unrestricted storage read. A source correction
inserted after capture must not affect only one calculation inside an assessment.
Latest prices alone are insufficient for known-at replay; required older vintages
remain available, with truthful availability times and content references.

Capture remains bounded by operation, source, native quote and mode. Ordinary
context and brief calculations should not archive unused history. Issued evidence
retains its lossless archive, checksum and uncompressed size limit. Old originals
remain immutable and readable.

## Assessment journal

The journal module owns schema initialization for explicit writes, noninitializing
reads, evidence projection, known-at cutoffs, stable ordering, bounds and retry
retrieval. The application owns request identity and calculation; the prospective
module owns interpretation of selected originals and outcomes.

The journal interface is the seam for real SQLite tests. Reading a missing journal
must not create one. Bounded overviews omit frozen archive bodies without decoding
them, report omissions and select originals and outcomes in a coherent read.
Concurrent identical requests return their original; conflicting requests fail.
No generic persistence adapter family is needed for the single SQLite backend.

## Research samples

Research owns alignment, feature eligibility, endpoint availability, purging,
disjoint sample selection and reference uncertainty. Purchase windows own event
names, issuance clocks and freshness. Both obtain descriptive evidence through
the research interface, rather than purchase windows repeating a sparse-history
selection loop or importing private research helpers.

The existing 20-effective-sample threshold for conditional estimates remains.
Descriptive frequencies below that threshold retain honest counts and uncertainty;
showing them does not establish a supported conditional forecast.

## Keep the useful depth

Named history datasets already hide identity, raw hashes, recovery and explicit
repair behind one module. Liquidity retention owns physical row identities and
concurrency rules required for safe retry. CLI and MCP are real transport adapters
sharing the application calculations; their distinct input handling remains useful.
These modules are not split merely because their files are long.

Future changes should improve locality: a domain rule and its verification belong
with its owner. The deletion test should expose whether a proposed module hides
meaningful knowledge or only adds forwarding. Architecture does not guarantee zero
future debt; preserving these invariants and reviewing changes makes debt visible.
