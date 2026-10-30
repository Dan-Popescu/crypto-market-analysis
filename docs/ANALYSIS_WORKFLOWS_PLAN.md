# Actionable analysis, transport budgets and resumable history

Baseline: `f5ab089aeff816a52685d057462a9ef71577fc0f` (version 0.7.1).
The user authorized all five proposed projects and parallel implementation.
This work adds local capabilities and read-only agent interfaces; it does not
deploy services, start permanent workers, connect accounts or execute orders.

## Acceptance

1. **Explain changes between two analyses.** Compare the same explicit native
   dossier selection at two known-at cutoffs using one physical SQLite snapshot.
   Distinguish new market events, revisions to the same event, late arrivals,
   receipt-only updates, clock/expiry changes, missing evidence and source errors.
   Do not label a changed hash or ingestion time a market movement. Preserve
   native sources, quotes, units and derivative dimensions. Include useful before
   and after values and bounded references; expose exact omissions.
2. **Give the agent an actionable readiness diagnosis.** Reuse actual dossier,
   qualification and optional purchase-window calculations on one read session.
   Explain which claims have sufficient inputs and which are incomplete, stale,
   missing, contradictory, unsupported, not requested or synthetic. Keep coverage,
   empirical support, source access, rights and France entity/service eligibility
   separate. Provide ordered, bounded next actions and valid local command
   guidance without collecting, importing or changing state through MCP.
3. **Enforce transport budgets.** Apply finite actual-request and monotonic-time
   policies to supported public Requests/HTTPX paths, including catalogue work,
   redirects and observable retries. A shared invocation budget spans concurrent
   collection workers and nested capture scopes. Refuse before the next attempt,
   cap known transport timeouts to the remaining deadline, preserve captured
   partial data and expose consumed/refused work separately from logical budgets.
   Unsupported or hidden transports must not claim complete enforcement. These
   controls do not promise interruption of arbitrary client code or OS quotas.
4. **Resume long histories on more free sources.** Add explicit finite Coinbase
   and Bitstamp native BTC/ETH EUR/USD import jobs, independently of existing
   Kraken named datasets. Store actual receipt times and retrospective lineage.
   Inspect without initialization; apply only with explicit local opt-in. Persist
   checkpoints before fetching and recheck actual oldest missing closed hours
   after interruption, partial pages or later writes. Bound pages, requests, rows,
   normalized payload bytes and HTTP work. Respect unsupported pairs, failures,
   empty/nonadvancing replies, retry cooldowns and small-budget fairness. Serialize
   cooperating job writers with the existing lock implementation. Never silently
   skip an unknown old gap or contaminate a named Kraken dataset. Provide archive
   guidance without automatic large downloads or assumed provider availability.
5. **Expose robustness of plan comparisons.** Reuse existing validated Decimal
   plans and chronological evaluation across a small caller-declared set of
   origins, fee/execution cases and missing-hour stresses. Keep native identities,
   fixed policy parameters and known-at versus retrospective semantics. Present
   qualified comparative outcomes against the regular baseline, sign changes,
   ranges and explicit excluded/pending denominators. Suppress stability claims
   when requested comparisons lack support. Do not optimize thresholds, recommend
   a winner, invent fees or describe descriptive stability as statistical
   confidence, calibration or prospective performance.

## Shared boundaries and integration

`Store.read_session` owns the physical snapshot and shared work limits. Nested
composition on an active read view reuses that view and its lifetime; it never
patches a shared Store or opens an independent snapshot. HTTP limits belong to a
transport budget, independently of retained accounting. Existing calculation
owners retain financial and evidence rules. Local locks share one implementation.

Independent owners use isolated worktrees. Root integrates new CLI commands,
read-only MCP tools, request schemas/examples, compact result presentation,
configuration, English documentation and version metadata. New tools accept
bounded objects rather than database paths or SQL. Expensive details remain
retrievable through existing frozen hash-bound paging.

Run focused tests during development, independent Standards/Spec reviews and the
required `uv run --frozen python tools/check.py` on the final integrated version.
Exercise actual local CLI/MCP protocols, representative four-native-market
workflows, loopback transport limits and installation from the built wheel.
Record exact tested revisions, available evidence and unverified provider access.
Generated data, reports and build outputs stay outside version control.
