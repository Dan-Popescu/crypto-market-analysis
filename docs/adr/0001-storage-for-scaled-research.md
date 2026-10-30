# Select storage by workload and preserve receipt history

Accepted for the 100-asset target, extensible to 1,000: SQLite WAL remains the
authoritative single-host ingestion/journal store; explicitly exported immutable
all-vintage Parquet snapshots and DuckDB handle repeated fleet research. Generic
asset count alone is not a reason to replace transaction semantics across every
existing reader. Long historical volume is bounded and exported in explicit
selections/parts; analytical exports do not silently prune the authority store.

We compared actual SQLite, DuckDB and PostgreSQL 17.11 in a disposable Docker
container on synthetic native/versioned data at 10, 100 and 1,000 assets. At
1,000 assets/384,000 vintages, SQLite fleet selection took ~794ms; cached verified
DuckDB fleet summary including the application interface took ~336ms, but first
verification plus that query took ~4,515ms. Selective 240-row reads took ~2.9ms in
SQLite versus ~81.6ms through DuckDB. PostgreSQL server fleet execution took
~1,093ms; its selective server query took ~0.82ms. Server execution, client
round trips and payload decoding have different scope. The container was limited
to two CPUs/512MiB; no multi-writer benchmark or generic engine superiority is claimed.
All engines produced matching ordered native/versioned result digests.

PostgreSQL becomes a candidate when multiple independent collectors/services need
concurrent writes, shared transactional job leasing, remote readers or operational
replication, or measured SQLite writer waits violate the actual cadence. That
migration should introduce an owned storage adapter and replay/migration parity
for cutoffs, vintages, idempotence, indexes and immutable journal originals before
cutover. Docker packaging alone does not provide those properties, failover or
lower operational cost. Revisit using measured concurrent workloads rather than
claiming the single-host benchmark settles distributed operation.

DuckDB consumes verified immutable files, disables spill for read-only fleet
queries, and owns bounded memory/thread/query limits plus fail-fast per-process admission.
Cold content verification has a separate cooperative deadline. The first verification cost
is deliberate; identity-checked reuse amortizes it without treating a changed
artifact as cached evidence. This read model never impersonates current live
acceptance or the full transactional dossier. No new database server is required
for the local default, and no permanent container or public deployment is started.
