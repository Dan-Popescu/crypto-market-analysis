# Remaining offline work

Baseline: `2f3d621e9424bbb067071ba86f72f494a13f01f1`.
The four architecture projects were complete at this baseline. They did not
complete the broader improvement backlog. The user's October 29, 2026 instruction
continues the useful work that can be implemented without a deployment.

The delivered implementation and checks are recorded in [OFFLINE_STATUS.md](OFFLINE_STATUS.md).

## Execution order and acceptance

| Order | Work | Acceptance |
| --- | --- | --- |
| 1 | Checked backup, restoration and original replay | A coherent SQLite backup has verified integrity, hashes, journal evidence and actual build/lock provenance. Restoration publishes only into a new or empty destination. Original calculations replay from frozen evidence without collection or journal writes. Named-dataset metadata and recovery payloads are preserved or explicitly refused; silent lineage loss is unacceptable. |
| 2 | Prospective scorecards | Bounded, read-only 90/180-day plan results compare holdings plus cash against regular purchases, separating qualified samples, inspectable scenarios, pending and excluded records. Selection precedes outcome inspection; repeated appends and overlapping origins do not inflate samples. Event frequencies remain descriptive where forecast/outcome comparability is unestablished. |
| 3 | Shared BTC/ETH budget envelope | Optional dated shared cash funds explicit plan families in declared order, with exclusive allocations, fees, caps and reserve release. Only the envelope's contributions supply shared funds. Price-free calculations reuse plan accounting; there is no actual portfolio or tax ledger. EUR and USD stay independent. |
| 4 | Purchase-cost and venue-disagreement calculations | Explicit local snapshots produce amount-specific hypothetical costs only when product, fee, rule and size coverage are sufficient. Missing/stale/skewed sources remain visible. Same-native-quote disagreement is diagnostic; unknown costs suppress comparisons. Global analytical coverage stays independent of France purchase eligibility. |
| 5 | EUR, stablecoin and native derivatives context | Sourced manual ECB schedule import preserves date-only versus explicitly timed events and European daylight saving. Explicit dated FX references and directly observed stablecoin quotes provide informational basis explanations with transformation provenance, without relabelling currencies or inferring executable rates. Native funding/OI series have known-at 4h/24h comparisons, preserving periods, units, contract conventions, timestamp uncertainty and missing endpoints. |
| 6 | Usable adapters and documentation | Each calculation is reachable through CLI and bounded read-only MCP inputs where appropriate. Filesystem backup/restore operations remain explicit CLI writes. Reports expose unknowns, exclusions and assumptions. Synthetic examples and actual local protocol/CLI tests demonstrate use. |

Independent implementations may run in parallel in isolated worktrees. Integration
follows the order above, with one owner for CLI, MCP and reporting edits. Existing
configuration, native market identity, modes, archive layout and rule contracts
remain compatible unless an explicit documented correction requires otherwise.

## What completion can establish

Software tests can establish arithmetic, chronology, evidence integrity and local
workflow behavior. They cannot establish successful source access or investment
advantage. A prospective 90/180-day result still requires actual subsequent time
and observations. A snapshot calculation is not a live-market acceptance test.

Real four-market history/refresh qualification remains separate and blocked by
the managed cloud's source-host network policy. No deployment is needed to run
it from an ordinarily authorized reachable environment. Automated perpetual
collection and public hosting are activation decisions, not conditions for the
offline features above.

Previously rejected scope remains rejected: new predictive models, optimized
thresholds, composite buy scores, precise crash probabilities, complete historical
books/liquidations, comprehensive automatic ETF scraping, exchange keys, a web
application, Ledgerly synchronization and external notifications.
