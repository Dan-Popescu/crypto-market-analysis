# Cross-asset implementation and acceptance

The approved scope turns [the design](CROSS_ASSET_DESIGN.md) into a versioned
instrument research path. Existing cryptocurrency observations, frozen evidence,
snapshots, hashes and commands retain their original meaning.

## Delivery sequence

1. **Domain foundations:** stable instruments/listings, effective and knowledge
   clocks, universe history, sourced session/publication schedules, native price
   bases, corporate actions, explicit mathematical validity and futures contracts.
2. **Persisted research:** atomic append-only native observations, bounded known-at
   reads, coherent identity/calendar checks, hypothetical cash plans, explicit
   source adapters and source qualification, immutable analytical snapshots.
3. **Agent interfaces and acceptance:** CLI mutations remain explicit, MCP remains
   read-only; expose metadata, capabilities, gaps, horizons and unsupported cases.
   Add frozen prospective contracts and mixed-class 100/1,000-instrument workload
   measurements. Review, test and merge each coherent delivery before depending on it.

Independent owners implement identity, sessions, prices/accounting, storage,
providers and the application facade in isolated worktrees. Shared changes and
integration belong to the orchestrator; reviewers examine immutable commit ranges.

## Financial and operational acceptance

Acceptance includes symbol collisions, listing currencies, later-known aliases,
delistings and universe membership; DST, holidays, short sessions, lunch breaks,
publication delays and unknown calendar coverage; native intervals and vintages;
split/dividend clocks, captured-history limitations and price-only labelling;
commodity units, negative prices, expiry/multiplier/settlement and explicit rolls.
Raw observations and derived continuous series remain separate. Cash-funded
equity research cannot silently value futures or execute reference prices.

Synthetic/offline tests qualify implementation behavior. They do not establish
public endpoint access, licensing, real-time availability, actual elapsed provider
acceptance or investment performance. Any such unverified capability must remain
visible in reports, CLI/MCP and final delivery notes. No paid API is enabled.

## Verification

Focused tests accompany each owner. Integrated delivery runs the required frozen
lock, Ruff and full offline suite through `uv run --frozen python tools/check.py`.
CLI and actual MCP transport checks cover noninitializing reads, explicit writes,
mode/currency separation and bounded responses. Benchmarks count instruments,
listings, series, intervals and vintages separately. Record tested revisions and
merged-main verification in the pull requests.
