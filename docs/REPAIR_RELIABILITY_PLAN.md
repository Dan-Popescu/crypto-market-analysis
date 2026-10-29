# Repair reliability follow-up

Baseline: `ca5fc0e98eb7a4f98e1ef48dcb3767e7b113b176` (version 0.7.0).
The user's instruction to continue authorizes this bounded local follow-up to the
five delivered operating-capacity projects. It needs no deployment or paid API.

## Acceptance

1. Measure actual named-dataset REST transport work, including catalogue failures,
   through the existing HTTP accounting boundary. Persist bounded successful and
   failed measurements with retained run records, expose them through the existing
   CLI/MCP accounting reader, and keep logical request budgets separate. Unsupported
   instrumentation remains unknown. Operational records must not retain secret
   URLs, queries or response bodies. Preserve original raw capture evidence.
2. Rotate automatic repair work durably across selected native markets when a
   cycle's request/hour budget cannot serve all of them. Respect backoff, manual
   ownership, retention and coverage. Inspection never advances the cursor.
   Checkpoint the consumed turn before capture so restart cannot starve a market.
3. Exclude simultaneous cooperating dataset writers with a fail-fast process
   lock acquired before automatic recovery or authoritative planning. Reuse that
   boundary for existing-dataset CLI writes, including manual repair and archive
   staging. A busy operation reports no work and exits unsuccessfully. OS lock
   release after process death must allow the next cycle to resume. Direct
   programmatic callers and external writers must cooperate with the same lock.
4. Verify real process-death recovery at committed journal, materialized capture
   and archive publication boundaries. Inspect before recovery, then check original
   receipt times, hashes, exact candle coverage and idempotency on explicit reopen.
   Also verify uncommitted SQLite work rolls back. Bound every child process and
   clean it up even after failures. These checks do not simulate power loss.
5. Integrate useful cycle summaries and CLI errors, protect dataset report outputs
   from overwriting evidence or request inputs, and preserve all existing read-only
   MCP behavior, native EUR/USD identities and live/import/demo separation.

## Verification

Use focused regression tests, actual local CLI/MCP calls and the required
`uv run --frozen python tools/check.py`. Run independent Standards and Spec
reviews against the baseline and this plan, then record the exact tested code
revision, observations and unavailable provider checks. Generated process evidence
and reports belong outside version control. No permanent worker is activated.
